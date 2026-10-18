#!/usr/bin/env python3
"""Private, asynchronous transport used by the graphical Z Hotspot."""

import fcntl
import ipaddress
import json
import os
from pathlib import Path
import queue
import select
import stat
import subprocess
import sys
import threading
import time

from gi.repository import GLib
from hotspot_manager import DemoManager, HotspotManager, validate_mac, validate_settings
from hotspot_metrics import NetworkMetrics


RPC_ACTIONS = frozenset(("boot", "snapshot", "start", "stop", "settings", "kick", "block", "unblock", "logs", "share_prepare", "share_close"))
RPC_REQUEST_LIMIT = 4096
RPC_RESPONSE_LIMIT = 1024 * 1024
RPC_LOCK_PATH = "/run/mint-hotspot-manager.lock"


def _rpc_error_text(error, *secrets):
    """Keep private credentials and control characters out of displayed errors."""
    value = str(error)
    for secret in sorted({str(item) for item in secrets if item}, key=len, reverse=True):
        value = value.replace(secret, "[password hidden]")
    return "".join(character if character.isprintable() or character == "\n" else " "
                   for character in value)[:2200] or "The operation could not be completed."


def _share_port(params):
    if set(params) != {"port"} or type(params["port"]) is not int or not 1024 <= params["port"] <= 65535:
        raise ValueError("Choose a sharing port from 1024 to 65535.")
    return params["port"]


class _ShareFirewall:
    """Grant local HTTP access only through our own running hotspot.

    This root helper handles firewall access only. The desktop user owns and
    serves shared files in a separate, unprivileged process.
    """
    def __init__(self, manager):
        self.manager = manager
        self.rules = {}
        self.comment = "mint-hotspot-share-" + str(os.getpid())

    def _address(self):
        if not self.manager.snapshot().get("running"):
            raise RuntimeError("Start the hotspot before sharing files.")
        if isinstance(self.manager, DemoManager):
            return ipaddress.IPv4Interface("192.168.12.1/24")
        if not self.manager._runtime():
            raise RuntimeError("File sharing requires this app's running hotspot on ap0.")
        try:
            entries = json.loads(self.manager._run(["ip", "-j", "-4", "address", "show", "dev", "ap0"], timeout=4))
            if not isinstance(entries, list) or len(entries) != 1 or entries[0].get("ifname") != "ap0":
                raise ValueError("Unexpected hotspot address response")
            addresses = []
            for record in entries[0].get("addr_info", []):
                if record.get("family") != "inet" or record.get("scope") != "global":
                    continue
                prefix = record.get("prefixlen")
                if type(prefix) is not int or not 1 <= prefix <= 32 or not isinstance(record.get("local"), str):
                    raise ValueError("Invalid hotspot IPv4 address")
                address = ipaddress.IPv4Interface(f"{record['local']}/{prefix}")
                if (address.ip.is_unspecified or address.ip.is_multicast or address.ip.is_loopback
                        or address.ip.is_link_local or address.ip.is_reserved
                        or (prefix < 31 and address.ip in (address.network.network_address, address.network.broadcast_address))):
                    raise ValueError("Hotspot needs a local unicast IPv4 address")
                addresses.append(address)
            if len(addresses) != 1:
                raise ValueError("Hotspot needs one IPv4 address")
            return addresses[0]
        except (ValueError, TypeError, AttributeError) as exc:
            raise RuntimeError("The hotspot's IPv4 address is not ready. Refresh and try again.") from exc

    def prepare(self, port):
        address = self._address()
        result = {"address": str(address.ip), "network": str(address.network), "port": port}
        if isinstance(self.manager, DemoManager):
            self.rules[port] = ()
            return result
        spec = ("-i", "ap0", "-s", str(address.network), "-d", str(address.ip),
                "-p", "tcp", "--dport", str(port), "-m", "comment", "--comment", self.comment, "-j", "ACCEPT")
        if self.rules.get(port) == spec:
            return result
        if port in self.rules:
            self.close(port)
        if len(self.rules) >= 8:
            raise RuntimeError("Close an existing file share before opening another.")
        self.manager._run(["iptables", "-w", "3", "-I", "INPUT", "1", *spec], timeout=5)
        self.rules[port] = spec
        return result

    def close(self, port):
        spec = self.rules.get(port)
        if spec is None:
            return
        if not isinstance(self.manager, DemoManager):
            # Delete only the exact rule inserted by this helper. A failed
            # deletion remains tracked for the subsequent stop/EOF retries.
            self.manager._run(["iptables", "-w", "3", "-D", "INPUT", *spec], timeout=5)
        del self.rules[port]

    def close_all(self):
        errors = []
        for port in list(self.rules):
            try:
                self.close(port)
            except Exception as exc:
                errors.append(str(exc))
        return errors


def _share_firewall(manager):
    firewall = getattr(manager, "_gui_share_firewall", None)
    if firewall is None:
        firewall = _ShareFirewall(manager)
        manager._gui_share_firewall = firewall
    return firewall


def _manager_state(manager):
    snapshot = manager.snapshot()
    if isinstance(manager, DemoManager):
        metrics = {"signal_percent": 88, "signal_dbm": -56,
                   "rx_link_mbps": 144.4, "tx_link_mbps": 144.4,
                   "upstream_ssid": "Home Wi-Fi", "upstream_bssid": "02:11:22:33:44:55",
                   "upstream_frequency_mhz": 2442,
                   "download_bps": 1_240_000.0 if snapshot.get("running") else 0.0,
                   "upload_bps": 186_000.0 if snapshot.get("running") else 0.0,
                   "download_total_bytes": 86_000_000, "upload_total_bytes": 19_000_000,
                   "traffic_available": bool(snapshot.get("running")),
                   "upstream_download_bps": 1_680_000.0,
                   "upstream_upload_bps": 240_000.0,
                   "upstream_download_total_bytes": 324_000_000,
                   "upstream_upload_total_bytes": 45_000_000,
                   "upstream_traffic_available": True,
                   "link_utilization_percent": 10.6}
    else:
        sampler = getattr(manager, "_gui_metrics", None)
        if sampler is None:
            sampler = NetworkMetrics()
            manager._gui_metrics = sampler
        metrics = sampler.sample(manager)
        snapshot["clients"] = sampler.enrich_clients(snapshot.get("clients", []),
                                                     running=bool(snapshot.get("running")),
                                                     sampled_at=snapshot.get("clients_sampled_at"))
    snapshot["metrics"] = metrics
    return {"snapshot": snapshot, "metrics": metrics, "settings": dict(manager.settings),
            "options": dict(getattr(manager, "options", {"hidden": False, "isolate_clients": False})),
            "interfaces": manager.interfaces()}


def dispatch_request(manager, action, params):
    """Validate a narrow RPC command, apply it, and return the refreshed state."""
    if not isinstance(action, str) or action not in RPC_ACTIONS:
        raise ValueError("Unsupported hotspot operation.")
    if not isinstance(params, dict):
        raise ValueError("Operation parameters must be an object.")
    if action in ("share_prepare", "share_close"):
        port = _share_port(params)
        firewall = _share_firewall(manager)
        if action == "share_prepare":
            return firewall.prepare(port)
        firewall.close(port)
        return {"port": port, "closed": True}
    elif action == "settings":
        required, optional = {"ssid", "password", "upstream"}, {"hidden", "isolate_clients"}
        if not required <= set(params) or set(params) - required - optional or any(
                not isinstance(params[key], str) for key in required):
            raise ValueError("Provide a network name, password, and Internet interface.")
        if any(len(params[key]) > 128 for key in required):
            raise ValueError("A settings value is too long.")
        options = {key: params[key] for key in optional if key in params}
        if any(type(value) is not bool for value in options.values()):
            raise ValueError("Hidden network and device isolation must be true or false.")
        values = validate_settings(**{key: params[key] for key in required})
        manager.save_settings(**values, **options)
    elif action in ("kick", "block", "unblock"):
        if set(params) != {"mac"} or not isinstance(params["mac"], str) or len(params["mac"]) > 32:
            raise ValueError("Select a valid device MAC address.")
        mac = validate_mac(params["mac"])
        if action == "kick":
            manager.kick(mac)
        elif action == "block":
            manager.block(mac)
        else:
            manager.unblock(mac)
    else:
        if params:
            raise ValueError("This operation does not accept parameters.")
        if action == "start":
            manager.start()
        elif action == "stop":
            firewall = _share_firewall(manager)
            firewall.close_all()
            try:
                manager.stop()
            finally:
                errors = firewall.close_all()
            if errors:
                raise RuntimeError("The hotspot stopped, but file-sharing access could not be fully closed. Close the app to retry cleanup.")
        elif action == "logs":
            return manager.log_tail()
    return _manager_state(manager)


def _write_rpc_reply(reply):
    try:
        sys.stdout.write(json.dumps(reply, ensure_ascii=False, separators=(",", ":")) + "\n")
        sys.stdout.flush()
        return True
    except (BrokenPipeError, OSError):
        return False


