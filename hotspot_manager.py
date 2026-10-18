#!/usr/bin/env python3
"""Z Hotspot: English terminal UI for Linux.

Uses the adjacent, BSD-licensed create_ap helper. No Python packages required.
"""
from __future__ import annotations

import argparse
import fcntl
import json
import math
import os
from pathlib import Path
import re
import shutil
import secrets
import signal
import stat
import subprocess
import sys
import tempfile
import threading
import time

BASE = Path(__file__).resolve().parent
CREATE_AP = BASE / "create_ap"
AP_INTERFACE = "ap0"
STATE_DIR = Path("/var/lib/mint-hotspot-manager")
PID_FILE = Path("/run/mint-hotspot.pid")
LOG_FILE = Path("/run/mint-hotspot.log")
MAC_PATTERN = re.compile(r"(?:[0-9a-f]{2}:){5}[0-9a-f]{2}\Z", re.I)
_UNSET_OPTION = object()


def clean_text(value: object) -> str:
    return "".join(c for c in str(value) if c.isprintable())


def validate_mac(value: str) -> str:
    value = value.strip().lower()
    if not MAC_PATTERN.fullmatch(value):
        raise ValueError("Select a valid device MAC address.")
    if value == "00:00:00:00:00:00" or int(value[:2], 16) & 1:
        raise ValueError("A device must have a unicast MAC address.")
    return value


def validate_settings(ssid: str, password: str, upstream: str) -> dict:
    if not ssid.strip() or len(ssid.encode("utf-8")) > 32:
        raise ValueError("Network name must be 1 to 32 UTF-8 bytes.")
    if any(not c.isprintable() for c in ssid):
        raise ValueError("Network name cannot contain control characters.")
    if not 8 <= len(password) <= 63 or any(not 32 <= ord(c) <= 126 for c in password):
        raise ValueError("Password must be 8 to 63 printable English characters.")
    if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_.:-]{0,14}", upstream):
        raise ValueError("Choose a valid Internet interface.")
    return {"ssid": ssid, "password": password, "upstream": upstream}


def validate_options(hidden=False, isolate_clients=False) -> dict:
    if type(hidden) is not bool or type(isolate_clients) is not bool:
        raise ValueError("Hidden network and device isolation must be true or false.")
    return {"hidden": hidden, "isolate_clients": isolate_clients}


def _link_mbps(value: str):
    match = re.match(r"([\d.]+)\s+([KMG]?)Bit/s\b", value, re.I)
    if match:
        try:
            rate = float(match[1]) * {"": 1e-6, "K": .001, "M": 1., "G": 1000.}[match[2].upper()]
            return rate if math.isfinite(rate) and rate >= 0 else None
        except ValueError:
            pass
    return None


def parse_key_values(text: str) -> dict:
    return dict(line.split("=", 1) for line in text.splitlines() if "=" in line)


def parse_stations(text: str) -> list[dict]:
    clients = []
    current = None
    for line in text.splitlines():
        match = re.match(r"Station ((?:[0-9a-f]{2}:){5}[0-9a-f]{2})", line, re.I)
        if match:
            current = {"mac": match[1].lower(), "ip": "—", "name": "Unknown device",
                       "signal": "—", "rx_bytes": 0, "tx_bytes": 0,
                       "connected_seconds": 0, "inactive_ms": None,
                       "rx_link_mbps": None, "tx_link_mbps": None,
                       "rx_packets": None, "tx_packets": None,
                       "tx_retries": None, "tx_failed": None,
                       "signal_average_dbm": None}
            clients.append(current)
        elif current is not None:
            field, _, value = line.strip().partition(":")
            value = value.strip()
            integers = {"rx bytes": "rx_bytes", "tx bytes": "tx_bytes",
                        "connected time": "connected_seconds", "inactive time": "inactive_ms",
                        "rx packets": "rx_packets", "tx packets": "tx_packets",
                        "tx retries": "tx_retries", "tx failed": "tx_failed"}
            if field in integers:
                number = re.match(r"\d+", value)
                if number:
                    current[integers[field]] = int(number[0])
            elif field in ("signal", "signal avg"):
                number = re.match(r"-?\d+", value)
                if number:
                    current["signal" if field == "signal" else "signal_average_dbm"] = int(number[0])
            elif field in ("rx bitrate", "tx bitrate"):
                current["rx_link_mbps" if field == "rx bitrate" else "tx_link_mbps"] = _link_mbps(value)
            elif field == "authorized":
                current["authorized"] = value.lower() == "yes"
    return [c for c in clients if c.get("authorized", True)]


def parse_leases(text: str) -> dict:
    result = {}
    for line in text.splitlines():
        parts = line.split()
        if len(parts) >= 4 and MAC_PATTERN.fullmatch(parts[1]):
            result[parts[1].lower()] = {
                "ip": clean_text(parts[2]),
                "name": clean_text(parts[3]) if parts[3] != "*" else "Unknown device",
            }
    return result


def atomic_json(path: Path, data: dict) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    with tempfile.NamedTemporaryFile("w", dir=path.parent, delete=False, encoding="utf-8") as tmp:
        temp = Path(tmp.name)
        os.chmod(temp, 0o600)
        json.dump(data, tmp, ensure_ascii=False, indent=2)
        tmp.flush()
        os.fsync(tmp.fileno())
    try:
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


class HotspotManager:
    def __init__(self, state_dir: Path = STATE_DIR):
        self.state_dir = Path(state_dir)
        self.config_file = self.state_dir / "settings.json"
        self._lock = threading.RLock()
        self._children: dict[int, subprocess.Popen] = {}
        self.settings = {"ssid": "Z-Hotspot", "password": secrets.token_urlsafe(15),
                         "upstream": self._default_upstream()}
        self.options = validate_options()
        self.blocked: list[str] = []
        self._last_error = ""
        if self.config_file.exists():
            try:
                saved = json.loads(self.config_file.read_text())
                self.settings = validate_settings(saved["ssid"], saved["password"], saved["upstream"])
                saved_options = saved.get("options", {})
                if not isinstance(saved_options, dict) or set(saved_options) - {"hidden", "isolate_clients"}:
                    raise ValueError("Invalid saved network options")
                self.options = validate_options(**saved_options)
                self.blocked = sorted({validate_mac(mac) for mac in saved.get("blocked", [])})
            except (OSError, ValueError, KeyError, TypeError) as exc:
                raise RuntimeError(f"Cannot read saved settings: {clean_text(exc)}") from exc
        else:
            runtime = self._runtime()
            if runtime:
                conf = parse_key_values((runtime / "hostapd.conf").read_text())
                self.settings["ssid"] = conf.get("ssid", self.settings["ssid"])
                self.settings["password"] = conf.get("wpa_passphrase", self.settings["password"])
                self.options = validate_options(hidden=conf.get("ignore_broadcast_ssid") == "1",
                                                isolate_clients=conf.get("ap_isolate") == "1")
                source = runtime / "nat_internet_iface"
                if source.exists():
                    self.settings["upstream"] = source.read_text().strip()

    def _run(self, args: list[str], *, check: bool = True, timeout: float = 10) -> str:
        try:
            completed = subprocess.run(args, stdin=subprocess.DEVNULL, capture_output=True,
                                       text=True, timeout=timeout,
                                       env={**os.environ, "LC_ALL": "C",
                                            "PATH": "/usr/sbin:/usr/bin:/sbin:/bin"})
        except FileNotFoundError as exc:
            raise RuntimeError(f"Missing command: {args[0]}. Install the required packages.") from exc
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError(f"{args[0]} did not respond in time. Try again.") from exc
        if check and completed.returncode:
            detail = completed.stderr.strip() or completed.stdout.strip() or "command failed"
            raise RuntimeError(self._redact(detail[-1800:]))
        return completed.stdout

    def _redact(self, text: str) -> str:
        password = self.settings.get("password", "")
        if password:
            text = text.replace(password, "[hidden]")
        return text

    def interfaces(self) -> list[dict]:
        output = self._run(["nmcli", "--escape", "no", "-t", "-f", "DEVICE,TYPE,STATE", "device", "status"])
        result = []
        for line in output.splitlines():
            parts = line.split(":", 2)
            if len(parts) == 3:
                name, kind, state = parts
                if kind in ("wifi", "ethernet") and name != AP_INTERFACE and state != "unmanaged":
                    result.append({"name": name, "type": kind, "state": state})
        return result

    def _default_upstream(self) -> str:
        options = self.interfaces()
        connected = [item for item in options if item["state"] == "connected"]
        return (connected or options or [{"name": "wlp0s20f3"}])[0]["name"]

    @staticmethod
    def _pid_alive(pid: int) -> bool:
        try:
            raw = Path(f"/proc/{pid}/stat").read_text()
            return raw.rsplit(")", 1)[1].split()[0] != "Z"
        except (OSError, IndexError):
            return False

    @staticmethod
    def _is_create_ap(pid: int) -> bool:
        try:
            args = Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0")
            return any(Path(arg.decode(errors="replace")).name == "create_ap" for arg in args[:3])
        except OSError:
            return False

    def _runtime(self) -> Path | None:
        for directory in sorted(Path("/tmp").glob(f"create_ap.{AP_INTERFACE}.conf.*"), reverse=True):
            try:
                if directory.is_symlink() or directory.stat().st_uid != 0:
                    continue
                pid = int((directory / "pid").read_text().strip())
                if (directory / "wifi_iface").read_text().strip() != AP_INTERFACE:
                    continue
                if self._pid_alive(pid) and self._is_create_ap(pid):
                    return directory
            except (OSError, ValueError):
                continue
        return None

    def _active_pid(self) -> int | None:
        runtime = self._runtime()
        if runtime:
            return int((runtime / "pid").read_text().strip())
        try:
            pid = int(PID_FILE.read_text().strip())
            return pid if self._pid_alive(pid) and self._is_create_ap(pid) else None
        except (OSError, ValueError):
            return None

    def _ctrl(self, *args: str, runtime: Path | None = None) -> str:
        runtime = runtime or self._runtime()
        if not runtime:
            raise RuntimeError("The hotspot is not running.")
        socket_path = runtime / "hostapd_ctrl" / AP_INTERFACE
        try:
            if not stat.S_ISSOCK(socket_path.stat().st_mode):
                raise RuntimeError("Hotspot control socket is not ready.")
        except OSError as exc:
            raise RuntimeError("Hotspot control socket is not ready.") from exc
        result = self._run(["hostapd_cli", "-p", str(runtime / "hostapd_ctrl"), "-i", AP_INTERFACE, *args], timeout=4)
        if any(line.strip() in ("FAIL", "UNKNOWN COMMAND") for line in result.splitlines()):
            raise RuntimeError("The hotspot could not apply that operation. Refresh and try again.")
        return result.strip()

    def _persist(self) -> None:
        atomic_json(self.config_file, {**self.settings, "options": dict(self.options),
                                       "blocked": sorted(self.blocked)})

    def _write_deny_file(self) -> Path:
        target = self.state_dir / "denied_macs"
        with tempfile.NamedTemporaryFile("w", dir=self.state_dir, delete=False) as tmp:
            temporary = Path(tmp.name)
            os.chmod(temporary, 0o600)
            tmp.write("\n".join(sorted(self.blocked)) + "\n")
        os.replace(temporary, target)
        return target

    def _radio(self) -> str:
        wifi = [item["name"] for item in self.interfaces() if item["type"] == "wifi"]
        if self.settings["upstream"] in wifi:
            return self.settings["upstream"]
        if not wifi:
            raise RuntimeError("No Wi-Fi adapter found. Enable Wi-Fi and try again.")
        return wifi[0]

    def _preflight(self) -> tuple[str, int]:
        validate_settings(**self.settings)
        validate_options(**self.options)
        if not CREATE_AP.is_file():
            raise RuntimeError("Keep the bundled create_ap file beside hotspot_manager.py.")
        for command in ("hostapd", "hostapd_cli", "dnsmasq", "iptables", "iw", "nmcli"):
            if not shutil.which(command, path="/usr/sbin:/usr/bin:/sbin:/bin"):
                raise RuntimeError("Missing tools. Install: sudo apt install hostapd dnsmasq-base iw iptables network-manager")
        options = {item["name"]: item for item in self.interfaces()}
        source = options.get(self.settings["upstream"])
        if not source or source["state"] != "connected":
            raise RuntimeError("The selected Internet interface is disconnected. Connect it first.")
        radio = self._radio()
        info = self._run(["iw", "dev", radio, "info"])
        channel = re.search(r"\bchannel (\d+) \((\d+) MHz\)", info)
        if channel and int(channel[2]) >= 2500:
            raise RuntimeError("Your Wi-Fi is on 5 GHz. Connect to a 2.4 GHz Wi-Fi network first; this adapter must share one channel.")
        return radio, int(channel[1]) if channel else 6

    def _prepare_ap(self, radio: str) -> None:
        if not Path(f"/sys/class/net/{AP_INTERFACE}").exists():
            self._run(["iw", "dev", radio, "interface", "add", AP_INTERFACE, "type", "__ap"])
        else:
            existing = self._run(["iw", "dev", AP_INTERFACE, "info"])
            if "type AP" not in existing and "type managed" not in existing:
                raise RuntimeError("The ap0 interface is in use by a different application.")
            if re.search(r"\bchannel \d+", existing):
                raise RuntimeError("ap0 is active in another application. Stop that hotspot first.")
        self._run(["nmcli", "device", "set", AP_INTERFACE, "managed", "no"])
        self._run(["ip", "link", "set", "dev", AP_INTERFACE, "down"])
        self._run(["iw", "dev", AP_INTERFACE, "set", "type", "__ap"])

    def start(self) -> None:
        with self._lock:
            radio, channel = self._preflight()
            runtime = self._runtime()
            if runtime:
                try:
                    if parse_key_values(self._ctrl("status", runtime=runtime)).get("state") == "ENABLED":
                        ap_info = self._run(["iw", "dev", AP_INTERFACE, "info"])
                        if re.search(rf"\bchannel {channel} \(", ap_info):
                            return
                except RuntimeError:
                    pass
                self.stop()
            self._prepare_ap(radio)
            self._persist()
            deny_file = self._write_deny_file()
            fd = os.open(LOG_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w") as output:
                command = ["/bin/bash", str(CREATE_AP), "--no-virt", "--no-haveged", "--freq-band", "2.4",
                           "-c", str(channel), "-w", "2"]
                if self.options["hidden"]:
                    command.append("--hidden")
                if self.options["isolate_clients"]:
                    command.append("--isolate-clients")
                command.extend((AP_INTERFACE, self.settings["upstream"]))
                process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=output,
                                           stderr=subprocess.STDOUT, text=True, start_new_session=True,
                                           env={**os.environ, "LC_ALL": "C",
                                                "PATH": "/usr/sbin:/usr/bin:/sbin:/bin",
                                                "MINT_HOTSPOT_DENY_FILE": str(deny_file)})
            self._children[process.pid] = process
            PID_FILE.write_text(str(process.pid) + "\n")
            os.chmod(PID_FILE, 0o600)
            try:
                # Credentials travel over a private pipe, never in process arguments.
                process.stdin.write(self.settings["ssid"] + "\n" + self.settings["password"] + "\n")
                process.stdin.close()
                deadline = time.monotonic() + 35
                while time.monotonic() < deadline:
                    if process.poll() is not None:
                        raise RuntimeError("Hotspot failed to start. " + self.log_tail()[-1500:])
                    runtime = self._runtime()
                    if runtime:
                        try:
                            info = parse_key_values(self._ctrl("status", runtime=runtime))
                            if info.get("state") == "ENABLED":
                                self._last_error = ""
                                return
                        except RuntimeError:
                            pass
                    time.sleep(0.3)
                raise RuntimeError("Hotspot did not become ready. " + self.log_tail()[-1200:])
            except Exception as exc:
                self._last_error = self._redact(str(exc))
                try:
                    self.stop()
                except RuntimeError:
                    pass
                raise RuntimeError(self._last_error) from exc

    def stop(self) -> None:
        with self._lock:
            pid = self._active_pid()
            if pid:
                os.kill(pid, signal.SIGUSR1)
                deadline = time.monotonic() + 20
                while self._pid_alive(pid) and time.monotonic() < deadline:
                    time.sleep(0.15)
                if self._pid_alive(pid):
                    raise RuntimeError("Hotspot is still cleaning up. Wait a few seconds and try Stop again.")
                child = self._children.pop(pid, None)
                if child:
                    child.wait(timeout=2)
            if Path(f"/sys/class/net/{AP_INTERFACE}").exists():
                if not pid:
                    info = self._run(["iw", "dev", AP_INTERFACE, "info"])
                    if re.search(r"\bchannel \d+", info):
                        raise RuntimeError("ap0 is active in another application; it was left unchanged.")
                # Only delete our dedicated virtual interface, never the Internet interface.
                self._run(["iw", "dev", AP_INTERFACE, "del"])
            PID_FILE.unlink(missing_ok=True)

    def save_settings(self, ssid: str, password: str, upstream: str, *,
                      hidden=_UNSET_OPTION, isolate_clients=_UNSET_OPTION) -> None:
        new = validate_settings(ssid, password, upstream)
        with self._lock:
            old = self.settings.copy()
            old_options = self.options.copy()
            new_options = validate_options(
                hidden=old_options["hidden"] if hidden is _UNSET_OPTION else hidden,
                isolate_clients=old_options["isolate_clients"] if isolate_clients is _UNSET_OPTION else isolate_clients)
            if new == old and new_options == old_options:
                return
            active = self._active_pid() is not None
            self.settings, self.options = new, new_options
            if active:
                try:
                    self._preflight()
                except Exception:
                    self.settings, self.options = old, old_options
                    raise
                self.settings, self.options = old, old_options
                self.stop()
                self.settings, self.options = new, new_options
            try:
                self._persist()
                if active:
                    self.start()
            except Exception as original:
                self.settings, self.options = old, old_options
                restore_save_failed = False
                try:
                    self._persist()
                except Exception:
                    restore_save_failed = True
                if active:
                    try:
                        self.start()
                        restored = "Previous settings were restored and restarted."
                    except Exception:
                        restored = "Previous settings were restored. Start the hotspot after fixing the connection."
                else:
                    restored = "Previous settings were restored."
                if restore_save_failed:
                    restored += " Saving the restored settings also failed."
                detail = self._redact(str(original)).replace(new["password"], "[hidden]")
                raise RuntimeError(f"Could not apply the new settings. {restored} {detail}") from original

    def kick(self, mac: str) -> None:
        mac = validate_mac(mac)
        with self._lock:
            if mac not in {c["mac"] for c in self.snapshot()["clients"]}:
                raise ValueError("That device is no longer connected. Refresh the list.")
            self._ctrl("deauthenticate", mac, "reason=3")

    def block(self, mac: str) -> None:
        mac = validate_mac(mac)
        with self._lock:
            if mac in self.blocked:
                return
            if self._runtime():
                self._ctrl("deny_acl", "ADD_MAC", mac)
            self.blocked.append(mac)
            try:
                self._persist()
            except Exception:
                self.blocked.remove(mac)
                if self._runtime():
                    self._ctrl("deny_acl", "DEL_MAC", mac)
                raise

    def unblock(self, mac: str) -> None:
        mac = validate_mac(mac)
        with self._lock:
            if self._runtime():
                self._ctrl("deny_acl", "DEL_MAC", mac)
            old = self.blocked.copy()
            self.blocked = [item for item in self.blocked if item != mac]
            try:
                self._persist()
            except Exception:
                self.blocked = old
                if self._runtime() and mac in old:
                    self._ctrl("deny_acl", "ADD_MAC", mac)
                raise

    def _ipv4_address(self, interface: str):
        """Report a configured address; failure must not hide connected devices."""
        try:
            entries = json.loads(self._run(["ip", "-j", "-4", "address", "show", "dev", interface],
                                           check=False, timeout=2))
            for item in entries if isinstance(entries, list) else []:
                for address in item.get("addr_info", []):
                    if address.get("family") == "inet" and address.get("scope") == "global":
                        return clean_text(address.get("local", "")) or None
        except (RuntimeError, OSError, ValueError, TypeError, AttributeError):
            pass
        return None

    @staticmethod
    def _process_uptime(pid):
        """Linux process start time gives uptime without guessing from file dates."""
        try:
            stat_fields = Path(f"/proc/{int(pid)}/stat").read_text().rsplit(")", 1)[1].split()
            started = int(stat_fields[19]) / os.sysconf("SC_CLK_TCK")
            elapsed = float(Path("/proc/uptime").read_text().split()[0]) - started
            return int(elapsed) if math.isfinite(elapsed) and elapsed >= 0 else None
        except (OSError, ValueError, IndexError, TypeError, ZeroDivisionError):
            return None

    def snapshot(self) -> dict:
        with self._lock:
            result = {"running": False, "ssid": self.settings["ssid"], "channel": "—",
                      "upstream": self.settings["upstream"], "status": "Offline", "clients": [],
                      "clients_sampled_at": None,
                      "blocked": sorted(self.blocked), "notices": [],
                      "ap_interface": AP_INTERFACE, "ap_ipv4": None,
                      "source_channel": None, "source_frequency_mhz": None,
                      "radio_driver": None, "radio_interface": None,
                      "upstream_ipv4": None, "upstream_gateway": None,
                      "hotspot_uptime_seconds": None, "band": "2.4 GHz",
                      "security": "WPA2-Personal", "hidden": self.options["hidden"],
                      "isolate_clients": self.options["isolate_clients"]}
            runtime = self._runtime()
            if runtime:
                try:
                    status = parse_key_values(self._ctrl("status", runtime=runtime))
                    result["running"] = status.get("state") == "ENABLED"
                    result["status"] = "Online" if result["running"] else status.get("state", "Starting")
                    conf = parse_key_values((runtime / "hostapd.conf").read_text())
                    result["ssid"] = conf.get("ssid", result["ssid"])
                    result["channel"] = status.get("channel", conf.get("channel", "—"))
                    result["hidden"] = conf.get("ignore_broadcast_ssid") == "1"
                    result["isolate_clients"] = conf.get("ap_isolate") == "1"
                    if result["running"]:
                        ap_info = self._run(["iw", "dev", AP_INTERFACE, "info"])
                        if not re.search(r"\bchannel \d+ \(", ap_info):
                            result["running"] = False
                            result["status"] = "Needs restart"
                            result["notices"].append("Hotspot has no active radio channel. Connect to 2.4 GHz Wi-Fi, then press S to restart.")
                    if result["running"]:
                        station_output = self._run(["iw", "dev", AP_INTERFACE, "station", "dump"])
                        result["clients_sampled_at"] = time.monotonic()
                        stations = parse_stations(station_output)
                        leases = parse_leases((runtime / "dnsmasq.leases").read_text()) if (runtime / "dnsmasq.leases").exists() else {}
                        for client in stations:
                            client.update(leases.get(client["mac"], {}))
                        result["clients"] = stations
                        result["ap_ipv4"] = self._ipv4_address(AP_INTERFACE)
                        try:
                            result["hotspot_uptime_seconds"] = self._process_uptime((runtime / "pid").read_text().strip())
                        except OSError:
                            pass
                except (RuntimeError, OSError) as exc:
                    result["status"] = "Not ready"
                    result["notices"].append(self._redact(str(exc)))
            try:
                radio = self._radio()
                result["radio_interface"] = radio
                driver = Path("/sys/class/net") / radio / "device" / "driver"
                if driver.exists():
                    result["radio_driver"] = clean_text(driver.resolve().name)
                info = self._run(["iw", "dev", radio, "info"])
                match = re.search(r"\bchannel (\d+) \((\d+) MHz\)", info)
                if match and radio == self.settings["upstream"]:
                    result["source_channel"] = int(match[1])
                    result["source_frequency_mhz"] = int(match[2])
                if match and int(match[2]) >= 2500:
                    result["notices"].append("Wi-Fi is on 5 GHz. Connect to a 2.4 GHz network before starting this hotspot.")
            except RuntimeError as exc:
                result["notices"].append(str(exc))
            if self._last_error:
                result["notices"].append(self._last_error.splitlines()[0])
            result["upstream_ipv4"] = self._ipv4_address(self.settings["upstream"])
            try:
                routes = json.loads(self._run(["ip", "-j", "-4", "route", "show", "default", "dev",
                                               self.settings["upstream"]], check=False, timeout=2))
                for route in routes if isinstance(routes, list) else []:
                    if route.get("gateway"):
                        result["upstream_gateway"] = clean_text(route["gateway"])
                        break
            except (RuntimeError, OSError, ValueError, TypeError, AttributeError):
                pass
            return result

    def log_tail(self) -> str:
        try:
            lines = LOG_FILE.read_text(errors="replace").splitlines()[-45:]
            lines = [re.sub(r"(wpa_(?:passphrase|psk)=).*", r"\1[hidden]", line) for line in lines]
            return self._redact("\n".join(lines))
        except OSError:
            return "No hotspot log yet."