def run_backend_service():
    """Run only the privileged backend; EOF releases its lock and keeps the AP on."""
    if os.geteuid() != 0:
        _write_rpc_reply({"ok": False, "error": "Administrator access is required for hotspot control."})
        return 2
    os.umask(0o077)
    lock_handle = None
    try:
        descriptor = os.open(RPC_LOCK_PATH, os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        lock_handle = os.fdopen(descriptor, "w")
        details = os.fstat(lock_handle.fileno())
        if not stat.S_ISREG(details.st_mode) or details.st_uid != 0:
            raise RuntimeError("The hotspot manager lock is not a trusted root-owned file.")
        try:
            fcntl.flock(lock_handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError("Z Hotspot is already open. Close its other window or terminal first.") from exc
        manager = HotspotManager()
    except Exception as exc:
        _write_rpc_reply({"ok": False, "error": _rpc_error_text(exc)})
        if lock_handle:
            lock_handle.close()
        return 1
    try:
        while True:
            raw = sys.stdin.buffer.readline(RPC_REQUEST_LIMIT + 1)
            if not raw:
                return 0
            if len(raw) > RPC_REQUEST_LIMIT or not raw.endswith(b"\n"):
                _write_rpc_reply({"ok": False, "error": "The helper received an oversized or incomplete request."})
                return 2
            request_id = None
            request = None
            incoming_password = None
            previous_password = manager.settings.get("password", "")
            try:
                request = json.loads(raw)
                if not isinstance(request, dict) or set(request) != {"id", "action", "params"}:
                    raise ValueError("The helper received an invalid request.")
                request_id = request["id"]
                if type(request_id) is not int or not 0 < request_id < 2 ** 53:
                    request_id = None
                    raise ValueError("The request identifier is invalid.")
                if isinstance(request["params"], dict):
                    candidate = request["params"].get("password")
                    incoming_password = candidate if isinstance(candidate, str) else None
                result = dispatch_request(manager, request["action"], request["params"])
                reply = {"id": request_id, "ok": True, "result": result}
            except Exception as exc:
                reply = {"id": request_id, "ok": False,
                         "error": _rpc_error_text(exc, previous_password, incoming_password,
                                                  manager.settings.get("password", ""))}
            if not _write_rpc_reply(reply):
                return 0
    finally:
        _share_firewall(manager).close_all()
        lock_handle.close()


class BackendConnectionError(RuntimeError):
    """The privileged helper is unavailable or its private transport ended."""


class BackendBridge:
    """Serialize backend work off the GTK thread and deliver callbacks via GLib."""

    def __init__(self, demo=False):
        self.demo = bool(demo)
        self.connected = False
        self.on_state = None
        self._manager = None
        self._process = None
        self._stdout_buffer = bytearray()
        self._request_number = 0
        self._secrets = set()
        self._queue = queue.Queue()
        self._closing = False
        self._worker = threading.Thread(target=self._work, name="hotspot-backend", daemon=True)
        self._worker.start()

    def connect(self, callback):
        if not self._closing:
            self._queue.put(("connect", None, None, callback))

    def request(self, action, params, callback):
        if not self._closing:
            self._queue.put(("request", action, params, callback))

    def close(self):
        if not self._closing:
            self._closing = True
            self.connected = False
            self._queue.put(("close", None, None, None))

    def _deliver(self, callback, result, error):
        if self._closing:
            return
        def deliver_once():
            if not self._closing:
                try:
                    if isinstance(result, dict) and isinstance(result.get("snapshot"), dict) and self.on_state is not None:
                        self.on_state(result)
                finally:
                    if callback is not None:
                        callback(result, error)
            return False
        GLib.idle_add(deliver_once)

    def _remember_secrets(self, result):
        if isinstance(result, dict) and isinstance(result.get("settings"), dict):
            password = result["settings"].get("password")
            if isinstance(password, str) and password:
                self._secrets.add(password)

    @staticmethod
    def _discard_stderr(stream):
        """Drain this separate pipe without exposing auth output or credentials."""
        try:
            while stream.read(4096):
                pass
        except (OSError, ValueError):
            pass

    def _open(self):
        if self.connected:
            return dispatch_request(self._manager, "boot", {}) if self.demo else self._rpc("boot", {})
        if self.demo:
            self._manager = DemoManager()
            result = dispatch_request(self._manager, "boot", {})
        else:
            self._disconnect()
            helper = str(Path(__file__).resolve())
            try:
                self._process = subprocess.Popen(
                    ["/usr/bin/pkexec", "/usr/bin/python3", helper, "--backend"],
                    stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                    bufsize=0, start_new_session=True,
                    env={**os.environ, "PATH": "/usr/sbin:/usr/bin:/sbin:/bin"})
            except FileNotFoundError as exc:
                raise BackendConnectionError("The pkexec authorization tool is missing. Install pkexec, then reconnect.") from exc
            except OSError as exc:
                raise BackendConnectionError("The administrator helper could not start. Try reopening the app.") from exc
            threading.Thread(target=self._discard_stderr, args=(self._process.stderr,),
                             name="hotspot-helper-stderr", daemon=True).start()
            result = self._rpc("boot", {}, timeout=120)
        self.connected = not self._closing
        return result

    def _transport_ended(self):
        code = self._process.poll() if self._process else None
        if code == 126:
            return BackendConnectionError("Administrator access was cancelled or denied. Connect again to retry.")
        return BackendConnectionError("The administrator helper closed. Connect again to retry.")

    def _read_reply(self, timeout):
        deadline = time.monotonic() + timeout
        while True:
            newline = self._stdout_buffer.find(b"\n")
            if newline >= 0:
                raw = bytes(self._stdout_buffer[:newline])
                del self._stdout_buffer[:newline + 1]
                try:
                    reply = json.loads(raw)
                except (ValueError, UnicodeError) as exc:
                    raise BackendConnectionError("The administrator helper returned an unreadable reply. Reconnect and try again.") from exc
                if not isinstance(reply, dict) or type(reply.get("ok")) is not bool:
                    raise BackendConnectionError("The administrator helper returned an invalid reply. Reconnect and try again.")
                return reply
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise BackendConnectionError("The administrator helper did not respond in time. Reconnect and try again.")
            if self._process is None or self._process.stdout is None:
                raise self._transport_ended()
            try:
                readable, _, _ = select.select([self._process.stdout], [], [], min(remaining, 0.25))
                if not readable:
                    continue
                chunk = os.read(self._process.stdout.fileno(), 16384)
            except (OSError, ValueError) as exc:
                raise BackendConnectionError("The connection to the administrator helper was lost. Reconnect to retry.") from exc
            if not chunk:
                raise self._transport_ended()
            self._stdout_buffer.extend(chunk)
            if len(self._stdout_buffer) > RPC_RESPONSE_LIMIT:
                raise BackendConnectionError("The administrator helper returned too much data. Reconnect to retry.")

    def _rpc(self, action, params, timeout=180):
        if self._process is None or self._process.stdin is None:
            raise BackendConnectionError("Connect to the hotspot controller first.")
        self._request_number += 1
        request_id = self._request_number
        try:
            raw = (json.dumps({"id": request_id, "action": action, "params": params},
                              ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
        except (TypeError, ValueError, UnicodeError) as exc:
            raise ValueError("The operation contains invalid parameters.") from exc
        if len(raw) > RPC_REQUEST_LIMIT:
            raise ValueError("The operation parameters are too long.")
        try:
            self._process.stdin.write(raw)
            self._process.stdin.flush()
        except (BrokenPipeError, OSError, ValueError) as exc:
            raise self._transport_ended() from exc
        reply = self._read_reply(timeout)
        if reply.get("id") not in (request_id, None):
            raise BackendConnectionError("The administrator helper returned an unexpected reply. Reconnect to retry.")
        if not reply["ok"]:
            error = str(reply.get("error") or "The hotspot operation failed.")
            if reply.get("id") is None:
                raise BackendConnectionError(error)
            raise RuntimeError(error)
        if reply.get("id") != request_id or "result" not in reply:
            raise BackendConnectionError("The administrator helper returned an incomplete reply. Reconnect to retry.")
        return reply["result"]

    def _disconnect(self):
        self.connected = False
        if self.demo and self._manager is not None:
            _share_firewall(self._manager).close_all()
        process, self._process = self._process, None
        self._stdout_buffer.clear()
        if process is None:
            return
        try:
            if process.stdin:
                process.stdin.close()
        except (OSError, ValueError):
            pass
        # Closing stdin lets the root service finish its current action and exit.
        # Its detached create_ap process intentionally stays alive.
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            try:
                process.terminate()
            except (OSError, PermissionError):
                pass
        for stream in (process.stdout, process.stderr):
            try:
                if stream:
                    stream.close()
            except (OSError, ValueError):
                pass

    def _work(self):
        try:
            while True:
                kind, action, params, callback = self._queue.get()
                if kind == "close":
                    return
                if self._closing:
                    continue
                incoming_password = params.get("password") if isinstance(params, dict) else None
                if isinstance(incoming_password, str) and incoming_password:
                    self._secrets.add(incoming_password)
                try:
                    if kind == "connect":
                        result = self._open()
                    else:
                        if not self.connected:
                            raise BackendConnectionError("Connect to the hotspot controller first.")
                        result = dispatch_request(self._manager, action, params) if self.demo else self._rpc(action, params)
                    self._remember_secrets(result)
                    self._deliver(callback, result, None)
                except Exception as exc:
                    if isinstance(exc, BackendConnectionError):
                        self._disconnect()
                    self._deliver(callback, None, _rpc_error_text(exc, *self._secrets))
        finally:
            self._disconnect()

from hotspot_share import FileShareServer


class FileSharingControls:
    """Large-view-only controls; HTTP runs as the desktop user, never root."""

    def _build_file_sharing(self, page):
        self._share_files = []
        self._share_upload_directory = None
        self._share_server = None
        self._share_demo_active = False
        self._share_busy = False
        self._share_generation = 0
        self._share_port = 8765
        card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        self.share_card = card
        card.get_style_context().add_class("card")
        inner = _gui_pad(Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12), 18)
        header = Gtk.Box(spacing=12)
        header.pack_start(_gui_label("Share files over your hotspot", "section-title"), True, True, 0)
        self.share_badge = _gui_label("OFF", "badge offline")
        header.pack_end(self.share_badge, False, False, 0)
        inner.pack_start(header, False, False, 0)
        info = _gui_label("1. Join this hotspot on your phone.   2. Scan the transfer QR.   3. Tap a file to download.", "muted tiny")
        info.set_line_wrap(True)
        inner.pack_start(info, False, False, 0)
        steps = Gtk.Box(spacing=10)
        join = _gui_button("1. Join Wi-Fi QR", "network-wireless-symbolic", "flat-button")
        self.share_join_button = join
        join.connect("clicked", self._share_join_wifi)
        steps.pack_start(join, False, False, 0)
        compatibility = _gui_label("iPhone / iPad · Samsung / Android · another laptop", "muted tiny")
        compatibility.set_line_wrap(True)
        steps.pack_start(compatibility, True, True, 0)
        inner.pack_start(steps, False, False, 0)
        selection = Gtk.Box(spacing=10)
        self.share_choose_button = _gui_button("Choose files", "document-open-symbolic")
        self.share_choose_button.connect("clicked", self._share_choose_files)
        self.share_selection_label = _gui_label("No files selected", "muted tiny")
        self.share_selection_label.set_ellipsize(Pango.EllipsizeMode.MIDDLE)
        selection.pack_start(self.share_choose_button, False, False, 0)
        selection.pack_start(self.share_selection_label, True, True, 0)
        inner.pack_start(selection, False, False, 0)
        receiving = Gtk.Box(spacing=10)
        self.share_receive_toggle = Gtk.CheckButton(label="Also receive files")
        self.share_receive_toggle.connect("toggled", lambda *_: self._update_file_sharing())
        self.share_folder_button = _gui_button("Choose receive folder", "folder-open-symbolic", "flat-button")
        self.share_folder_button.connect("clicked", self._share_choose_folder)
        receiving.pack_start(self.share_receive_toggle, False, False, 0)
        receiving.pack_start(self.share_folder_button, False, False, 0)
        inner.pack_start(receiving, False, False, 0)
        self.share_folder_label = _gui_label("Receiving is off.", "muted tiny")
        self.share_folder_label.set_ellipsize(Pango.EllipsizeMode.MIDDLE)
        inner.pack_start(self.share_folder_label, False, False, 0)
        link_row = Gtk.Box(spacing=9)
        self.share_link_entry = Gtk.Entry()
        self.share_link_entry.set_editable(False)
        self.share_link_entry.set_hexpand(True)
        self.share_link_entry.set_placeholder_text("Your private hotspot link appears here")
        self.share_copy_button = _gui_button("Copy link", "edit-copy-symbolic")
        self.share_copy_button.connect("clicked", self._share_copy_link)
        self.share_qr_button = _gui_button("2. Transfer QR", "view-grid-symbolic", "primary")
        self.share_qr_button.connect("clicked", self._share_qr_code)
        link_row.pack_start(self.share_link_entry, True, True, 0)
        link_row.pack_start(self.share_qr_button, False, False, 0)
        link_row.pack_start(self.share_copy_button, False, False, 0)
        inner.pack_start(link_row, False, False, 0)
        iphone = _gui_label("iPhone: scan with Camera, open in Safari, tap a file and confirm Download. Find it in Files → Downloads. Photos can then be saved to Photos from the Share menu.", "muted tiny")
        iphone.set_line_wrap(True)
        inner.pack_start(iphone, False, False, 0)
        self.share_activity_label = _gui_label("Transfer activity appears here when a device downloads or sends a file.", "muted tiny")
        self.share_activity_label.set_line_wrap(True)
        self.share_activity_label.set_selectable(True)
        inner.pack_start(self.share_activity_label, False, False, 0)
        privacy = _gui_label("Files transfer through the browser over this Wi-Fi. The receiving person chooses what to download. Keep the app and phone screen open while transferring.", "muted tiny")
        privacy.set_line_wrap(True)
        inner.pack_start(privacy, False, False, 0)
        actions = Gtk.Box(spacing=9)
        self.share_status_label = _gui_label("Background keeps sharing active. Quit stops sharing; the hotspot stays on.", "muted tiny")
        self.share_status_label.set_line_wrap(True)
        actions.pack_start(self.share_status_label, True, True, 0)
        self.share_start_button = _gui_button("Start sharing", "folder-publicshare-symbolic", "primary")
        self.share_stop_button = _gui_button("Stop sharing", "media-playback-stop-symbolic")
        self.share_start_button.connect("clicked", self._start_file_share)
        self.share_stop_button.connect("clicked", lambda *_: self._stop_file_share())
        actions.pack_end(self.share_start_button, False, False, 0)
        actions.pack_end(self.share_stop_button, False, False, 0)
        inner.pack_start(actions, False, False, 0)
        card.pack_start(inner, True, True, 0)
        page.pack_start(card, False, False, 0)
        self._update_file_sharing()

    def _focus_file_sharing(self, *_):
        if hasattr(self, "page_stack"):
            self.page_stack.set_visible_child_name("files")
            return
        adjustment = self.page_scroll.get_vadjustment()
        target = max(adjustment.get_lower(), self.share_card.get_allocation().y - 12)
        adjustment.set_value(min(target, max(adjustment.get_lower(), adjustment.get_upper() - adjustment.get_page_size())))

    def _share_active(self):
        return self._share_server is not None or self._share_demo_active

    def _update_file_sharing(self):
        if not hasattr(self, "share_start_button"):
            return
        connected = bool(getattr(self.manager, "connected", False))
        running = bool(self._snapshot.get("running"))
        if self._share_active() and not running and not self._closing:
            self._stop_file_share()
        active = self._share_active()
        ready = connected and running and not self._busy and not self._share_busy
        if hasattr(self, "share_join_button"):
            self.share_join_button.set_sensitive(ready)
        editable = not active and not self._share_busy
        receiving = self.share_receive_toggle.get_active()
        chosen = bool(self._share_files) or (receiving and self._share_upload_directory is not None)
        self.share_start_button.set_sensitive(ready and not active and chosen)
        self.share_stop_button.set_sensitive(active or self._share_busy)
        self.share_choose_button.set_sensitive(editable)
        self.share_receive_toggle.set_sensitive(editable)
        self.share_folder_button.set_sensitive(editable and receiving)
        self.share_copy_button.set_sensitive(active)
        if hasattr(self, "share_qr_button"):
            self.share_qr_button.set_sensitive(active)
        self.share_badge.set_text("SHARING" if active else "STARTING" if self._share_busy else "OFF")
        self.share_badge.get_style_context().remove_class("online")
        if active:
            self.share_badge.get_style_context().add_class("online")
        count = len(self._share_files)
        self.share_selection_label.set_text(f"{count} file{'s' if count != 1 else ''} selected" if count else "No files selected")
        self.share_selection_label.set_tooltip_text("\n".join(str(path) for path in self._share_files))
        folder = self._share_upload_directory
        self.share_folder_label.set_text(f"Receive into: {folder}" if receiving and folder else "Choose a folder to receive files." if receiving else "Receiving is off.")
        if active and self._share_server:
            stats = self._share_server.stats()
            def size(value):
                number = float(value)
                for unit in ("B", "KB", "MB", "GB"):
                    if number < 1024 or unit == "GB":
                        return f"{number:.1f} {unit}" if unit != "B" else f"{number:.0f} B"
                    number /= 1024
            self.share_status_label.set_text(f"{stats.get('downloads', 0)} sent · {stats.get('uploads', 0)} received · {size(stats.get('bytes_sent', 0))} out / {size(stats.get('bytes_received', 0))} in")
            if hasattr(self, "share_activity_label"):
                events = stats.get("recent_transfers", [])[:4]
                rows = []
                for event in events:
                    arrow = "To" if event["direction"] == "download" else "From"
                    rows.append(f"{arrow} {event.get('browser_hint', 'Browser')} ({event.get('client_address', '?')}) · {event.get('status', '')}\n{event.get('name', '')} · {size(event.get('transferred_bytes', 0))} / {size(event.get('total_bytes', 0))}")
                text = "\n\n".join(rows) if rows else "Waiting for a connected device to open the transfer link."
                self.share_activity_label.set_text(text)
                self.share_activity_label.set_tooltip_text("Browser and platform names are reported by the browser; they do not verify device identity. Sent means the server completed sending; check the receiving device's Downloads to confirm it was saved.")

    def _share_join_wifi(self, *_):
        action = getattr(self, "_show_join_qr", None)
        if action:
            action()
        else:
            self._toast("Join this hotspot from your phone's Wi-Fi settings, then scan the transfer QR.")

    def _share_qr_code(self, *_):
        link = self.share_link_entry.get_text()
        if not self._share_active() or not link:
            self._toast("Start sharing first to create the transfer QR.", error=True)
            return
        try:
            from hotspot_qr import show_qr_dialog
            show_qr_dialog(self, "Open file transfer", link,
                           "First connect your phone to this hotspot. Scan with Camera, open in Safari / your browser, then choose a file.")
        except Exception as exc:
            self._toast(f"Could not show transfer QR: {exc}", error=True)

    def _share_choose_files(self, *_):
        dialog = Gtk.FileChooserDialog(title="Choose files to share", transient_for=self, action=Gtk.FileChooserAction.OPEN)
        dialog.add_buttons("Cancel", Gtk.ResponseType.CANCEL, "Share selected files", Gtk.ResponseType.OK)
        dialog.set_select_multiple(True)
        if dialog.run() == Gtk.ResponseType.OK:
            self._share_files = [Path(value) for value in dialog.get_filenames()]
        dialog.destroy()
        self._update_file_sharing()

    def _share_choose_folder(self, *_):
        dialog = Gtk.FileChooserDialog(title="Choose where received files are saved", transient_for=self, action=Gtk.FileChooserAction.SELECT_FOLDER)
        dialog.add_buttons("Cancel", Gtk.ResponseType.CANCEL, "Use this folder", Gtk.ResponseType.OK)
        if dialog.run() == Gtk.ResponseType.OK:
            self._share_upload_directory = Path(dialog.get_filename())
        dialog.destroy()
        self._update_file_sharing()

    def _share_copy_link(self, *_):
        Gtk.Clipboard.get(Gdk.SELECTION_CLIPBOARD).set_text(self.share_link_entry.get_text(), -1)
        self._toast("File-sharing link copied. Open it on a device connected to this hotspot.")

    def _start_file_share(self, *_):
        if self._share_busy or self._share_active() or not self._snapshot.get("running"):
            return
        if not self._share_files and not (self.share_receive_toggle.get_active() and self._share_upload_directory):
            self._toast("Choose files or a receive folder first.", error=True)
            return
        self._share_generation += 1
        generation = self._share_generation
        self._share_busy = True
        self._update_file_sharing()
        self._toast("Preparing private file sharing on this hotspot…")
        def prepared(context, error):
            if generation != self._share_generation or self._closing:
                # Stop already queued cleanup (or app shutdown closes the helper).
                # Closing here could remove the rule belonging to a newer start.
                return
            self._share_busy = False
            if error:
                self._toast(error, error=True)
            else:
                try:
                    if self.demo:
                        self._share_demo_active = True
                        self.share_link_entry.set_text("http://192.168.12.1:8765/demo-preview/")
                        self.share_status_label.set_text("Demo preview only. No files or network ports are exposed.")
                    else:
                        receiver = self._share_upload_directory if self.share_receive_toggle.get_active() else None
                        server = FileShareServer(self._share_files, context["address"], context["network"], port=context["port"], upload_directory=receiver)
                        server.start()
                        self._share_server = server
                        self.share_link_entry.set_text(server.url)
                    self._toast("Sharing is ready. Scan Transfer QR on a device connected to this hotspot.")
                except Exception as exc:
                    self.manager.request("share_close", {"port": self._share_port}, lambda *_: None)
                    self._toast(f"Could not start file sharing: {exc}", error=True)
            self._update_file_sharing()
        self.manager.request("share_prepare", {"port": self._share_port}, prepared)

    def _stop_file_share(self, close_firewall=True):
        if not hasattr(self, "_share_generation"):
            return
        was_active = self._share_active() or self._share_busy
        self._share_generation += 1
        self._share_busy = False
        server, self._share_server = self._share_server, None
        self._share_demo_active = False
        if server:
            server.stop()
        if close_firewall and was_active and getattr(self.manager, "connected", False):
            self.manager.request("share_close", {"port": self._share_port}, lambda result, error: self._toast(error, error=True) if error and not self._closing else None)
        self.share_link_entry.set_text("")
        self.share_status_label.set_text("Sharing stops when you close the app. The hotspot can stay on.")
        if hasattr(self, "share_activity_label"):
            self.share_activity_label.set_text("Transfer activity appears here when a device downloads or sends a file.")
        if not self._closing:
            self._update_file_sharing()

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
from gi.repository import Gtk, Gdk, GLib, Pango, Gio, GdkPixbuf
import datetime
import math
import secrets
from hotspot_qr import wifi_payload, show_qr_dialog


_GUI_CSS = b"""
window.hotspot-window { background: #0d1420; color: #e7edf6; font-family: Sans; font-size: 10pt; }
.hotspot-window label { color: #dbe5f1; }
.hotspot-window .muted { color: #8493a9; }
.hotspot-window .tiny { font-size: 8.5pt; }
.hotspot-window .brand { font-size: 15pt; font-weight: 800; letter-spacing: 0.7px; }
.hotspot-window .section-title { font-size: 12pt; font-weight: 700; }
.hotspot-window .section-kicker { color: #91a2b8; font-size: 8pt; font-weight: 700; letter-spacing: 1px; }
.hotspot-window .hero-title { color: #f4f8ff; font-size: 24pt; font-weight: 800; }
.hotspot-window .hero { background: linear-gradient(120deg, #142d3c, #172333 65%, #14202f); border: 1px solid #284354; border-radius: 16px; }
.hotspot-window .card { background: #162130; border: 1px solid #263449; border-radius: 14px; }
.hotspot-window .inner-rule { background: #263449; min-height: 1px; }
.hotspot-window .stat-number { color: #e9f2fb; font-size: 16pt; font-weight: 700; }
.hotspot-window .brand-mark { background: #123d42; color: #64e2ce; border-radius: 12px; padding: 11px; }
.hotspot-window .hero-mark { background: #18424a; color: #67e6d3; border: 1px solid #2a5d62; border-radius: 18px; padding: 17px; }
.hotspot-window .badge { border-radius: 12px; background: #23364b; color: #a2b9d2; padding: 5px 10px; font-size: 8pt; font-weight: 800; }
.hotspot-window .online { background: #17463e; color: #7de6c7; }
.hotspot-window .offline { background: #2a3545; color: #b4c0d0; }
.hotspot-window .warning { background: #4b3a23; color: #f5cf8c; }
.hotspot-window .demo { background: #39304d; color: #ccafed; }
.hotspot-window button { background-image: none; background: #223249; border: 1px solid #344761; border-radius: 8px; color: #dce7f5; padding: 8px 13px; box-shadow: none; text-shadow: none; }
.hotspot-window button:hover { background: #2c405b; border-color: #45627f; }
.hotspot-window button:active { background: #1a2a40; }
.hotspot-window button:disabled { color: #5c6b80; background: #1a2738; border-color: #29374b; }
.hotspot-window button.primary { background: #62d9c6; border-color: #62d9c6; color: #0d2929; font-weight: 800; }
.hotspot-window button.primary:hover { background: #83e6d6; border-color: #83e6d6; }
.hotspot-window button.primary:disabled { background: #27514f; border-color: #315b58; color: #7b9f9d; }
.hotspot-window button.danger { color: #f0aeb4; border-color: #65404b; background: #332531; }
.hotspot-window button.danger:hover { background: #4a2d3c; }
.hotspot-window button.flat-button { background: transparent; border-color: transparent; color: #9aafc8; }
.hotspot-window button.flat-button:hover { background: #23344a; color: #e4effc; }
.hotspot-window entry, .hotspot-window combobox button { background: #101b29; border: 1px solid #34445c; border-radius: 8px; color: #e1edf9; padding: 9px 10px; box-shadow: none; }
.hotspot-window entry:focus { border-color: #62d9c6; }
.hotspot-window entry:disabled { color: #687c94; }
.hotspot-window checkbutton { color: #9caec4; }
.hotspot-window checkbutton check { background: #142235; border: 1px solid #4b627d; border-radius: 4px; }
.hotspot-window checkbutton check:checked { background: #62d9c6; color: #0d2929; border-color: #62d9c6; }
.hotspot-window treeview { background: #162130; color: #dae6f4; border: none; font-size: 9.5pt; }
.hotspot-window treeview:selected { background: #254553; color: #bcf4eb; }
.hotspot-window treeview header button { background: #1b293b; color: #8fa2bc; border: none; border-radius: 0; padding: 10px 9px; font-size: 8.5pt; font-weight: 700; }
.hotspot-window treeview.view { -GtkTreeView-vertical-separator: 13; -GtkTreeView-horizontal-separator: 16; }
.hotspot-window scrollbar { background: transparent; }
.hotspot-window scrollbar slider { background: #34465e; border-radius: 5px; min-width: 7px; min-height: 20px; }
.hotspot-window .notice { background: #302d25; border: 1px solid #5c5135; border-radius: 9px; padding: 10px 12px; color: #dcc493; }
.hotspot-window .unlock-card { background: #1e293b; border: 1px solid #354963; border-radius: 10px; }
.hotspot-window .footer { border-top: 1px solid #253247; padding: 8px 22px; background: #101927; }
.hotspot-window .toast-success { color: #8bdfca; }
.hotspot-window .toast-error { color: #f0b3b5; }
.hotspot-window .empty { color: #7e91a9; }
.hotspot-window textview, .hotspot-window textview text { color: #c2d7ea; background: #101b29; font-family: Monospace; font-size: 9pt; }
.hotspot-window dialog { background: #162130; }
.hotspot-window stacksidebar { background: #101b29; border-right: 1px solid #263449; }
.hotspot-window stacksidebar list { background: transparent; padding: 10px 8px; }
.hotspot-window stacksidebar row { border-radius: 9px; padding: 13px 8px; color: #93a7c0; }
.hotspot-window stacksidebar row:selected { background: #1d4249; color: #7ce9d7; }
.hotspot-window progressbar trough { background: #101b29; border-radius: 5px; min-height: 7px; }
.hotspot-window progressbar progress { background: #62d9c6; border-radius: 5px; }
"""


def _gui_label(text="", css=None, xalign=0):
    item = Gtk.Label(label=text, xalign=xalign)
    if css:
        for name in css.split():
            item.get_style_context().add_class(name)
    return item


def _gui_pad(widget, value=18):
    widget.set_margin_start(value)
    widget.set_margin_end(value)
    widget.set_margin_top(value)
    widget.set_margin_bottom(value)
    return widget


def _gui_icon(name, size=Gtk.IconSize.BUTTON):
    return Gtk.Image.new_from_icon_name(name, size)


def _gui_logo(size=48):
    filename = Path(__file__).resolve().with_name("icon.svg")
    try:
        return Gtk.Image.new_from_pixbuf(GdkPixbuf.Pixbuf.new_from_file_at_scale(str(filename), size, size, True))
    except GLib.Error:
        return _gui_icon("network-wireless-symbolic", Gtk.IconSize.LARGE_TOOLBAR)


def _gui_button(text, icon=None, css=None):
    button = Gtk.Button()
    row = Gtk.Box(spacing=7)
    row.set_halign(Gtk.Align.CENTER)
    if icon:
        row.pack_start(_gui_icon(icon), False, False, 0)
    row.pack_start(Gtk.Label(label=text), False, False, 0)
    button.add(row)
    button._caption = row.get_children()[-1]
    if css:
        for name in css.split():
            button.get_style_context().add_class(name)
    return button


def _gui_bytes(amount):
    try:
        size = float(amount or 0)
    except (ValueError, TypeError):
        size = 0
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024


def _gui_rate(value):
    try:
        number = float(value)
        return _gui_bytes(max(0, number)) + "/s" if math.isfinite(number) else "—"
    except (TypeError, ValueError):
        return "—"


def _gui_duration(value):
    try:
        seconds = max(0, int(value))
    except (TypeError, ValueError):
        return "—"
    hours, seconds = divmod(seconds, 3600)
    minutes, seconds = divmod(seconds, 60)
    return f"{hours}h {minutes}m" if hours else f"{minutes}m {seconds}s"


class HotspotWindow(Gtk.Window, FileSharingControls):
    """Native desktop front end. The supplied bridge owns all privileged work."""

    def __init__(self, manager, demo=False):
        super().__init__(title="Z Hotspot" + (" · Demo" if demo else ""))
        self.manager = manager
        self.demo = demo
        self._closing = False
        self._busy = False
        self._refresh_pending = False
        self._connecting = False
        self._loading_form = False
        self._dirty = False
        self._saved_settings = {}
        self._saved_options = {"hidden": False, "isolate_clients": False}
        self._metrics = {}
        self._refresh_seconds = 4
        self._interfaces = []
        self._snapshot = {"running": False, "clients": [], "blocked": [], "status": "Locked"}
        self._refresh_id = None
        self._blocked_dialog = None
        self._blocked_store = None
        monitor = Gdk.Display.get_default().get_primary_monitor()
        workarea = monitor.get_workarea() if monitor else None
        self.set_default_size(min(1180, workarea.width - 60) if workarea else 1180,
                              min(860, workarea.height - 70) if workarea else 860)
        self.set_size_request(960, 680)
        self.set_position(Gtk.WindowPosition.CENTER)
        self.set_icon_name("z-hotspot")
        icon_file = Path(__file__).resolve().with_name("icon.svg")
        if icon_file.is_file():
            try:
                self.set_icon_from_file(str(icon_file))
            except GLib.Error:
                pass
        self.set_wmclass("hotspot-manager", "Hotspot Manager")
        self.get_style_context().add_class("hotspot-window")
        provider = Gtk.CssProvider()
        provider.load_from_data(_GUI_CSS)
        Gtk.StyleContext.add_provider_for_screen(Gdk.Screen.get_default(), provider,
                                                 Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
        self._provider = provider
        self.connect("delete-event", self._on_close)
        self._build_ui()
        self._update_sensitivity()
        self.show_all()
        self.notice_label.hide()
        self.empty_label.show()
        self.spinner.hide()
        if demo:
            GLib.idle_add(self._unlock)

    def _build_ui(self):
        shell = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.add(shell)
        header = Gtk.Box(spacing=12)
        header.set_margin_start(26)
        header.set_margin_end(26)
        header.set_margin_top(20)
        header.set_margin_bottom(18)
        brand_icon = _gui_logo(48)
        header.pack_start(brand_icon, False, False, 0)
        brand_words = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        brand_words.pack_start(_gui_label("Z Hotspot", "brand"), False, False, 0)
        brand_words.pack_start(_gui_label("v0.1 · @zalem.j", "muted tiny"), False, False, 0)
        header.pack_start(brand_words, True, True, 0)
        if self.demo:
            demo_badge = _gui_label("DEMO PREVIEW", "badge demo")
            demo_badge.set_valign(Gtk.Align.CENTER)
            header.pack_start(demo_badge, False, False, 4)
        self.unlock_button = _gui_button("Unlock controls", "changes-allow-symbolic")
        self.unlock_button.set_valign(Gtk.Align.CENTER)
        self.unlock_button.connect("clicked", self._unlock)
        header.pack_end(self.unlock_button, False, False, 0)
        self.compact_button = _gui_button("Compact view", "view-restore-symbolic", "flat-button")
        self.compact_button.connect("clicked", lambda *_: self.on_open_compact() if callable(getattr(self, "on_open_compact", None)) else None)
        header.pack_end(self.compact_button, False, False, 0)
        self.background_button = _gui_button("Background", "go-bottom-symbolic", "flat-button")
        self.background_button.set_tooltip_text("Keep Z Hotspot and file sharing running in the system tray")
        self.background_button.connect("clicked", lambda *_: self.on_background() if callable(getattr(self, "on_background", None)) else None)
        header.pack_end(self.background_button, False, False, 0)
        self.files_button = _gui_button("Share files", "folder-publicshare-symbolic", "flat-button")
        self.files_button.connect("clicked", self._focus_file_sharing)
        header.pack_end(self.files_button, False, False, 0)
        shell.pack_start(header, False, False, 0)

        self.auth_card = Gtk.Box(spacing=12)
        self.auth_card.set_margin_start(26)
        self.auth_card.set_margin_end(26)
        self.auth_card.set_margin_bottom(12)
        self.auth_card.get_style_context().add_class("unlock-card")
        auth_content = _gui_pad(Gtk.Box(spacing=11), 13)
        auth_content.pack_start(_gui_icon("changes-prevent-symbolic"), False, False, 0)
        auth_words = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=3)
        auth_words.pack_start(_gui_label("Ready when you are", "section-title"), False, False, 0)
        auth_words.pack_start(_gui_label("Unlock with your Linux password to view and control your hotspot.", "muted tiny"), False, False, 0)
        auth_content.pack_start(auth_words, True, True, 0)
        self.auth_card.pack_start(auth_content, True, True, 0)
        shell.pack_start(self.auth_card, False, False, 0)

        body = Gtk.Box(spacing=0)
        self.page_stack = Gtk.Stack()
        self.page_stack.set_transition_type(Gtk.StackTransitionType.CROSSFADE)
        self.page_stack.set_transition_duration(140)
        self.page_stack.set_hexpand(True)
        self.page_stack.set_vexpand(True)
        sidebar = Gtk.StackSidebar()
        sidebar.set_stack(self.page_stack)
        sidebar.set_size_request(175, -1)
        body.pack_start(sidebar, False, False, 0)
        body.pack_start(self.page_stack, True, True, 0)
        shell.pack_start(body, True, True, 0)
        self.pages = {}
        for name, title, description in (
                ("overview", "Overview", "Your network at a glance"),
                ("devices", "Connected devices", "Live connections and access controls"),
                ("files", "Share files", "Send and receive over your local hotspot"),
                ("settings", "Network settings", "Connection, privacy and display preferences")):
            scroll = Gtk.ScrolledWindow()
            scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
            content = _gui_pad(Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=16), 22)
            content.pack_start(_gui_label(title, "section-title"), False, False, 0)
            content.pack_start(_gui_label(description, "muted tiny"), False, False, 0)
            scroll.add(content)
            self.page_stack.add_titled(scroll, name, title)
            self.pages[name] = content
        page = self.pages["overview"]

        hero = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        hero.get_style_context().add_class("hero")
        hero_top = _gui_pad(Gtk.Box(spacing=18), 22)
        self.hero_icon = _gui_icon("network-wireless-signal-excellent-symbolic", Gtk.IconSize.DIALOG)
        self.hero_icon.get_style_context().add_class("hero-mark")
        self.hero_icon.set_valign(Gtk.Align.CENTER)
        hero_top.pack_start(self.hero_icon, False, False, 0)
        hero_words = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=7)
        kicker_row = Gtk.Box(spacing=10)
        kicker_row.pack_start(_gui_label("YOUR PERSONAL NETWORK", "section-kicker"), False, False, 0)
        self.status_label = _gui_label("LOCKED", "badge offline")
        kicker_row.pack_start(self.status_label, False, False, 0)
        hero_words.pack_start(kicker_row, False, False, 0)
        self.network_title = _gui_label("Your hotspot", "hero-title")
        self.network_title.set_ellipsize(Pango.EllipsizeMode.END)
        hero_words.pack_start(self.network_title, False, False, 0)
        self.hero_subtitle = _gui_label("Unlock controls to read your network status.", "muted")
        hero_words.pack_start(self.hero_subtitle, False, False, 0)
        hero_top.pack_start(hero_words, True, True, 0)
        controls = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        controls.set_valign(Gtk.Align.CENTER)
        self.start_button = _gui_button("Start hotspot", "media-playback-start-symbolic", "primary")
        self.start_button.connect("clicked", lambda *_: self._perform("start", {}, "Starting your hotspot…", "Hotspot is online."))
        controls.pack_start(self.start_button, False, False, 0)
        control_row = Gtk.Box(spacing=6, homogeneous=False)
        self.stop_button = _gui_button("Stop", "media-playback-stop-symbolic")
        self.stop_button.connect("clicked", self._stop_clicked)
        self.refresh_button = _gui_button("Refresh", "view-refresh-symbolic", "flat-button")
        self.refresh_button.connect("clicked", lambda *_: self._refresh(manual=True))
        control_row.pack_start(self.stop_button, False, False, 0)
        control_row.pack_start(self.refresh_button, False, False, 0)
        controls.pack_start(control_row, False, False, 0)
        hero_top.pack_start(controls, False, False, 0)
        hero.pack_start(hero_top, False, False, 0)
        hero.pack_start(Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL), False, False, 0)
        stats = Gtk.Box(spacing=20, homogeneous=True)
        stats.set_margin_start(24)
        stats.set_margin_end(24)
        stats.set_margin_top(14)
        stats.set_margin_bottom(15)
        self.client_stat = self._stat(stats, "CONNECTED DEVICES", "—")
        self.source_stat = self._stat(stats, "INTERNET SOURCE", "—")
        self.channel_stat = self._stat(stats, "RADIO CHANNEL", "—")
        hero.pack_start(stats, False, False, 0)
        page.pack_start(hero, False, False, 0)

        self.notice_label = _gui_label("", "notice tiny")
        self.notice_label.set_line_wrap(True)
        self.notice_label.set_line_wrap_mode(Pango.WrapMode.WORD_CHAR)
        self.notice_label.set_max_width_chars(100)
        page.pack_start(self.notice_label, False, False, 0)

        traffic = Gtk.Grid(column_spacing=12, row_spacing=12)
        traffic.set_column_homogeneous(True)
        self.traffic_values = {}
        for index, (key, title, hint) in enumerate((
                ("upstream_download_bps", "INCOMING FROM INTERNET SOURCE", "All traffic received by the primary connection"),
                ("download_bps", "SENT TO HOTSPOT DEVICES", "Current download rate for connected devices"),
                ("upload_bps", "RECEIVED FROM HOTSPOT DEVICES", "Current upload rate from connected devices"),
                ("link_utilization_percent", "SOURCE WI-FI LINK USAGE", "Estimate against the negotiated Wi-Fi rate"))):
            card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
            card.get_style_context().add_class("card")
            inner = _gui_pad(Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8), 17)
            card.pack_start(inner, True, True, 0)
            inner.pack_start(_gui_label(title, "section-kicker"), False, False, 0)
            value = _gui_label("—", "stat-number")
            inner.pack_start(value, False, False, 0)
            note = _gui_label(hint, "muted tiny")
            note.set_line_wrap(True)
            note.set_max_width_chars(42)
            inner.pack_start(note, False, False, 0)
            self.traffic_values[key] = value
            traffic.attach(card, index % 2, index // 2, 1, 1)
        page.pack_start(traffic, False, False, 0)
        info_card, self.info_values = self._details_card("Connection details", (
            ("source_signal", "Source signal"), ("source_link", "Source RX / TX link"),
            ("source_upload", "Sent to Internet source"), ("ap_address", "Hotspot address"),
            ("uptime", "Hotspot uptime"), ("driver", "Wi-Fi driver"),
            ("source_frequency", "Source radio"), ("totals", "Hotspot sent / received"),
            ("source_network", "Primary Wi-Fi network"), ("source_address", "Primary IPv4 / gateway")))
        page.pack_start(info_card, False, False, 0)
        explanation = _gui_label("Live rates reflect current traffic, not a speed test. The primary connection also includes this computer’s traffic.", "muted tiny")
        explanation.set_line_wrap(True)
        page.pack_start(explanation, False, False, 0)

        page = self.pages["settings"]
        settings_card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14)
        settings_card.get_style_context().add_class("card")
        settings_inner = _gui_pad(Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14), 19)
        settings_header = Gtk.Box(spacing=12)
        settings_header.pack_start(_gui_label("Network settings", "section-title"), True, True, 0)
        self.edit_badge = _gui_label("SAVED", "badge offline tiny")
        settings_header.pack_start(self.edit_badge, False, False, 0)
        settings_inner.pack_start(settings_header, False, False, 0)
        fields = Gtk.Grid(column_spacing=14, row_spacing=7)
        fields.set_column_homogeneous(True)
        self.ssid_entry = Gtk.Entry()
        self.ssid_entry.set_placeholder_text("e.g. Z-Hotspot")
        self.ssid_entry.set_hexpand(True)
        self.ssid_entry.connect("changed", self._form_changed)
        self.password_entry = Gtk.Entry()
        self.password_entry.set_visibility(False)
        self.password_entry.set_input_purpose(Gtk.InputPurpose.PASSWORD)
        self.password_entry.set_placeholder_text("8–63 English characters")
        self.password_entry.set_hexpand(True)
        self.password_entry.connect("changed", self._form_changed)
        self.source_combo = Gtk.ComboBoxText()
        self.source_combo.set_hexpand(True)
        self.source_combo.connect("changed", self._form_changed)
        for column, (title, control) in enumerate((("Network name", self.ssid_entry), ("Password", self.password_entry), ("Internet interface", self.source_combo))):
            fields.attach(_gui_label(title, "muted tiny"), column, 0, 1, 1)
            fields.attach(control, column, 1, 1, 1)
        self.reveal_toggle = Gtk.CheckButton(label="Show password")
        self.reveal_toggle.get_style_context().add_class("tiny")
        self.reveal_toggle.connect("toggled", lambda item: self.password_entry.set_visibility(item.get_active()))
        fields.attach(self.reveal_toggle, 1, 2, 1, 1)
        settings_inner.pack_start(fields, False, False, 0)
        password_tools = Gtk.Box(spacing=8)
        self.generate_button = _gui_button("Generate password", "view-refresh-symbolic", "flat-button")
        self.generate_button.connect("clicked", lambda *_: self.password_entry.set_text(secrets.token_urlsafe(15)))
        self.join_qr_button = _gui_button("Join Wi-Fi QR", "view-grid-symbolic", "flat-button")
        self.join_qr_button.connect("clicked", self._show_join_qr)
        password_tools.pack_start(self.generate_button, False, False, 0)
        password_tools.pack_start(self.join_qr_button, False, False, 0)
        settings_inner.pack_start(password_tools, False, False, 0)
        self.hidden_toggle = Gtk.CheckButton(label="Hide network name")
        self.hidden_toggle.set_tooltip_text("Devices may need the Join Wi-Fi QR or the exact network name to connect.")
        self.isolation_toggle = Gtk.CheckButton(label="Isolate connected devices from each other")
        self.isolation_toggle.set_tooltip_text("Prevents direct Wi-Fi traffic between hotspot clients. They can still reach this computer’s file share.")
        for toggle in (self.hidden_toggle, self.isolation_toggle):
            toggle.connect("toggled", self._form_changed)
            settings_inner.pack_start(toggle, False, False, 0)
        settings_bottom = Gtk.Box(spacing=16)
        self.settings_note = _gui_label("Changes are saved securely. Applying while online restarts the hotspot.", "muted tiny")
        self.settings_note.set_line_wrap(True)
        self.settings_note.set_max_width_chars(72)
        settings_bottom.pack_start(self.settings_note, True, True, 0)
        self.apply_button = _gui_button("Save settings", "document-save-symbolic")
        self.apply_button.connect("clicked", self._apply_clicked)
        settings_bottom.pack_end(self.apply_button, False, False, 0)
        settings_inner.pack_start(settings_bottom, False, False, 0)
        settings_card.pack_start(settings_inner, True, True, 0)
        page.pack_start(settings_card, False, False, 0)

        preferences = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        preferences.get_style_context().add_class("card")
        preferences_inner = _gui_pad(Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12), 19)
        preferences.pack_start(preferences_inner, True, True, 0)
        preferences_inner.pack_start(_gui_label("Dashboard preferences", "section-title"), False, False, 0)
        refresh_row = Gtk.Box(spacing=12)
        refresh_row.pack_start(_gui_label("Full view refresh interval", "muted"), True, True, 0)
        self.refresh_combo = Gtk.ComboBoxText()
        for seconds in (2, 4, 8, 15):
            self.refresh_combo.append(str(seconds), f"Every {seconds} seconds")
        self.refresh_combo.set_active_id("4")
        self.refresh_combo.connect("changed", self._refresh_interval_changed)
        refresh_row.pack_end(self.refresh_combo, False, False, 0)
        preferences_inner.pack_start(refresh_row, False, False, 0)
        settings_hint = _gui_label("WPA2 security · 2.4 GHz · channel follows your source Wi-Fi automatically. Background keeps file sharing available. Quit stops file sharing and leaves the hotspot on.", "muted tiny")
        settings_hint.set_line_wrap(True)
        settings_hint.set_max_width_chars(85)
        preferences_inner.pack_start(settings_hint, False, False, 0)
        page.pack_start(preferences, False, False, 0)

        page = self.pages["devices"]
        devices_card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        devices_card.get_style_context().add_class("card")
        devices_header = Gtk.Box(spacing=10)
        devices_header.set_margin_start(19)
        devices_header.set_margin_end(19)
        devices_header.set_margin_top(16)
        devices_header.set_margin_bottom(13)
        devices_header.pack_start(_gui_label("Connected devices", "section-title"), False, False, 0)
        self.device_count = _gui_label("0", "badge offline")
        devices_header.pack_start(self.device_count, False, False, 0)
        live = _gui_label("●  LIVE CONNECTIONS", "muted tiny")
        devices_header.pack_end(live, False, False, 0)
        devices_card.pack_start(devices_header, False, False, 0)
        self.device_store = Gtk.ListStore(str, str, str, str, str, str, str)
        self.device_view = Gtk.TreeView(model=self.device_store)
        self.device_view.set_headers_visible(True)
        self.device_view.set_enable_search(True)
        self.device_view.set_search_column(0)
        self.device_view.get_selection().connect("changed", lambda *_: self._update_sensitivity())
        for number, (title, expand, minimum) in enumerate((("DEVICE", True, 130), ("IP ADDRESS", False, 115), ("MAC ADDRESS", False, 155), ("SIGNAL", False, 85), ("TOTAL", False, 90), ("↓ / ↑ NOW", False, 150), ("CONNECTED", False, 95))):
            renderer = Gtk.CellRendererText()
            renderer.set_property("ellipsize", Pango.EllipsizeMode.END)
            column = Gtk.TreeViewColumn(title, renderer, text=number)
            column.set_expand(expand)
            column.set_min_width(minimum)
            column.set_resizable(True)
            self.device_view.append_column(column)
        device_scroll = Gtk.ScrolledWindow()
        device_scroll.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        device_scroll.set_min_content_height(190)
        device_scroll.set_max_content_height(290)
        device_scroll.set_propagate_natural_height(True)
        device_scroll.add(self.device_view)
        overlay = Gtk.Overlay()
        overlay.add(device_scroll)
        self.empty_label = _gui_label("Unlock controls to see connected devices.", "empty")
        self.empty_label.set_halign(Gtk.Align.CENTER)
        self.empty_label.set_valign(Gtk.Align.CENTER)
        overlay.add_overlay(self.empty_label)
        devices_card.pack_start(overlay, True, True, 0)
        devices_actions = Gtk.Box(spacing=7)
        devices_actions.set_margin_start(16)
        devices_actions.set_margin_end(16)
        devices_actions.set_margin_top(11)
        devices_actions.set_margin_bottom(13)
        self.selection_label = _gui_label("Select a device to manage access", "muted tiny")
        self.selection_label.set_ellipsize(Pango.EllipsizeMode.END)
        devices_actions.pack_start(self.selection_label, True, True, 0)
        self.disconnect_button = _gui_button("Disconnect", "network-offline-symbolic")
        self.disconnect_button.set_tooltip_text("Disconnect once. The device can reconnect using the password.")
        self.disconnect_button.connect("clicked", self._disconnect_clicked)
        self.block_button = _gui_button("Block device", "action-unavailable-symbolic", "danger")
        self.block_button.set_tooltip_text("Disconnect and prevent this MAC address from rejoining until unblocked.")
        self.block_button.connect("clicked", self._block_clicked)
        devices_actions.pack_end(self.block_button, False, False, 0)
        devices_actions.pack_end(self.disconnect_button, False, False, 0)
        devices_card.pack_start(devices_actions, False, False, 0)
        page.pack_start(devices_card, True, True, 0)
        details, self.device_detail_values = self._details_card("Selected device", (
            ("name", "DHCP name"), ("mac", "Wi-Fi address"),
            ("ip", "IPv4 address"), ("signal", "Signal strength"),
            ("connected", "Connected for"), ("inactive", "Last activity"),
            ("download", "Downloaded / current rate"), ("upload", "Uploaded / current rate"),
            ("rx_link", "Device → hotspot link"), ("tx_link", "Hotspot → device link")))
        page.pack_start(details, False, False, 0)
        device_hint = _gui_label("Names come from DHCP. A private Wi-Fi address can change, so a device may reappear with a different address.", "muted tiny")
        device_hint.set_line_wrap(True)
        page.pack_start(device_hint, False, False, 0)

        self._build_file_sharing(self.pages["files"])
        bottom = Gtk.Box(spacing=8)
        self.blocked_button = _gui_button("Blocked devices (0)", "changes-prevent-symbolic", "flat-button")
        self.blocked_button.connect("clicked", self._show_blocked)
        self.logs_button = _gui_button("View activity log", "text-x-generic-symbolic", "flat-button")
        self.logs_button.connect("clicked", self._show_logs)
        bottom.pack_start(self.blocked_button, False, False, 0)
        page.pack_start(bottom, False, False, 0)
        self.pages["settings"].pack_start(self.logs_button, False, False, 0)
        self.logs_button.set_halign(Gtk.Align.START)
        footer = Gtk.Box(spacing=10)
        footer.get_style_context().add_class("footer")
        self.spinner = Gtk.Spinner()
        footer.pack_start(self.spinner, False, False, 0)
        self.footer_label = _gui_label("Unlock controls to begin.", "muted tiny")
        self.footer_label.set_ellipsize(Pango.EllipsizeMode.END)
        footer.pack_start(self.footer_label, True, True, 0)
        self.updated_label = _gui_label("", "muted tiny")
        footer.pack_end(self.updated_label, False, False, 0)
        shell.pack_end(footer, False, False, 0)

    def _stat(self, box, label, value):
        section = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        section.pack_start(_gui_label(label, "section-kicker"), False, False, 0)
        number = _gui_label(value, "stat-number")
        number.set_ellipsize(Pango.EllipsizeMode.END)
        section.pack_start(number, False, False, 0)
        box.pack_start(section, True, True, 0)
        return number

    def _details_card(self, title, fields):
        card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        card.get_style_context().add_class("card")
        inner = _gui_pad(Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=15), 19)
        card.pack_start(inner, True, True, 0)
        inner.pack_start(_gui_label(title, "section-title"), False, False, 0)
        grid = Gtk.Grid(column_spacing=28, row_spacing=14)
        grid.set_column_homogeneous(True)
        values = {}
        for index, (key, label) in enumerate(fields):
            cell = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
            cell.pack_start(_gui_label(label, "muted tiny"), False, False, 0)
            value = _gui_label("—")
            value.set_selectable(True)
            value.set_ellipsize(Pango.EllipsizeMode.END)
            cell.pack_start(value, False, False, 0)
            grid.attach(cell, index % 2, index // 2, 1, 1)
            values[key] = value
        inner.pack_start(grid, False, False, 0)
        return card, values

    def _refresh_interval_changed(self, combo):
        self._refresh_seconds = int(combo.get_active_id() or "4")
        if self._refresh_id is not None:
            GLib.source_remove(self._refresh_id)
            self._refresh_id = GLib.timeout_add_seconds(self._refresh_seconds, self._periodic_refresh)

    def _show_join_qr(self, *_):
        if not self._snapshot.get("running") or not self._saved_settings.get("password"):
            return
        try:
            payload = wifi_payload(self._saved_settings.get("ssid", ""), self._saved_settings["password"],
                                   hidden=self._saved_options.get("hidden", False))
            show_qr_dialog(self, "Join your hotspot", payload,
                           "Scan with your phone’s Camera, then tap Join. This code contains your Wi-Fi password.", sensitive=True)
        except Exception as exc:
            self._toast(self._safe_text(exc), error=True)

    def _toast(self, message, error=False):
        if self._closing:
            return
        text = self._safe_text(message)
        self.footer_label.set_text(text.replace("\n", " "))
        self.footer_label.set_tooltip_text(text)
        context = self.footer_label.get_style_context()
        context.remove_class("muted")
        context.remove_class("toast-error")
        context.remove_class("toast-success")
        context.add_class("toast-error" if error else "toast-success")

    def _safe_text(self, text):
        text = str(text)
        for secret in (self.password_entry.get_text(), self._saved_settings.get("password", "")):
            if secret:
                text = text.replace(secret, "[hidden]")
        return "".join(c for c in text if c.isprintable() or c in "\n\t")

    def _update_sensitivity(self):
        connected = bool(getattr(self.manager, "connected", False))
        if not connected and not self._connecting:
            self.unlock_button._caption.set_text("Unlock controls")
            self.auth_card.show()
        ready = connected and not self._busy and not self._connecting
        running = bool(self._snapshot.get("running"))
        self.unlock_button.set_sensitive(not self._connecting and not connected)
        self.start_button.set_sensitive(ready and not running)
        self.stop_button.set_sensitive(ready and (running or self._snapshot.get("status", "").lower() == "needs restart"))
        self.refresh_button.set_sensitive(ready and not self._refresh_pending)
        for control in (self.ssid_entry, self.password_entry, self.source_combo, self.reveal_toggle,
                        self.hidden_toggle, self.isolation_toggle, self.generate_button):
            control.set_sensitive(ready)
        self.join_qr_button.set_sensitive(ready and running)
        self.apply_button.set_sensitive(ready and self._dirty)
        self.blocked_button.set_sensitive(ready)
        self.logs_button.set_sensitive(ready)
        selected = self._selected_client()
        self.disconnect_button.set_sensitive(ready and running and selected is not None)
        self.block_button.set_sensitive(ready and running and selected is not None)
        self.selection_label.set_text(f"Selected: {selected['name']}" if selected else "Select a device to manage access")
        self._render_device_details()
        self.apply_button._caption.set_text("Apply & restart" if running else "Save settings")
        self._update_file_sharing()
        if self._busy or self._connecting:
            self.spinner.show()
            self.spinner.start()
        else:
            self.spinner.stop()
            self.spinner.hide()

    def _unlock(self, *_):
        if self._connecting or getattr(self.manager, "connected", False):
            return False
        self._connecting = True
        self._toast("Opening system authorization…" if not self.demo else "Loading demo preview…")
        self._update_sensitivity()
        def completed(result, error):
            if self._closing:
                return
            self._connecting = False
            if error:
                self._toast(f"Could not unlock: {error}", error=True)
            else:
                self.auth_card.hide()
                self.unlock_button._caption.set_text("Controls unlocked")
                self._receive(result, force_form=True)
                self._toast("Demo preview ready. No network settings are changed." if self.demo else "Controls unlocked. Your hotspot is ready to manage.")
                if self._refresh_id is None:
                    self._refresh_id = GLib.timeout_add_seconds(self._refresh_seconds, self._periodic_refresh)
            self._update_sensitivity()
        try:
            self.manager.connect(completed)
        except Exception as exc:
            completed(None, exc)
        return False

    def _receive(self, payload, force_form=False):
        if self._closing or not isinstance(payload, dict):
            return
        if getattr(self.manager, "connected", False):
            self.auth_card.hide()
            self.unlock_button._caption.set_text("Controls unlocked")
        snapshot = payload.get("snapshot")
        if isinstance(snapshot, dict):
            self._snapshot = snapshot
        metrics = payload.get("metrics", self._snapshot.get("metrics"))
        if isinstance(metrics, dict):
            self._metrics = dict(metrics)
        options = payload.get("options")
        if isinstance(options, dict):
            self._saved_options = {key: bool(options.get(key, False)) for key in ("hidden", "isolate_clients")}
        settings = payload.get("settings")
        interfaces = payload.get("interfaces")
        if isinstance(interfaces, list):
            self._interfaces = interfaces
        if isinstance(settings, dict):
            self._saved_settings = dict(settings)
            if force_form or not self._dirty:
                self._load_form(settings)
        self._render_snapshot()
        self._update_sensitivity()
        self.updated_label.set_text("Updated " + datetime.datetime.now().strftime("%H:%M:%S"))

    def _load_form(self, settings):
        self._loading_form = True
        self.ssid_entry.set_text(str(settings.get("ssid", "")))
        self.password_entry.set_text(str(settings.get("password", "")))
        self.source_combo.remove_all()
        available = set()
        for item in self._interfaces:
            name = str(item.get("name", ""))
            if not name:
                continue
            available.add(name)
            kind = "Wi-Fi" if item.get("type") == "wifi" else "Ethernet"
            state = item.get("state", "")
            state = "Connected" if state == "connected" else str(state).replace("-", " ").capitalize()
            self.source_combo.append(name, f"{name}  ·  {kind}" + (f"  ·  {state}" if state else ""))
        source = str(settings.get("upstream", ""))
        if source and source not in available:
            self.source_combo.append(source, source + "  ·  unavailable")
        self.source_combo.set_active_id(source)
        self.hidden_toggle.set_active(self._saved_options.get("hidden", False))
        self.isolation_toggle.set_active(self._saved_options.get("isolate_clients", False))
        self._loading_form = False
        self._dirty = False
        self.edit_badge.set_text("SAVED")
        self.edit_badge.get_style_context().remove_class("warning")

    def _form_changed(self, *_):
        if self._loading_form:
            return
        current = {"ssid": self.ssid_entry.get_text(), "password": self.password_entry.get_text(), "upstream": self.source_combo.get_active_id() or ""}
        options = {"hidden": self.hidden_toggle.get_active(), "isolate_clients": self.isolation_toggle.get_active()}
        self._dirty = current != self._saved_settings or options != self._saved_options
        self.edit_badge.set_text("UNSAVED" if self._dirty else "SAVED")
        if self._dirty:
            self.edit_badge.get_style_context().add_class("warning")
        else:
            self.edit_badge.get_style_context().remove_class("warning")
        self._update_sensitivity()

    def _render_snapshot(self):
        snapshot = self._snapshot
        running = bool(snapshot.get("running"))
        status = str(snapshot.get("status", "Offline"))
        needs_restart = status.lower() == "needs restart"
        self.status_label.set_text("ONLINE" if running else "NEEDS RESTART" if needs_restart else "OFFLINE")
        context = self.status_label.get_style_context()
        for css in ("online", "offline", "warning"):
            context.remove_class(css)
        context.add_class("online" if running else "warning" if needs_restart else "offline")
        self.network_title.set_text(str(snapshot.get("ssid") or self._saved_settings.get("ssid") or "Your hotspot"))
        self.hero_subtitle.set_text("Your hotspot is sharing a secure WPA2 connection." if running else "Restart to restore the radio connection." if needs_restart else "Start your hotspot to share this computer’s Internet.")
        clients = snapshot.get("clients", [])
        self.client_stat.set_text(str(len(clients)))
        self.source_stat.set_text(str(snapshot.get("upstream") or "—"))
        channel = str(snapshot.get("channel", "—"))
        self.channel_stat.set_text(f"{channel}  ·  2.4 GHz" if running and channel != "—" else "—")
        self.device_count.set_text(str(len(clients)))
        selected = self._selected_client()
        selected_mac = selected["mac"] if selected else None
        self.device_store.clear()
        for client in clients:
            signal = client.get("signal", "—")
            signal_text = f"{signal} dBm" if isinstance(signal, (int, float)) else str(signal)
            total = int(client.get("rx_bytes", 0) or 0) + int(client.get("tx_bytes", 0) or 0)
            row = self.device_store.append([str(client.get("name") or "Unknown device"), str(client.get("ip") or "—"), str(client.get("mac") or ""), signal_text, _gui_bytes(total),
                                            _gui_rate(client.get("download_bps")) + " / " + _gui_rate(client.get("upload_bps")),
                                            _gui_duration(client.get("connected_seconds"))])
            if client.get("mac") == selected_mac:
                self.device_view.get_selection().select_iter(row)
        self.empty_label.set_text("No devices connected. Join from your phone or another device." if running else "Your connected devices will appear here when the hotspot is online.")
        self.empty_label.set_visible(not clients)
        self.blocked_button._caption.set_text(f"Blocked devices ({len(snapshot.get('blocked', []))})")
        notices = [str(text).replace("press S to restart", "select Start to restart") for text in snapshot.get("notices", []) if text]
        if self.demo:
            notices = [text for text in notices if "DEMO MODE" not in text.upper()]
        self.notice_label.set_text(self._safe_text("\n".join(dict.fromkeys(notices))))
        self.notice_label.set_visible(bool(notices))
        self._render_metrics()
        self._render_device_details()
        self._populate_blocked()

    def _render_metrics(self):
        metrics = self._metrics
        for key, label in self.traffic_values.items():
            value = metrics.get(key)
            if key == "link_utilization_percent":
                try:
                    number = float(value)
                    text = f"{number:.1f}%" if math.isfinite(number) else "Unavailable"
                except (ValueError, TypeError):
                    text = "Unavailable"
            else:
                text = _gui_rate(value)
            label.set_text(text)
        percent, dbm = metrics.get("signal_percent"), metrics.get("signal_dbm")
        signal = f"{percent}% · {dbm} dBm" if percent is not None and dbm is not None else "Unavailable"
        def link(value):
            return f"{value:g} Mb/s" if isinstance(value, (int, float)) and math.isfinite(value) else "—"
        snapshot = self._snapshot
        details = {
            "source_signal": signal,
            "source_link": link(metrics.get("rx_link_mbps")) + " / " + link(metrics.get("tx_link_mbps")),
            "source_upload": _gui_rate(metrics.get("upstream_upload_bps")),
            "ap_address": snapshot.get("ap_ipv4") or "—",
            "uptime": _gui_duration(snapshot.get("hotspot_uptime_seconds")),
            "driver": snapshot.get("radio_driver") or "—",
            "source_frequency": str(metrics.get("upstream_frequency_mhz")) + " MHz" if metrics.get("upstream_frequency_mhz") else "—",
            "totals": _gui_bytes(metrics.get("download_total_bytes")) + " / " + _gui_bytes(metrics.get("upload_total_bytes")),
            "source_network": metrics.get("upstream_ssid") or "—",
            "source_address": str(snapshot.get("upstream_ipv4") or "—") + " / " + str(snapshot.get("upstream_gateway") or "—"),
        }
        for key, label in self.info_values.items():
            label.set_text(str(details.get(key, "—")))

    def _render_device_details(self):
        selected = self._selected_client()
        client = next((item for item in self._snapshot.get("clients", []) if selected and item.get("mac") == selected["mac"]), {})
        def link(value):
            return f"{value:g} Mb/s" if isinstance(value, (int, float)) and math.isfinite(value) else "—"
        signal = client.get("signal")
        values = {
            "name": client.get("name") or ("Unknown device" if client else "Select a device above"),
            "mac": client.get("mac") or "—", "ip": client.get("ip") or "—",
            "signal": f"{signal} dBm" if isinstance(signal, (int, float)) else "—",
            "connected": _gui_duration(client.get("connected_seconds")),
            "inactive": f"{client['inactive_ms']} ms ago" if client.get("inactive_ms") is not None else "—",
            "download": _gui_bytes(client.get("tx_bytes")) + " / " + _gui_rate(client.get("download_bps")) if client else "—",
            "upload": _gui_bytes(client.get("rx_bytes")) + " / " + _gui_rate(client.get("upload_bps")) if client else "—",
            "rx_link": link(client.get("rx_link_mbps")), "tx_link": link(client.get("tx_link_mbps")),
        }
        for key, label in self.device_detail_values.items():
            label.set_text(str(values.get(key, "—")))

    def _selected_client(self):
        if not hasattr(self, "device_view"):
            return None
        model, selected = self.device_view.get_selection().get_selected()
        if selected is None:
            return None
        return {"name": model[selected][0], "mac": model[selected][2]}

    def _periodic_refresh(self):
        if self._closing:
            return False
        if not self.get_visible():
            return True
        if getattr(self.manager, "connected", False) and not self._busy and not self._refresh_pending:
            self._refresh()
        return True

    def _refresh(self, manual=False):
        if self._busy or self._refresh_pending or not getattr(self.manager, "connected", False):
            return
        self._refresh_pending = True
        self._update_sensitivity()
        def completed(result, error):
            self._refresh_pending = False
            if self._closing:
                return
            if error:
                self._toast(f"Could not refresh: {error}", error=True)
            else:
                self._receive(result)
                if manual:
                    self._toast("Device list and hotspot status refreshed.")
            self._update_sensitivity()
        self.manager.request("snapshot", {}, completed)

    def _perform(self, action, params, progress, success, after=None):
        if self._busy or not getattr(self.manager, "connected", False):
            return
        self._busy = True
        self._toast(progress)
        self._update_sensitivity()
        def completed(result, error):
            self._busy = False
            if self._closing:
                return
            if error:
                self._toast(error, error=True)
            else:
                self._receive(result, force_form=action == "settings")
                self._toast(success)
                if after:
                    after(result)
            self._update_sensitivity()
        try:
            self.manager.request(action, params, completed)
        except Exception as exc:
            completed(None, exc)

    def _confirm(self, title, explanation, action):
        dialog = Gtk.MessageDialog(transient_for=self, modal=True, destroy_with_parent=True,
                                   message_type=Gtk.MessageType.QUESTION, buttons=Gtk.ButtonsType.NONE,
                                   text=title)
        dialog.get_style_context().add_class("hotspot-window")
        dialog.format_secondary_text(explanation)
        dialog.add_button("Cancel", Gtk.ResponseType.CANCEL)
        accept = dialog.add_button(action, Gtk.ResponseType.OK)
        accept.get_style_context().add_class("primary")
        dialog.set_default_response(Gtk.ResponseType.CANCEL)
        result = dialog.run()
        dialog.destroy()
        return result == Gtk.ResponseType.OK

    def _stop_clicked(self, *_):
        count = len(self._snapshot.get("clients", []))
        if count and not self._confirm("Stop your hotspot?", f"{count} connected device{'s' if count != 1 else ''} will lose Internet access. You can start the hotspot again at any time.", "Stop hotspot"):
            return
        self._perform("stop", {}, "Stopping the hotspot…", "Hotspot stopped.")

    def _apply_clicked(self, *_):
        settings = {"ssid": self.ssid_entry.get_text(), "password": self.password_entry.get_text(), "upstream": self.source_combo.get_active_id() or ""}
        if not settings["ssid"].strip() or len(settings["ssid"].encode("utf-8")) > 32 or any(not c.isprintable() for c in settings["ssid"]):
            self._toast("Enter a network name of 1–32 UTF-8 bytes with no control characters.", error=True)
            self.ssid_entry.grab_focus()
            return
        if not 8 <= len(settings["password"]) <= 63 or any(not 32 <= ord(c) <= 126 for c in settings["password"]):
            self._toast("Use a password of 8–63 printable English characters.", error=True)
            self.password_entry.grab_focus()
            return
        if not settings["upstream"]:
            self._toast("Choose an Internet interface before saving.", error=True)
            self.source_combo.grab_focus()
            return
        count = len(self._snapshot.get("clients", []))
        if self._snapshot.get("running") and count:
            password_changed = settings["password"] != self._saved_settings.get("password")
            detail = f"Applying these settings will restart the hotspot and disconnect {count} device{'s' if count != 1 else ''}."
            if password_changed:
                detail += " Reconnect your devices with the new password."
            else:
                detail += " Your devices can reconnect after the restart."
            if not self._confirm("Apply settings and restart?", detail, "Apply & restart"):
                return
        settings.update(hidden=self.hidden_toggle.get_active(), isolate_clients=self.isolation_toggle.get_active())
        self._perform("settings", settings, "Applying network settings…", "Settings applied. Hotspot restarted." if self._snapshot.get("running") else "Network settings saved.")

    def _disconnect_clicked(self, *_):
        selected = self._selected_client()
        if selected:
            self._perform("kick", {"mac": selected["mac"]}, f"Disconnecting {selected['name']}…", f"{selected['name']} disconnected. It can reconnect with the password.")

    def _block_clicked(self, *_):
        selected = self._selected_client()
        if selected and self._confirm(f"Block {selected['name']}?", f"This device ({selected['mac']}) will be disconnected and cannot rejoin until you unblock its MAC address.", "Block device"):
            self._perform("block", {"mac": selected["mac"]}, f"Blocking {selected['name']}…", f"{selected['name']} blocked. Manage it under Blocked devices.")

    def _populate_blocked(self):
        if self._blocked_store is None:
            return
        self._blocked_store.clear()
        for mac in self._snapshot.get("blocked", []):
            self._blocked_store.append([str(mac)])
        if hasattr(self, "_blocked_empty"):
            self._blocked_empty.set_visible(len(self._blocked_store) == 0)
        if hasattr(self, "_unblock_button"):
            self._unblock_button.set_sensitive(False)

    def _show_blocked(self, *_):
        dialog = Gtk.Dialog(title="Blocked devices", transient_for=self, modal=True, destroy_with_parent=True)
        dialog.get_style_context().add_class("hotspot-window")
        dialog.set_default_size(510, 370)
        dialog.add_button("Close", Gtk.ResponseType.CLOSE)
        self._blocked_dialog = dialog
        content = _gui_pad(Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12), 18)
        content.pack_start(_gui_label("Manage blocked devices", "section-title"), False, False, 0)
        text = _gui_label("Select a MAC address to allow that device to reconnect.", "muted tiny")
        text.set_line_wrap(True)
        content.pack_start(text, False, False, 0)
        self._blocked_store = Gtk.ListStore(str)
        view = Gtk.TreeView(model=self._blocked_store)
        view.append_column(Gtk.TreeViewColumn("MAC ADDRESS", Gtk.CellRendererText(), text=0))
        scroll = Gtk.ScrolledWindow()
        scroll.set_min_content_height(160)
        scroll.add(view)
        overlay = Gtk.Overlay()
        overlay.add(scroll)
        self._blocked_empty = _gui_label("No blocked devices.", "empty")
        self._blocked_empty.set_valign(Gtk.Align.CENTER)
        self._blocked_empty.set_halign(Gtk.Align.CENTER)
        overlay.add_overlay(self._blocked_empty)
        content.pack_start(overlay, True, True, 0)
        self._unblock_button = _gui_button("Unblock selected", "changes-allow-symbolic", "primary")
        self._unblock_button.set_halign(Gtk.Align.END)
        def selection_changed(selection):
            _, tree_iter = selection.get_selected()
            self._unblock_button.set_sensitive(tree_iter is not None and not self._busy)
        view.get_selection().connect("changed", selection_changed)
        def unblock(*_):
            model, tree_iter = view.get_selection().get_selected()
            if tree_iter is not None:
                mac = model[tree_iter][0]
                self._unblock_button.set_sensitive(False)
                self._perform("unblock", {"mac": mac}, f"Unblocking {mac}…", f"{mac} can reconnect with the hotspot password.")
        self._unblock_button.connect("clicked", unblock)
        content.pack_start(self._unblock_button, False, False, 0)
        dialog.get_content_area().pack_start(content, True, True, 0)
        dialog.show_all()
        self._populate_blocked()
        dialog.run()
        self._blocked_store = None
        self._blocked_dialog = None
        dialog.destroy()

    def _show_logs(self, *_):
        def show(result):
            if self._closing:
                return
            dialog = Gtk.Dialog(title="Hotspot activity log", transient_for=self, modal=True, destroy_with_parent=True)
            dialog.get_style_context().add_class("hotspot-window")
            dialog.set_default_size(740, 430)
            dialog.add_button("Close", Gtk.ResponseType.CLOSE)
            content = _gui_pad(Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12), 16)
            content.pack_start(_gui_label("Activity log", "section-title"), False, False, 0)
            content.pack_start(_gui_label("Recent hotspot messages · passwords are hidden", "muted tiny"), False, False, 0)
            view = Gtk.TextView()
            view.set_editable(False)
            view.set_cursor_visible(False)
            view.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
            view.set_left_margin(12)
            view.set_right_margin(12)
            view.set_top_margin(10)
            view.set_bottom_margin(10)
            log_text = result if isinstance(result, str) else result.get("logs", "No activity yet.") if isinstance(result, dict) else "No activity yet."
            view.get_buffer().set_text(self._safe_text(log_text))
            scroll = Gtk.ScrolledWindow()
            scroll.add(view)
            content.pack_start(scroll, True, True, 0)
            dialog.get_content_area().pack_start(content, True, True, 0)
            dialog.show_all()
            dialog.run()
            dialog.destroy()
        self._perform("logs", {}, "Reading the activity log…", "Activity log loaded.", after=show)

    def _on_close(self, *_):
        callback = getattr(self, "on_request_close", None)
        if callable(callback):
            return bool(callback())
        self._closing = True
        self._stop_file_share(close_firewall=False)
        if self._refresh_id is not None:
            GLib.source_remove(self._refresh_id)
            self._refresh_id = None
        self.manager.close()
        return False