class DemoManager:
    """In-memory preview; never invokes system commands or writes settings."""
    def __init__(self):
        self.settings = {"ssid": "Z-Hotspot", "password": "DemoPassword82", "upstream": "wlp0s20f3"}
        self.options = validate_options()
        self.running = True
        self.blocked = []
        self.clients = [
            {"mac": "02:aa:bb:cc:dd:01", "ip": "192.168.12.20", "name": "Pixel-9",
             "signal": -43, "rx_bytes": 24700000, "tx_bytes": 73100000, "connected_seconds": 842,
             "inactive_ms": 18, "rx_link_mbps": 72.2, "tx_link_mbps": 72.2,
             "rx_packets": 18040, "tx_packets": 54300, "tx_retries": 23, "tx_failed": 0,
             "signal_average_dbm": -45, "download_bps": 890000.0, "upload_bps": 54000.0,
             "signal_percent": 100},
            {"mac": "02:aa:bb:cc:dd:02", "ip": "192.168.12.21", "name": "Living-room-TV",
             "signal": -58, "rx_bytes": 431000000, "tx_bytes": 12000000, "connected_seconds": 4012,
             "inactive_ms": 82, "rx_link_mbps": 65.0, "tx_link_mbps": 58.5,
             "rx_packets": 307000, "tx_packets": 10000, "tx_retries": 59, "tx_failed": 2,
             "signal_average_dbm": -60, "download_bps": 280000.0, "upload_bps": 129000.0,
             "signal_percent": 84},
            {"mac": "02:aa:bb:cc:dd:03", "ip": "192.168.12.22", "name": "Tablet",
             "signal": -66, "rx_bytes": 5800000, "tx_bytes": 900000, "connected_seconds": 173,
             "inactive_ms": 144, "rx_link_mbps": 26.0, "tx_link_mbps": 19.5,
             "rx_packets": 4700, "tx_packets": 900, "tx_retries": 16, "tx_failed": 1,
             "signal_average_dbm": -67, "download_bps": 70000.0, "upload_bps": 3000.0,
             "signal_percent": 68},
        ]

    def snapshot(self):
        return {"running": self.running, "ssid": self.settings["ssid"], "channel": 7,
                "upstream": self.settings["upstream"], "status": "Online" if self.running else "Offline",
                "clients": [dict(client) for client in self.clients] if self.running else [], "blocked": self.blocked.copy(),
                "ap_interface": AP_INTERFACE, "ap_ipv4": "192.168.12.1" if self.running else None,
                "source_channel": 7, "source_frequency_mhz": 2442,
                "radio_driver": "iwlwifi", "radio_interface": "wlp0s20f3",
                "upstream_ipv4": "192.168.1.42", "upstream_gateway": "192.168.1.1",
                "hotspot_uptime_seconds": 4258 if self.running else None,
                "band": "2.4 GHz", "security": "WPA2-Personal", **self.options,
                "notices": ["DEMO MODE — sample devices. Your network is unchanged."]}

    def interfaces(self):
        return [{"name": "wlp0s20f3", "type": "wifi", "state": "connected"},
                {"name": "enp0s31f6", "type": "ethernet", "state": "unavailable"}]

    def start(self): self.running = True
    def stop(self): self.running = False
    def save_settings(self, ssid, password, upstream, *, hidden=_UNSET_OPTION, isolate_clients=_UNSET_OPTION):
        settings = validate_settings(ssid, password, upstream)
        options = validate_options(hidden=self.options["hidden"] if hidden is _UNSET_OPTION else hidden,
                                   isolate_clients=self.options["isolate_clients"] if isolate_clients is _UNSET_OPTION else isolate_clients)
        self.settings, self.options = settings, options
    def kick(self, mac): self.clients = [c for c in self.clients if c["mac"] != validate_mac(mac)]
    def block(self, mac):
        mac = validate_mac(mac)
        if mac not in self.blocked: self.blocked.append(mac)
        self.kick(mac)
    def unblock(self, mac): self.blocked = [x for x in self.blocked if x != validate_mac(mac)]
    def log_tail(self): return "[demo] Hotspot enabled on channel 7\n[demo] 3 devices joined securely with WPA2."