import math
from hotspot_qr import show_qr_dialog, wifi_payload


_COMPACT_GUI_CSS = b"""
.hotspot-window.compact-window { font-size: 9.5pt; }
.compact-window label { min-height: 0; padding: 0; }
.compact-window .compact-name { font-size: 16pt; font-weight: 800; color: #f2f8ff; }
.compact-window .compact-caption { font-size: 8pt; font-weight: 800; letter-spacing: 0.7px; }
.compact-window .compact-value { font-size: 19pt; font-weight: 800; color: #e9f7f8; }
.compact-window .compact-icon { background: #123d42; color: #64e2ce; border-radius: 10px; padding: 8px; }
.compact-window .compact-metric { background: #162130; border: 1px solid #26394b; border-radius: 11px; }
.compact-window .compact-download { color: #75e0cc; }
.compact-window .compact-upload { color: #90c8fb; }
.compact-window .compact-signal trough { background: #23354a; border: none; border-radius: 3px; min-height: 5px; }
.compact-window .compact-signal progress { background: #62d9c6; border: none; border-radius: 3px; min-height: 5px; }
.compact-window button { padding: 7px 10px; }
.compact-window .compact-message { font-size: 8pt; color: #8298ae; }
"""


def _compact_rate(value):
    """Traffic rates are bytes per second, from connected devices' perspective."""
    try:
        value = max(0.0, float(value or 0))
        if not math.isfinite(value):
            value = 0.0
    except (ValueError, TypeError, OverflowError):
        value = 0.0
    return _gui_bytes(value) + "/s"