import curses
import queue
import textwrap
import threading
import time
import unicodedata


def ui_clean(value):
    """Keep terminal escape sequences and control characters out of the display."""
    value = str(value if value is not None else "")
    return " ".join("".join(c if not unicodedata.category(c).startswith("C") else " "
                            for c in value).split())


def ui_redact(value, secrets):
    result = str(value if value is not None else "")
    for secret in sorted((str(item) for item in secrets if item), key=len, reverse=True):
        result = result.replace(secret, "[password hidden]")
    return result


def ui_put(win, y, x, value, color=0, width=None):
    height, columns = win.getmaxyx()
    if y < 0 or y >= height or x < 0 or x >= columns:
        return
    limit = max(0, columns - x - (1 if y == height - 1 else 0))
    if width is not None:
        limit = min(limit, max(0, width))
    if not limit:
        return
    try:
        win.addnstr(y, x, ui_clean(value), limit, color)
    except (curses.error, UnicodeError):
        pass


def ui_size_bytes(value):
    try:
        size = max(0.0, float(value or 0))
    except (ValueError, TypeError):
        return "--"
    for unit in ("B", "K", "M", "G", "T"):
        if size < 1024 or unit == "T":
            return ("%.0f%s" if unit == "B" else "%.1f%s") % (size, unit)
        size /= 1024