def _compact_link(value):
    try:
        number = float(value)
        if not math.isfinite(number) or number <= 0:
            return "—"
        return f"{number:g} Mb/s"
    except (ValueError, TypeError, OverflowError):
        return "—"


class CompactHotspotWindow(Gtk.Window):
    """Small, live dashboard sharing the full window's asynchronous controller."""

    def __init__(self, manager, demo=False, on_open_full=None):
        super().__init__(title="Z Hotspot · Compact" + (" · Demo" if demo else ""))
        self.manager = manager
        self.demo = demo
        self.on_open_full = on_open_full
        self._closing = False
        self._busy = False
        self._connecting = False
        self._refresh_pending = False
        self._refresh_id = None
        self._saved_settings = {}
        self._options = {}
        self._snapshot = {"running": False, "clients": [], "status": "Locked"}
        self._metrics = {}
        self.set_default_size(444, 420)
        self.set_size_request(420, 390)
        self.set_resizable(False)
        self.set_position(Gtk.WindowPosition.CENTER)
        self.set_icon_name("z-hotspot")
        icon_file = Path(__file__).resolve().with_name("icon.svg")
        if icon_file.is_file():
            try:
                self.set_icon_from_file(str(icon_file))
            except GLib.Error:
                pass
        self.set_wmclass("hotspot-manager", "Hotspot Manager")
        for css in ("hotspot-window", "compact-window"):
            self.get_style_context().add_class(css)
        provider = Gtk.CssProvider()
        provider.load_from_data(_GUI_CSS + _COMPACT_GUI_CSS)
        Gtk.StyleContext.add_provider_for_screen(Gdk.Screen.get_default(), provider,
                                                 Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
        self._provider = provider
        self._build_ui()
        self.connect("delete-event", self._on_close)
        self.connect("show", self._on_show)
        self._update_sensitivity()
        self.show_all()
        self.spinner.hide()
        self._refresh_id = GLib.timeout_add_seconds(2, self._periodic_refresh)
        if self.demo and not getattr(self.manager, "connected", False):
            GLib.idle_add(self._unlock)

    def _build_ui(self):
        shell = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=9)
        _gui_pad(shell, 15)
        self.add(shell)
        header = Gtk.Box(spacing=9)
        icon = _gui_logo(42)
        header.pack_start(icon, False, False, 0)
        words = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=1)
        words.pack_start(_gui_label("Z Hotspot", "brand"), False, False, 0)
        words.pack_start(_gui_label("v0.1 · @zalem.j", "muted tiny"), False, False, 0)
        header.pack_start(words, True, True, 0)
        self.unlock_button = _gui_button("Unlock", "changes-allow-symbolic")
        self.unlock_button.set_tooltip_text("Authorize hotspot controls with your Linux password")
        self.unlock_button.set_valign(Gtk.Align.CENTER)
        self.unlock_button.connect("clicked", self._unlock)
        header.pack_end(self.unlock_button, False, False, 0)
        shell.pack_start(header, False, False, 0)

        hero = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        hero.get_style_context().add_class("hero")
        content = _gui_pad(Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=9), 12)
        network = Gtk.Box(spacing=8)
        self.network_title = _gui_label("Your hotspot", "compact-name")
        self.network_title.set_ellipsize(Pango.EllipsizeMode.END)
        self.network_title.set_max_width_chars(21)
        network.pack_start(self.network_title, True, True, 0)
        self.status_label = _gui_label("LOCKED", "badge offline")
        self.status_label.set_valign(Gtk.Align.CENTER)
        network.pack_end(self.status_label, False, False, 0)
        content.pack_start(network, False, False, 0)
        signal = Gtk.Box(spacing=8)
        signal.pack_start(_gui_label("Source signal", "muted tiny"), True, True, 0)
        self.signal_label = _gui_label("—", "tiny")
        signal.pack_end(self.signal_label, False, False, 0)
        content.pack_start(signal, False, False, 0)
        self.signal_bar = Gtk.ProgressBar()
        self.signal_bar.get_style_context().add_class("compact-signal")
        self.signal_bar.set_tooltip_text("Signal strength of the Internet source Wi-Fi connection")
        content.pack_start(self.signal_bar, False, False, 0)
        hero.pack_start(content, False, False, 0)
        shell.pack_start(hero, False, False, 0)

        rates = Gtk.Box(spacing=9, homogeneous=True)
        self.download_label, self.download_link_label = self._metric(rates, "↓  DOWNLOAD", "compact-download")
        self.upload_label, self.upload_link_label = self._metric(rates, "↑  UPLOAD", "compact-upload")
        rates.set_tooltip_text("Live transfer rates for devices using your hotspot; not an Internet speed test")
        shell.pack_start(rates, False, False, 0)

        devices = Gtk.Box(spacing=8)
        devices.pack_start(_gui_icon("network-workgroup-symbolic"), False, False, 0)
        devices.pack_start(_gui_label("Connected devices", "muted"), True, True, 0)
        self.count_label = _gui_label("—", "section-title")
        devices.pack_start(self.count_label, False, False, 0)
        self.join_qr_button = _gui_button("Join QR", "camera-photo-symbolic")
        self.join_qr_button.set_tooltip_text("Scan a QR code to join this hotspot from your phone")
        self.join_qr_button.connect("clicked", self._join_qr)
        devices.pack_end(self.join_qr_button, False, False, 0)
        shell.pack_start(devices, False, False, 0)

        controls = Gtk.Box(spacing=7, homogeneous=True)
        self.start_button = _gui_button("Start", "media-playback-start-symbolic", "primary")
        self.stop_button = _gui_button("Stop", "media-playback-stop-symbolic")
        self.refresh_button = _gui_button("Refresh", "view-refresh-symbolic")
        self.start_button.connect("clicked", lambda *_: self._perform("start"))
        self.stop_button.connect("clicked", lambda *_: self._perform("stop"))
        self.refresh_button.connect("clicked", lambda *_: self._refresh(manual=True))
        for button in (self.start_button, self.stop_button, self.refresh_button):
            controls.pack_start(button, True, True, 0)
        shell.pack_start(controls, False, False, 0)

        footer = Gtk.Box(spacing=7)
        self.spinner = Gtk.Spinner()
        footer.pack_start(self.spinner, False, False, 0)
        self.updated_label = _gui_label("Unlock to load live status", "muted tiny")
        footer.pack_start(self.updated_label, True, True, 0)
        self.full_button = _gui_button("Full view", "view-fullscreen-symbolic", "flat-button")
        self.full_button.set_tooltip_text("Open network settings, devices and file sharing")
        self.full_button.connect("clicked", self._open_full)
        footer.pack_end(self.full_button, False, False, 0)
        self.background_button = _gui_button("", "go-bottom-symbolic", "flat-button")
        self.background_button.set_tooltip_text("Run in background · show Z Hotspot from the system tray")
        self.background_button.connect("clicked", lambda *_: self.on_background() if callable(getattr(self, "on_background", None)) else None)
        footer.pack_end(self.background_button, False, False, 0)
        shell.pack_start(footer, False, False, 0)
        self.message_label = _gui_label("Your hotspot stays on when you close this window.", "compact-message")
        self.message_label.set_ellipsize(Pango.EllipsizeMode.END)
        self.message_label.set_max_width_chars(54)
        shell.pack_start(self.message_label, False, False, 0)

    def _metric(self, parent, title, accent):
        card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        card.get_style_context().add_class("compact-metric")
        inside = _gui_pad(Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4), 10)
        inside.pack_start(_gui_label(title, "tiny compact-caption " + accent), False, False, 0)
        value = _gui_label("—", "compact-value")
        inside.pack_start(value, False, False, 0)
        link = _gui_label("Link —", "muted tiny")
        inside.pack_start(link, False, False, 0)
        card.pack_start(inside, False, False, 0)
        parent.pack_start(card, True, True, 0)
        return value, link

    def _message(self, text, error=False):
        safe = str(text)
        password = self._saved_settings.get("password")
        if isinstance(password, str) and password:
            safe = safe.replace(password, "[hidden]")
        safe = " ".join(safe.split())
        self.message_label.set_text(safe)
        self.message_label.set_tooltip_text(safe)
        context = self.message_label.get_style_context()
        context.remove_class("toast-error")
        if error:
            context.add_class("toast-error")

    def _update_sensitivity(self):
        connected = bool(getattr(self.manager, "connected", False))
        ready = connected and not self._connecting and not self._busy and not self._refresh_pending
        running = bool(self._snapshot.get("running"))
        self.unlock_button._caption.set_text("Unlocked" if connected else "Unlock")
        self.unlock_button.set_sensitive(not connected and not self._connecting)
        self.start_button.set_sensitive(ready and not running)
        self.stop_button.set_sensitive(ready and (running or str(self._snapshot.get("status", "")).lower() == "needs restart"))
        self.refresh_button.set_sensitive(ready and not self._refresh_pending)
        self.join_qr_button.set_sensitive(ready and running)
        self.full_button.set_sensitive(not self._busy and not self._connecting and not self._refresh_pending)
        spinning = self._connecting or self._busy or self._refresh_pending
        self.spinner.set_visible(spinning)
        self.spinner.start() if spinning else self.spinner.stop()

    def _on_show(self, *_):
        if not self._closing and getattr(self.manager, "connected", False):
            GLib.idle_add(self._refresh)

    def _open_full(self, *_):
        if self.on_open_full and not self._busy and not self._connecting and not self._refresh_pending:
            self.on_open_full()

    def _join_qr(self, *_):
        if (self._closing or self._busy or self._connecting or self._refresh_pending
                or not getattr(self.manager, "connected", False) or not self._snapshot.get("running")):
            return
        try:
            payload = wifi_payload(self._saved_settings.get("ssid", ""),
                                   self._saved_settings.get("password", ""),
                                   hidden=self._options.get("hidden") is True)
        except (TypeError, ValueError):
            self._message("Save a valid network name and password in Full view before creating a QR code.", error=True)
            return
        show_qr_dialog(self, "Join hotspot" + (" · Demo" if self.demo else ""), payload,
                       "Demo QR · sample credentials only." if self.demo else "Scan to connect to this hotspot.",
                       sensitive=True)

    def _unlock(self, *_):
        if self._closing or self._connecting:
            return False
        if getattr(self.manager, "connected", False):
            self._refresh()
            return False
        self._connecting = True
        self._message("Loading demo preview…" if self.demo else "Waiting for system authorization…")
        self._update_sensitivity()
        def completed(result, error):
            self._connecting = False
            if self._closing:
                return
            if error:
                self._message(f"Could not unlock: {error}", error=True)
            else:
                self._receive(result)
                self._message("Demo preview · no network changes" if self.demo else "Controls unlocked. Ready to manage your hotspot.")
            self._update_sensitivity()
        try:
            self.manager.connect(completed)
        except Exception as exc:
            completed(None, exc)
        return False

    def _receive(self, payload):
        if self._closing or not isinstance(payload, dict):
            return
        if isinstance(payload.get("snapshot"), dict):
            self._snapshot = payload["snapshot"]
        if isinstance(payload.get("settings"), dict):
            self._saved_settings = dict(payload["settings"])
        if isinstance(payload.get("options"), dict):
            self._options = dict(payload["options"])
        self._metrics = dict(payload.get("metrics") or {}) if isinstance(payload.get("metrics"), dict) else {}
        snapshot, metrics = self._snapshot, self._metrics
        running = bool(snapshot.get("running"))
        status = str(snapshot.get("status", "Offline"))
        unlocked = bool(getattr(self.manager, "connected", False))
        self.status_label.set_text("ONLINE" if running else "RESTART" if status.lower() == "needs restart" else "OFFLINE" if unlocked else "LOCKED")
        context = self.status_label.get_style_context()
        for css in ("online", "offline", "warning"):
            context.remove_class(css)
        context.add_class("online" if running else "warning" if status.lower() == "needs restart" else "offline")
        self.network_title.set_text(str(snapshot.get("ssid") or self._saved_settings.get("ssid") or "Your hotspot"))
        self.network_title.set_tooltip_text(self.network_title.get_text())
        clients = snapshot.get("clients", [])
        self.count_label.set_text(str(len(clients)) if isinstance(clients, list) else "0")
        self.download_label.set_text(_compact_rate(metrics.get("download_bps")) if unlocked else "—")
        self.upload_label.set_text(_compact_rate(metrics.get("upload_bps")) if unlocked else "—")
        self.download_link_label.set_text("RX link " + _compact_link(metrics.get("rx_link_mbps")))
        self.upload_link_label.set_text("TX link " + _compact_link(metrics.get("tx_link_mbps")))
        percent = metrics.get("signal_percent")
        dbm = metrics.get("signal_dbm")
        valid_percent = isinstance(percent, (int, float)) and not isinstance(percent, bool) and math.isfinite(percent)
        valid_dbm = isinstance(dbm, (int, float)) and not isinstance(dbm, bool) and math.isfinite(dbm)
        self.signal_bar.set_fraction(max(0.0, min(100.0, float(percent))) / 100.0 if valid_percent else 0)
        signal_parts = []
        if valid_percent:
            signal_parts.append(f"{max(0, min(100, round(percent)))}%")
        if valid_dbm:
            signal_parts.append(f"{round(dbm)} dBm")
        self.signal_label.set_text(" · ".join(signal_parts) if signal_parts else "Unavailable")
        self.updated_label.set_text(("Demo · " if self.demo else "") + "Updated " + datetime.datetime.now().strftime("%H:%M:%S"))
        notices = [str(item) for item in snapshot.get("notices", []) if item and "DEMO MODE" not in str(item).upper()]
        if notices:
            self._message(notices[0])
        self._update_sensitivity()

    def _periodic_refresh(self):
        if self._closing:
            return False
        if self.get_visible():
            self._refresh()
        return True

    def _refresh(self, manual=False):
        if self._closing or not self.get_visible() or self._busy or self._connecting or self._refresh_pending or not getattr(self.manager, "connected", False):
            return False
        self._refresh_pending = True
        self._update_sensitivity()
        def completed(result, error):
            self._refresh_pending = False
            if self._closing:
                return
            if error:
                self._message(f"Could not refresh: {error}", error=True)
            else:
                self._receive(result)
                if manual:
                    self._message("Status, transfer rates and devices refreshed.")
            self._update_sensitivity()
        try:
            self.manager.request("snapshot", {}, completed)
        except Exception as exc:
            completed(None, exc)
        return False

    def _perform(self, action):
        if self._closing or self._busy or self._connecting or self._refresh_pending or not getattr(self.manager, "connected", False):
            return
        self._busy = True
        self._message("Starting your hotspot…" if action == "start" else "Stopping your hotspot…")
        self._update_sensitivity()
        def completed(result, error):
            self._busy = False
            if self._closing:
                return
            if error:
                self._message(error, error=True)
            else:
                self._receive(result)
                self._message("Hotspot is online." if action == "start" else "Hotspot stopped.")
            self._update_sensitivity()
        try:
            self.manager.request(action, {}, completed)
        except Exception as exc:
            completed(None, exc)

    def _on_close(self, *_):
        callback = getattr(self, "on_request_close", None)
        if callable(callback):
            return bool(callback())
        if self._closing:
            return False
        self._closing = True
        if self._refresh_id is not None:
            GLib.source_remove(self._refresh_id)
            self._refresh_id = None
        self.manager.close()
        return False