def ui_duration(value):
    try:
        seconds = max(0, int(value))
    except (ValueError, TypeError):
        return "--"
    if seconds >= 3600:
        return "%dh %dm" % (seconds // 3600, seconds % 3600 // 60)
    return "%dm %ds" % (seconds // 60, seconds % 60)


def ui_panel(win, y, x, height, width, title, colors):
    if height < 3 or width < 4:
        return
    ui_put(win, y, x, "+" + "-" * (width - 2) + "+", colors["border"])
    for row in range(y + 1, y + height - 1):
        ui_put(win, row, x, "|" + " " * (width - 2) + "|", colors["border"])
    ui_put(win, y + height - 1, x, "+" + "-" * (width - 2) + "+", colors["border"])
    ui_put(win, y, x + 3, " " + ui_clean(title) + " ", colors["accent"], width - 6)


def ui_modal_frame(screen, title, preferred_height, preferred_width, colors):
    screen_height, screen_width = screen.getmaxyx()
    height = max(1, min(preferred_height, screen_height))
    width = max(1, min(preferred_width, screen_width))
    win = curses.newwin(height, width, max(0, (screen_height - height) // 2),
                        max(0, (screen_width - width) // 2))
    win.keypad(True)
    win.timeout(150)
    try:
        win.bkgd(" ", colors["normal"])
    except curses.error:
        pass
    win.erase()
    ui_panel(win, 0, 0, height, width, title, colors)
    return win


def ui_confirm(screen, title, message, colors, yes_label="Continue"):
    while True:
        win = ui_modal_frame(screen, title, 10, 72, colors)
        height, width = win.getmaxyx()
        lines = textwrap.wrap(ui_clean(message), max(8, width - 8))
        for offset, line in enumerate(lines[:max(1, height - 6)]):
            ui_put(win, 2 + offset, 4, line, colors["normal"], width - 8)
        ui_put(win, height - 3, 4, "[Y] " + yes_label + "     [N / Esc / Q] Cancel", colors["accent"], width - 8)
        win.refresh()
        key = win.getch()
        if key in (ord("y"), ord("Y")):
            return True
        if key in (27, ord("n"), ord("N"), ord("q"), ord("Q")):
            return False
        if key == curses.KEY_RESIZE:
            screen.erase()
            screen.refresh()


def ui_message(screen, title, content, colors):
    offset = 0
    # Split lines first so log entries retain their useful structure.
    source = str(content if content is not None else "").splitlines() or ["No information available."]
    while True:
        height, width = screen.getmaxyx()
        win = ui_modal_frame(screen, title, min(26, height - 2), min(100, width - 4), colors)
        rows, cols = win.getmaxyx()
        lines = []
        for line in source:
            lines.extend(textwrap.wrap(ui_clean(line), max(8, cols - 8)) or [""])
        visible = max(1, rows - 5)
        offset = min(max(0, offset), max(0, len(lines) - visible))
        for number, line in enumerate(lines[offset:offset + visible]):
            ui_put(win, 2 + number, 4, line, colors["normal"], cols - 8)
        ui_put(win, rows - 2, 4, "Up/Down scroll   PgUp/PgDn page   Q / Esc / Enter close", colors["muted"], cols - 8)
        win.refresh()
        key = win.getch()
        if key in (27, 10, 13, ord("q"), ord("Q")):
            return
        if key in (curses.KEY_DOWN, ord("j")):
            offset += 1
        elif key in (curses.KEY_UP, ord("k")):
            offset -= 1
        elif key == curses.KEY_NPAGE:
            offset += visible
        elif key == curses.KEY_PPAGE:
            offset -= visible
        elif key == curses.KEY_HOME:
            offset = 0
        elif key == curses.KEY_END:
            offset = max(0, len(lines) - visible)
        elif key == curses.KEY_RESIZE:
            screen.erase()
            screen.refresh()


def ui_input(screen, title, label, initial, colors, secret=False, max_length=128):
    value = str(initial or "")
    cursor = len(value)
    while True:
        win = ui_modal_frame(screen, title, 10, 78, colors)
        height, width = win.getmaxyx()
        ui_put(win, 2, 4, label, colors["normal"], width - 8)
        available = max(1, width - 10)
        first = max(0, cursor - available + 1)
        shown = "*" * len(value) if secret else value
        ui_put(win, 4, 4, " " * max(1, width - 8), colors["selected"], width - 8)
        ui_put(win, 4, 5, shown[first:first + available], colors["selected"], available)
        ui_put(win, height - 3, 4, "Enter save   Esc cancel   Ctrl+U clear   Left/Right move", colors["muted"], width - 8)
        try:
            curses.curs_set(1)
            win.move(min(4, height - 1), min(width - 2, 5 + cursor - first))
        except curses.error:
            pass
        win.refresh()
        try:
            key = win.get_wch()
        except curses.error:
            continue
        if key in ("\n", "\r"):
            try:
                curses.curs_set(0)
            except curses.error:
                pass
            return value
        if key == "\x1b":
            try:
                curses.curs_set(0)
            except curses.error:
                pass
            return None
        if key in (curses.KEY_BACKSPACE, "\x7f", "\b"):
            if cursor:
                value = value[:cursor - 1] + value[cursor:]
                cursor -= 1
        elif key == curses.KEY_DC:
            value = value[:cursor] + value[cursor + 1:]
        elif key == curses.KEY_LEFT:
            cursor = max(0, cursor - 1)
        elif key == curses.KEY_RIGHT:
            cursor = min(len(value), cursor + 1)
        elif key in (curses.KEY_HOME, "\x01"):
            cursor = 0
        elif key in (curses.KEY_END, "\x05"):
            cursor = len(value)
        elif key == "\x15":
            value, cursor = "", 0
        elif key == curses.KEY_RESIZE:
            screen.erase()
            screen.refresh()
        elif isinstance(key, str) and key.isprintable() and not unicodedata.category(key).startswith("C"):
            if len(value) < max_length:
                value = value[:cursor] + key + value[cursor:]
                cursor += 1


def ui_choose_interface(screen, interfaces, current, colors):
    entries = [("", "Automatic: choose the connected Wi-Fi interface")]
    seen = set()
    for interface in interfaces:
        name = str(interface.get("name", ""))
        if name and name not in seen:
            entries.append((name, "%s  |  %s  |  %s" %
                            (ui_clean(name), ui_clean(interface.get("type", "--")),
                             ui_clean(interface.get("state", "--")))))
            seen.add(name)
    selected = next((i for i, row in enumerate(entries) if row[0] == current), 0)
    while True:
        win = ui_modal_frame(screen, "SOURCE INTERFACE", min(18, len(entries) + 6), 78, colors)
        height, width = win.getmaxyx()
        visible = max(1, height - 5)
        first = max(0, selected - visible + 1)
        for index, entry in enumerate(entries[first:first + visible], first):
            attribute = colors["selected"] if index == selected else colors["normal"]
            ui_put(win, 2 + index - first, 3, ("> " if index == selected else "  ") + entry[1], attribute, width - 6)
        ui_put(win, height - 2, 3, "Up/Down choose   Enter select   Q / Esc cancel", colors["muted"], width - 6)
        win.refresh()
        key = win.getch()
        if key in (27, ord("q"), ord("Q")):
            return None
        if key in (10, 13):
            return entries[selected][0]
        if key in (curses.KEY_UP, ord("k")):
            selected = max(0, selected - 1)
        elif key in (curses.KEY_DOWN, ord("j")):
            selected = min(len(entries) - 1, selected + 1)
        elif key == curses.KEY_RESIZE:
            screen.erase()
            screen.refresh()


def ui_edit_settings(screen, manager, snapshot, colors):
    settings = dict(manager.settings)
    ssid = str(settings.get("ssid", "Z-Hotspot"))
    password = str(settings.get("password", ""))
    upstream = str(settings.get("upstream", "") or "")
    while True:
        win = ui_modal_frame(screen, "HOTSPOT SETTINGS", 16, 78, colors)
        height, width = win.getmaxyx()
        ui_put(win, 2, 4, "[1] Network name     " + ssid, colors["normal"], width - 8)
        ui_put(win, 4, 4, "[2] Password         ********  (hidden)", colors["normal"], width - 8)
        ui_put(win, 6, 4, "[3] Source interface " + (upstream or "Automatic"), colors["normal"], width - 8)
        ui_put(win, 9, 4, "Settings are saved locally. An active hotspot will restart.", colors["muted"], width - 8)
        ui_put(win, 10, 4, "The password must be 8-63 characters.", colors["muted"], width - 8)
        ui_put(win, height - 3, 4, "1 / 2 / 3 edit   Enter apply   Q / Esc cancel", colors["accent"], width - 8)
        win.refresh()
        key = win.getch()
        if key in (27, ord("q"), ord("Q")):
            return None
        if key == ord("1"):
            value = ui_input(screen, "NETWORK NAME", "Enter the name shown to nearby devices:", ssid, colors, max_length=32)
            if value is not None:
                ssid = value
        elif key == ord("2"):
            value = ui_input(screen, "CHANGE PASSWORD", "Enter a new password (8-63 characters):", password, colors,
                             secret=True, max_length=63)
            if value is not None:
                password = value
        elif key == ord("3"):
            try:
                interfaces = manager.interfaces()
                value = ui_choose_interface(screen, interfaces, upstream, colors)
                if value is not None:
                    upstream = value
            except Exception as error:
                ui_message(screen, "INTERFACES UNAVAILABLE", ui_redact(error, (password, settings.get("password", ""))), colors)
        elif key in (10, 13):
            previous = (str(settings.get("ssid", "Z-Hotspot")), str(settings.get("password", "")),
                        str(settings.get("upstream", "") or ""))
            if (ssid, password, upstream) == previous:
                return None
            if not ssid or len(ssid.encode("utf-8")) > 32:
                ui_message(screen, "CHECK NETWORK NAME", "Use a network name containing 1-32 UTF-8 bytes.", colors)
                continue
            if not 8 <= len(password) <= 63:
                ui_message(screen, "CHECK PASSWORD", "Use a password containing 8-63 characters.", colors)
                continue
            clients = snapshot.get("clients", [])
            if snapshot.get("running") and clients:
                if not ui_confirm(screen, "RESTART HOTSPOT?",
                                  "Applying these settings will briefly disconnect %d connected device(s). Devices may need the new password to reconnect." % len(clients),
                                  colors, "Save and restart"):
                    continue
            return ssid, password, upstream
        elif key == curses.KEY_RESIZE:
            screen.erase()
            screen.refresh()


def ui_choose_blocked(screen, blocked, colors):
    if not blocked:
        ui_message(screen, "BLOCKED DEVICES", "No devices are blocked. Select a connected device and press B to block it.", colors)
        return None
    selected = 0
    while True:
        win = ui_modal_frame(screen, "BLOCKED DEVICES", min(22, len(blocked) + 8), 76, colors)
        height, width = win.getmaxyx()
        ui_put(win, 2, 4, "A blocked device stays denied until you unblock it.", colors["muted"], width - 8)
        visible = max(1, height - 7)
        first = max(0, selected - visible + 1)
        for index, mac in enumerate(blocked[first:first + visible], first):
            ui_put(win, 4 + index - first, 4, ("> " if index == selected else "  ") + ui_clean(mac),
                   colors["selected"] if index == selected else colors["normal"], width - 8)
        ui_put(win, height - 2, 4, "Up/Down select   Enter unblock   Q / Esc close", colors["accent"], width - 8)
        win.refresh()
        key = win.getch()
        if key in (27, ord("q"), ord("Q")):
            return None
        if key in (10, 13):
            return str(blocked[selected])
        if key in (curses.KEY_UP, ord("k")):
            selected = max(0, selected - 1)
        elif key in (curses.KEY_DOWN, ord("j")):
            selected = min(len(blocked) - 1, selected + 1)
        elif key == curses.KEY_RESIZE:
            screen.erase()
            screen.refresh()


def ui_draw(screen, snapshot, selected, colors, message, message_error, busy, demo):
    screen.erase()
    height, width = screen.getmaxyx()
    if height < 22 or width < 76:
        ui_put(screen, 1, 2, "HOTSPOT CONTROL", colors["accent"])
        ui_put(screen, 3, 2, "Resize your terminal to at least 76 columns x 22 rows.", colors["normal"])
        ui_put(screen, 5, 2, "Current size: %d x %d" % (width, height), colors["muted"])
        ui_put(screen, 7, 2, "Q to quit. Quitting leaves the hotspot running.", colors["muted"])
        if busy:
            ui_put(screen, 9, 2, busy + "... Please wait.", colors["warning"])
        screen.refresh()
        return
    now = time.strftime("%H:%M:%S")
    ui_put(screen, 0, 2, "NETWORK / HOTSPOT", colors["muted"])
    ui_put(screen, 0, max(24, width - 29), ("DEMO MODE  /  " if demo else "LOCAL CONTROL  /  ") + now, colors["muted"])
    ui_put(screen, 1, 2, "HOTSPOT CONTROL", colors["title"])
    ui_put(screen, 1, 20, "Your Wi-Fi, at a glance.", colors["muted"])
    ui_put(screen, 2, 2, "-" * (width - 4), colors["border"])
    running = bool(snapshot.get("running"))
    ui_put(screen, 3, 2, " ONLINE " if running else " OFFLINE ", colors["online"] if running else colors["offline"])
    ui_put(screen, 3, 13, snapshot.get("ssid", "Z-Hotspot"), colors["title"], width - 16)
    upstream = ui_clean(snapshot.get("upstream") or "Automatic")
    channel = ui_clean(snapshot.get("channel") or "--")
    ui_put(screen, 4, 2, "SOURCE  %s    CHANNEL  %s    SECURITY  WPA2-Personal" % (upstream, channel), colors["normal"], width - 4)
    ui_put(screen, 5, 2, "PASSWORD  ********    [E] change settings    BLOCKED  %d" % len(snapshot.get("blocked", [])), colors["muted"], width - 4)
    notices = snapshot.get("notices", [])
    if isinstance(notices, str):
        notices = [notices]
    status = ui_clean(snapshot.get("status", ""))
    if notices:
        notice = "  |  ".join(ui_clean(item) for item in notices if item)
        notice_lines = textwrap.wrap(notice, width - 6)
        for index, line in enumerate(notice_lines[:2]):
            ui_put(screen, 6 + index, 2, ("! " if index == 0 else "  ") + line, colors["warning"], width - 4)
    else:
        for index, line in enumerate(textwrap.wrap(status, width - 4)[:2]):
            ui_put(screen, 6 + index, 2, line, colors["normal"], width - 4)
    clients = snapshot.get("clients", [])
    table_top, table_height = 8, height - 14
    ui_panel(screen, table_top, 2, table_height, width - 4, "CONNECTED DEVICES / %02d" % len(clients), colors)
    content_width = width - 10
    wide = width >= 110
    name_width = max(8, content_width - (79 if wide else 51))
    if wide:
        headings = "  %-*s  %-15s  %-17s  %7s  %8s  %8s  %9s" % (name_width, "DEVICE", "IP ADDRESS", "MAC ADDRESS", "SIGNAL", "RX", "TX", "CONNECTED")
    else:
        headings = "  %-*s  %-15s  %-17s  %7s" % (name_width, "DEVICE", "IP ADDRESS", "MAC ADDRESS", "SIGNAL")
    ui_put(screen, table_top + 1, 4, headings, colors["muted"], content_width + 2)
    ui_put(screen, table_top + 2, 4, "-" * content_width, colors["border"], content_width)
    visible = max(1, table_height - 4)
    first = max(0, selected - visible + 1)
    for index, client in enumerate(clients[first:first + visible], first):
        name = ui_clean(client.get("name") or "Unknown device")[:name_width]
        ip = ui_clean(client.get("ip") or "Pending DHCP")[:15]
        mac = ui_clean(client.get("mac") or "--")[:17]
        signal = str(client.get("signal", "--"))
        try:
            signal = "%d dBm" % int(float(signal))
        except (ValueError, TypeError):
            signal = ui_clean(signal)[:7]
        prefix = "> " if index == selected else "  "
        row = prefix + "%-*s  %-15s  %-17s  %7s" % (name_width, name, ip, mac, signal)
        if wide:
            row += "  %8s  %8s  %9s" % (ui_size_bytes(client.get("rx_bytes")), ui_size_bytes(client.get("tx_bytes")),
                                          ui_duration(client.get("connected_seconds")))
        ui_put(screen, table_top + 3 + index - first, 4, row.ljust(content_width),
               colors["selected"] if index == selected else colors["normal"], content_width)
    if not clients:
        ui_put(screen, table_top + 3, 6, "No connected devices." if running else "Start the hotspot to connect your devices.", colors["muted"], width - 12)
    if len(clients) > visible:
        ui_put(screen, table_top + table_height - 1, max(7, width - 28), " %d-%d of %d " % (first + 1, min(len(clients), first + visible), len(clients)), colors["muted"], 25)
    if busy:
        frame = "|/-\\"[int(time.monotonic() * 8) % 4]
        ui_put(screen, height - 5, 2, "%s  %s..." % (frame, busy), colors["warning"], width - 4)
    else:
        ui_put(screen, height - 5, 2, message, colors["error"] if message_error else colors["normal"], width - 4)
    ui_put(screen, height - 4, 2, "-" * (width - 4), colors["border"])
    ui_put(screen, height - 3, 2, "S Start   X Stop   E Settings   R Refresh   Up/Down Select", colors["accent"], width - 4)
    ui_put(screen, height - 2, 2, "D Disconnect   B Block   U Blocked   L Logs   H Help   Q Quit", colors["accent"], width - 4)
    ui_put(screen, height - 1, 2, "Refreshes every 4s. Disconnect allows rejoin. Quit leaves the hotspot running.", colors["muted"], width - 4)
    screen.refresh()


def ui_main(screen, manager, demo=False):
    try:
        curses.curs_set(0)
    except curses.error:
        pass
    screen.keypad(True)
    screen.timeout(100)
    try:
        curses.start_color()
    except curses.error:
        pass
    try:
        curses.use_default_colors()
    except curses.error:
        pass
    palette = ((curses.COLOR_WHITE, -1), (curses.COLOR_CYAN, -1), (curses.COLOR_BLUE, -1),
               (curses.COLOR_BLACK, curses.COLOR_CYAN), (curses.COLOR_GREEN, -1),
               (curses.COLOR_YELLOW, -1), (curses.COLOR_RED, -1))
    for index, (fg, bg) in enumerate(palette, 1):
        try:
            curses.init_pair(index, fg, bg)
        except curses.error:
            pass
    def attr(index, extra=0):
        return (curses.color_pair(index) if curses.has_colors() else 0) | extra
    colors = {"normal": attr(1), "accent": attr(2, curses.A_BOLD), "title": attr(1, curses.A_BOLD),
              "border": attr(3), "selected": attr(4, curses.A_BOLD) if curses.has_colors() else curses.A_REVERSE,
              "online": attr(5, curses.A_BOLD), "offline": attr(6, curses.A_BOLD), "warning": attr(6),
              "error": attr(7, curses.A_BOLD), "muted": attr(1, curses.A_DIM)}
    snapshot = {"running": False, "ssid": manager.settings.get("ssid", "Z-Hotspot"),
                "upstream": manager.settings.get("upstream", ""), "clients": [], "blocked": [],
                "status": "Reading hotspot status..."}
    selected, selected_mac = 0, None
    message, message_error = "Ready. Press H for help.", False
    refreshed, busy = 0.0, ""
    results = queue.Queue()
    known_passwords = {str(manager.settings.get("password", ""))}

    def dispatch(label, action, success):
        nonlocal busy, message, message_error
        busy, message, message_error = label, "", False
        def worker():
            try:
                result = action()
                results.put((True, success, result))
            except Exception as error:
                results.put((False, str(error), None))
        threading.Thread(target=worker, name="hotspot-action", daemon=True).start()

    while True:
        try:
            ok, outcome, result = results.get_nowait()
            completed = busy
            busy = ""
            outcome = ui_redact(outcome, known_passwords)
            message, message_error = ui_clean(outcome), not ok
            refreshed = 0.0
            if ok and completed == "Reading logs":
                ui_message(screen, "RECENT LOGS", ui_redact(result or "No log entries yet.", known_passwords), colors)
            elif not ok:
                ui_message(screen, "ACTION FAILED", outcome, colors)
        except queue.Empty:
            pass
        if not busy and time.monotonic() - refreshed >= 4:
            try:
                fresh = manager.snapshot()
                if not isinstance(fresh, dict):
                    raise RuntimeError("The hotspot status returned an invalid response.")
                snapshot = fresh
                clients = snapshot.get("clients", [])
                if selected_mac:
                    selected = next((i for i, c in enumerate(clients) if c.get("mac") == selected_mac),
                                    min(selected, max(0, len(clients) - 1)))
                else:
                    selected = min(selected, max(0, len(clients) - 1))
                selected_mac = clients[selected].get("mac") if clients else None
            except Exception as error:
                snapshot["notices"] = ["Status unavailable: " + ui_clean(error)]
            refreshed = time.monotonic()
        ui_draw(screen, snapshot, selected, colors, message, message_error, busy, demo)
        key = screen.getch()
        if key < 0:
            continue
        if key == curses.KEY_RESIZE:
            screen.erase()
            continue
        if busy:
            continue
        if key in (ord("q"), ord("Q")):
            return
        height, width = screen.getmaxyx()
        if height < 22 or width < 76:
            continue
        clients = snapshot.get("clients", [])
        if key in (curses.KEY_UP, ord("k")):
            selected = max(0, selected - 1)
            selected_mac = clients[selected].get("mac") if clients else None
        elif key in (curses.KEY_DOWN, ord("j")):
            selected = min(max(0, len(clients) - 1), selected + 1)
            selected_mac = clients[selected].get("mac") if clients else None
        elif key in (ord("r"), ord("R")):
            refreshed = 0.0
            message, message_error = "Refreshing status...", False
        elif key in (ord("s"), ord("S")):
            if snapshot.get("running"):
                message, message_error = "The hotspot is already running.", False
            else:
                dispatch("Starting hotspot", manager.start, "Hotspot started. Your devices can now connect.")
        elif key in (ord("x"), ord("X")):
            if not snapshot.get("running") or not clients or ui_confirm(screen, "STOP HOTSPOT?", "Stopping the hotspot will disconnect %d connected device(s)." % len(clients), colors, "Stop hotspot"):
                dispatch("Stopping hotspot", manager.stop, "Hotspot stopped.")
        elif key in (ord("e"), ord("E")):
            settings = ui_edit_settings(screen, manager, snapshot, colors)
            if settings is not None:
                known_passwords.add(settings[1])
                dispatch("Applying settings", lambda values=settings: manager.save_settings(*values), "Settings saved.")
        elif key in (ord("d"), ord("D"), ord("b"), ord("B")):
            if not clients:
                message, message_error = "Select a connected device first.", False
                continue
            client = clients[selected]
            mac = str(client.get("mac", ""))
            device = "%s (%s)" % (ui_clean(client.get("name") or "Device"), ui_clean(mac))
            if key in (ord("d"), ord("D")):
                if ui_confirm(screen, "DISCONNECT DEVICE?", device + " will be disconnected. It can reconnect using the saved password. Choose Block to prevent it from rejoining.", colors, "Disconnect"):
                    dispatch("Disconnecting device", lambda target=mac: manager.kick(target), "Device disconnected. It may reconnect.")
            elif ui_confirm(screen, "BLOCK DEVICE?", device + " will be disconnected and blocked. It stays blocked until you remove it from the blocked list.", colors, "Block device"):
                dispatch("Blocking device", lambda target=mac: manager.block(target), "Device blocked. Use U to manage blocked devices.")
        elif key in (ord("u"), ord("U")):
            mac = ui_choose_blocked(screen, list(snapshot.get("blocked", [])), colors)
            if mac:
                dispatch("Unblocking device", lambda target=mac: manager.unblock(target), "Device unblocked. It can reconnect with the hotspot password.")
        elif key in (ord("l"), ord("L")):
            dispatch("Reading logs", manager.log_tail, "Logs loaded.")
        elif key in (ord("h"), ord("H"), ord("?")):
            ui_message(screen, "KEYBOARD GUIDE", "\n".join([
                "S  Start the hotspot.    X  Stop the hotspot.",
                "E  Change the network name, hidden password, or source Wi-Fi interface.",
                "R  Refresh now. The dashboard also refreshes every 4 seconds.",
                "Up / Down or J / K  Select a connected device.",
                "D  Disconnect the selected device. It can reconnect immediately.",
                "B  Block the selected device until you explicitly unblock it.",
                "U  Open the blocked list; select a MAC and press Enter to unblock.",
                "L  Show recent service logs. H or ? opens this guide.",
                "Q  Close this program. The hotspot keeps running until you stop it.",
                "", "In dialogs, Q or Esc closes the dialog. In text fields use Esc to cancel; Q is typed normally.",
                "Password entry stays masked. Ctrl+U clears the text field.",
                "IP addresses and device names appear when DHCP information is available.",
                "Blocking uses a device's current Wi-Fi MAC address. A device using a different or randomized MAC appears as a new device.",
            ]), colors)


def run_tui(manager, demo=False):
    """Run the English terminal dashboard without stopping the hotspot on exit."""
    try:
        curses.wrapper(ui_main, manager, demo)
    except KeyboardInterrupt:
        pass



def main() -> int:
    parser = argparse.ArgumentParser(description="English terminal manager for a Linux Mint Wi-Fi hotspot.")
    parser.add_argument("--demo", action="store_true", help="preview with sample devices; no administrator access or network changes")
    parser.add_argument("--status-json", action="store_true", help="print real hotspot state as JSON and exit (requires root)")
    args = parser.parse_args()
    if not args.demo and os.geteuid() != 0:
        if not sys.stdin.isatty():
            print("Run this program in a terminal with sudo, or use --demo for a preview.", file=sys.stderr)
            return 2
        print("Z Hotspot needs administrator access to control your Wi-Fi adapter.", flush=True)
        os.execv("/usr/bin/sudo", ["sudo", "--", sys.executable, str(Path(__file__).resolve()), *sys.argv[1:]])
    os.umask(0o077)
    try:
        manager = DemoManager() if args.demo else HotspotManager()
        if args.status_json:
            print(json.dumps(manager.snapshot(), ensure_ascii=False, indent=2))
            return 0
        if not sys.stdin.isatty() or not sys.stdout.isatty():
            print("Open a terminal and run this .py file there. Use --status-json for script output.", file=sys.stderr)
            return 2
        lock_handle = None
        if not args.demo:
            lock_handle = open("/run/mint-hotspot-manager.lock", "w")
            try:
                fcntl.flock(lock_handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                print("Z Hotspot is already open in another terminal.", file=sys.stderr)
                return 2
        run_tui(manager, demo=args.demo)
        if not args.demo:
            print("Z Hotspot closed. Any running hotspot stays on; reopen the app to stop it.")
        return 0
    except KeyboardInterrupt:
        print("\nZ Hotspot closed. Any running hotspot stays on.")
        return 0
    except (RuntimeError, ValueError, OSError, curses.error) as exc:
        print("Z Hotspot: " + clean_text(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