import argparse
import os
import sys

from hotspot_background import BackgroundController


def main() -> int:
    parser = argparse.ArgumentParser(description="Z Hotspot for Linux")
    parser.add_argument("--demo", action="store_true", help="preview without network changes")
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--full", action="store_true", help="open the large view with settings and file sharing")
    modes.add_argument("--compact", action="store_true", help="open the small connection dashboard (default)")
    parser.add_argument("--background", action="store_true", help="keep running in the system tray")
    parser.add_argument("--backend", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.backend:
        return run_backend_service()
    if os.geteuid() == 0 and not args.demo:
        print("Run the app as your normal user; use Unlock controls inside it.", file=sys.stderr)
        return 2
    if not Gtk.init_check()[0]:
        print("Open this app from your Linux Mint desktop.", file=sys.stderr)
        return 2
    application = Gtk.Application(
        application_id="local.codex.HotspotManager" + (".Demo" if args.demo else ""),
        flags=Gio.ApplicationFlags.HANDLES_COMMAND_LINE)
    for option in ("full", "compact", "demo", "background"):
        application.add_main_option(option, 0, GLib.OptionFlags.NONE, GLib.OptionArg.NONE, option, None)
    state = {"bridge": None, "windows": {}, "closing": False, "mode": "compact"}

    def on_state(payload):
        background.observe(payload)
        for window in list(state["windows"].values()):
            if not window.get_visible() and not window._closing:
                window._receive(payload)

    def close_all(*_):
        if state["closing"]:
            return True
        state["closing"] = True
        background.close()
        for window in list(state["windows"].values()):
            window._closing = True
            if hasattr(window, "_stop_file_share"):
                window._stop_file_share(close_firewall=False)
            if getattr(window, "_refresh_id", None) is not None:
                GLib.source_remove(window._refresh_id)
                window._refresh_id = None
            window.destroy()
        if state["bridge"]:
            state["bridge"].close()
        application.quit()
        return True

    def request_close():
        if background.enabled:
            # enter verifies tray availability before hiding, including when a
            # desktop panel was removed since the previous background request.
            background.enter()
            return True
        return close_all()

    def show_mode(mode):
        if state["closing"]:
            return
        if state["bridge"] is None:
            state["bridge"] = BackendBridge(demo=args.demo)
            state["bridge"].on_state = on_state
        windows = state["windows"]
        if mode not in windows:
            if mode == "full":
                window = HotspotWindow(state["bridge"], demo=args.demo)
                window.on_open_compact = lambda: show_mode("compact")
            else:
                window = CompactHotspotWindow(state["bridge"], demo=args.demo, on_open_full=lambda: show_mode("full"))
            application.add_window(window)
            window.on_request_close = request_close
            window.on_background = background.enter
            windows[mode] = window
        for name, window in windows.items():
            if name != mode:
                window.hide()
        current = windows[mode]
        state["mode"] = mode
        current.present()
        if state["bridge"].connected:
            current._refresh(manual=True)
        background.update()

    background = BackgroundController(application,
                                      lambda: state["bridge"],
                                      lambda: state["windows"],
                                      show_mode, close_all)

    def command_line(app, command):
        options = command.get_options_dict()
        show_mode("full" if options.contains("full") else "compact")
        if options.contains("background"):
            # Creating a window first gives a safe, visible fallback if the
            # desktop cannot provide a tray icon.
            background.enter()
        return 0

    application.connect("command-line", command_line)
    application.connect("activate", lambda *_: show_mode(state["mode"]))
    return application.run(sys.argv)


if __name__ == "__main__":
    raise SystemExit(main())
