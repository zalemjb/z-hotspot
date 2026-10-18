#!/usr/bin/env python3
"""Unprivileged, opt-in file sharing bound to one hotspot IPv4 address.

No discovery, directory listing, privileged helper, or third-party package is
used. Only selected regular files are exposed. Receiving files is disabled
unless the caller explicitly supplies an upload directory.
"""

from __future__ import annotations

from contextlib import contextmanager
from collections import deque
from dataclasses import dataclass
from email.utils import formatdate
import hmac
import html
import ipaddress
import json
import os
from pathlib import Path
import re
import secrets
import socket
import stat
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import quote, unquote, urlsplit


MAX_UPLOAD_BYTES = 2 * 1024 * 1024 * 1024
CHUNK_SIZE = 128 * 1024


@dataclass(frozen=True)
class _SharedFile:
    identifier: str
    path: Path
    name: str
    size: int
    device: int
    inode: int


def _human_size(value: int) -> str:
    number = float(value)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if number < 1024 or unit == "TB":
            return f"{number:.0f} {unit}" if unit == "B" else f"{number:.1f} {unit}"
        number /= 1024
    return str(value)


def _safe_filename(value: str) -> str:
    """Keep a portable basename, including when a client sends Windows paths."""
    name = value.replace("\\", "/").rsplit("/", 1)[-1]
    name = "".join(c for c in name if c.isprintable() and c not in '/\\\x00')
    name = name.strip(" .")
    name = name.encode("utf-8")[:180].decode("utf-8", errors="ignore").strip(" .")
    return name or "upload.bin"


def _browser_hint(user_agent: str) -> str:
    """A coarse browser report, never an assertion of a client's identity."""
    agent = user_agent[:1024].lower()
    platform = ("iPhone" if "iphone" in agent else "iPad" if "ipad" in agent
                else "Samsung / Android" if "android" in agent and
                ("samsungbrowser" in agent or "sm-" in agent)
                else "Android" if "android" in agent else "Mac" if "macintosh" in agent
                else "Windows" if "windows" in agent else "Linux" if "linux" in agent
                else "Device")
    browser = ("Samsung Internet" if "samsungbrowser" in agent
               else "Edge" if "edg/" in agent else "Firefox" if "firefox" in agent or "fxios" in agent
               else "Chrome" if "chrome" in agent or "crios" in agent
               else "Safari" if "safari" in agent else "Browser")
    return f"{platform} · {browser}"


class _HTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    block_on_close = False
    allow_reuse_address = True
    request_queue_size = 16

    def __init__(self, address, owner):
        self.owner = owner
        super().__init__(address, _Handler)

    def handle_error(self, request, client_address):
        # A browser leaving a page or an interrupted transfer is not a UI error.
        pass


class FileShareServer:
    """Share explicit files with clients on a specified hotspot network.

    ``start()`` returns this object. ``url`` is populated after starting; pass
    port=0 to request a free port. ``stop()`` is idempotent and stops transfers.
    The object can be started again; each start generates a fresh access token.
    """

    def __init__(self, files: list[Path], bind_address: str, network: str,
                 port: int = 8765, upload_directory: Path | None = None):
        address = ipaddress.IPv4Address(bind_address)
        subnet = ipaddress.IPv4Network(network, strict=False)
        if address.is_unspecified or address.is_multicast or address not in subnet:
            raise ValueError("Choose the hotspot's own IPv4 address and subnet.")
        if address in (subnet.network_address, subnet.broadcast_address) and subnet.prefixlen < 31:
            raise ValueError("The sharing address must be a host address.")
        if not isinstance(port, int) or isinstance(port, bool) or not 0 <= port <= 65535:
            raise ValueError("Choose a port between 0 and 65535.")
        self.bind_address = str(address)
        self.network = subnet
        self.port = port
        self.upload_directory = None
        self._directory_identity = None
        if upload_directory is not None:
            directory = Path(upload_directory).expanduser().absolute()
            descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            try:
                info = os.fstat(descriptor)
                self._directory_identity = (info.st_dev, info.st_ino)
            finally:
                os.close(descriptor)
            self.upload_directory = directory
        self._files = {}
        seen = set()
        for value in files:
            path = Path(value).expanduser().absolute()
            descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            try:
                info = os.fstat(descriptor)
                if not stat.S_ISREG(info.st_mode):
                    raise ValueError(f"Select a regular file: {path.name}")
                if (info.st_dev, info.st_ino) in seen:
                    continue
                seen.add((info.st_dev, info.st_ino))
                identifier = secrets.token_urlsafe(15)
                self._files[identifier] = _SharedFile(identifier, path, path.name, info.st_size,
                                                       info.st_dev, info.st_ino)
            finally:
                os.close(descriptor)
        if not self._files and self.upload_directory is None:
            raise ValueError("Select at least one file or a receiving folder.")
        self._lock = threading.RLock()
        self._stopping = threading.Event()
        self._server = None
        self._thread = None
        self._token = ""
        self._url = ""
        self._connections = set()
        self._workers = set()
        self._totals = {"downloads": 0, "uploads": 0, "bytes_sent": 0, "bytes_received": 0}
        self._transfers = deque(maxlen=16)

    @property
    def url(self) -> str:
        return self._url

    def start(self) -> FileShareServer:
        if os.geteuid() == 0:
            raise PermissionError("File sharing must run as your regular user, without sudo.")
        with self._lock:
            if self._server is not None:
                return self
            self._stopping.clear()
            self._token = secrets.token_urlsafe(32)
            server = _HTTPServer((self.bind_address, self.port), self)
            self._server = server
            self._url = f"http://{self.bind_address}:{server.server_address[1]}/{self._token}/"
            self._thread = threading.Thread(
                target=server.serve_forever, kwargs={"poll_interval": 0.1},
                name="hotspot-file-sharing", daemon=True)
            self._thread.start()
        return self

    def stop(self) -> None:
        with self._lock:
            server, thread = self._server, self._thread
            self._stopping.set()
            self._server = None
            self._thread = None
            self._url = ""
        if server is None:
            return
        server.shutdown()
        with self._lock:
            connections = tuple(self._connections)
            workers = tuple(self._workers)
        for connection in connections:
            try:
                connection.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                connection.close()
            except OSError:
                pass
        server.server_close()
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=2)
        deadline = time.monotonic() + 2
        for worker in workers:
            if worker is not threading.current_thread():
                worker.join(timeout=max(0, deadline - time.monotonic()))

    def stats(self) -> dict:
        with self._lock:
            return {**self._totals, "active_requests": len(self._connections),
                    "files": len(self._files), "receiving": self.upload_directory is not None,
                    "recent_transfers": [dict(item) for item in reversed(self._transfers)],
                    "running": self._server is not None and not self._stopping.is_set()}

    def _transfer_start(self, direction, name, total, address, user_agent):
        transfer = {"direction": direction, "name": name, "total_bytes": total,
                    "transferred_bytes": 0, "status": "Transferring", "started_at": time.time(),
                    "client_address": address, "browser_hint": _browser_hint(user_agent),
                    "source": "Browser report; device identity is not verified"}
        with self._lock:
            self._transfers.append(transfer)
        return transfer

    def _transfer_progress(self, transfer, amount):
        with self._lock:
            transfer["transferred_bytes"] += amount
            key = "bytes_sent" if transfer["direction"] == "download" else "bytes_received"
            self._totals[key] += amount

    def _transfer_finish(self, transfer, success):
        with self._lock:
            transfer["status"] = ("Sent to browser" if transfer["direction"] == "download"
                                  else "Saved to computer") if success else "Interrupted / failed"
            transfer["finished_at"] = time.time()
            if success:
                self._totals["downloads" if transfer["direction"] == "download" else "uploads"] += 1

    def _increment(self, **values) -> None:
        with self._lock:
            for key, value in values.items():
                self._totals[key] += value

    def _client_allowed(self, address: str) -> bool:
        try:
            return ipaddress.IPv4Address(address) in self.network
        except ValueError:
            return False

    @contextmanager
    def _open_file(self, shared: _SharedFile):
        descriptor = os.open(shared.path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        try:
            info = os.fstat(descriptor)
            if not stat.S_ISREG(info.st_mode) or (info.st_dev, info.st_ino) != (shared.device, shared.inode):
                raise OSError("The selected file has been replaced. Select it again.")
            yield descriptor, info
        finally:
            os.close(descriptor)

    @contextmanager
    def _open_receiving_directory(self):
        if self.upload_directory is None:
            raise PermissionError("Receiving files is disabled.")
        descriptor = os.open(self.upload_directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            info = os.fstat(descriptor)
            if (info.st_dev, info.st_ino) != self._directory_identity:
                raise OSError("The receiving folder has been replaced. Select it again.")
            yield descriptor
        finally:
            os.close(descriptor)

    def _page(self, user_agent="") -> bytes:
        cards = "".join(
            '<a class="file" download="' + html.escape(entry.name, quote=True) + '" href="file/' + entry.identifier + '"><span class="fileicon">&#8595;</span>'
            '<span class="filename">' + html.escape(entry.name) + '<small>' + _human_size(entry.size)
            + '</small></span><span class="download">Download &#8599;</span></a>'
            for entry in self._files.values()) or '<p class="empty">No files have been selected for download.</p>'
        receiving = """
        <section class="card"><div class="sectiontitle"><h2>Send to this computer</h2><span class="tag">Receiving enabled</span></div>
        <p>Choose photos or files from your device. Keep this page open while sending. Each file can be up to 2 GB.</p>
        <label class="picker" for="uploads">Choose files to send <span>&#8593;</span></label>
        <input id="uploads" class="visuallyhidden" type="file" multiple><div id="uploadlist" aria-live="polite"></div>
        <noscript><p>Enable JavaScript to send files. Downloading files still works without it.</p></noscript>
        <script>
        const picker = document.getElementById('uploads');
        picker.addEventListener('change', async () => {
          const files=Array.from(picker.files); picker.disabled=true;
          for (const file of files) {
            const row = document.createElement('div'); row.className='uploadrow';
            const text = document.createElement('span'); text.textContent=file.name;
            const status = document.createElement('small'); status.textContent='Waiting';
            const progress = document.createElement('progress'); progress.max=100; progress.value=0;
            row.append(text,status,progress); document.getElementById('uploadlist').append(row);
            if(file.size>2147483648){status.textContent='Too large (2 GB maximum)';continue;}
            await new Promise(resolve => {
              const request=new XMLHttpRequest(); request.open('POST','upload'); request.timeout=0;
              request.setRequestHeader('Content-Type','application/octet-stream');
              request.setRequestHeader('X-Filename',encodeURIComponent(file.name));
              request.upload.onprogress=event => { if(event.lengthComputable){ progress.value=event.loaded/event.total*100; status.textContent=Math.round(progress.value)+'%'; }};
              request.onload=()=>{ try { const result=JSON.parse(request.responseText); status.textContent=request.status===201?'Saved to computer':result.error||'Upload failed'; if(request.status===201)progress.value=100; }catch(e){status.textContent='Upload failed';} resolve(); };
              request.onerror=()=>{status.textContent='Connection lost';resolve();};
              request.onabort=()=>{status.textContent='Cancelled';resolve();};
              status.textContent='Sending'; request.send(file);
            });
          } picker.value=''; picker.disabled=false;
        });
        </script></section>""" if self.upload_directory is not None else """
        <div class="readonly">Downloads only &middot; Receiving files is turned off.</div>"""
        iphone = "iphone" in user_agent.lower() or "ipad" in user_agent.lower()
        save_hint = ("On iPhone: tap a file, confirm Download, then open Safari's Downloads or Files → Downloads. "
                     "Photos stay in Files until you choose Share → Save Image / Save Video."
                     if iphone else "Tap a file and confirm your browser's download prompt. "
                     "On Samsung: open My Files → Downloads. On a laptop: open the browser's Downloads.")
        return ("""<!doctype html><html lang="en"><meta charset="utf-8">
        <meta name="viewport" content="width=device-width,initial-scale=1"><title>Z Hotspot · Files</title>
        <style>
        *{box-sizing:border-box}body{margin:0;background:#0c111b;color:#f4f6fa;font:15px system-ui,-apple-system,sans-serif;line-height:1.5}
        main{max-width:840px;margin:0 auto;padding:48px 24px}header{display:flex;align-items:center;gap:14px;margin-bottom:38px}
        .logo{width:49px;height:49px;display:grid;place-items:center;background:#58dcc1;border-radius:16px;color:#102a26;font-size:29px;font-weight:700}
        h1{font-size:24px;letter-spacing:-.7px;margin:0}header p{margin:0;color:#8e9bb0;font-size:13px}.eyebrow{color:#58dcc1;letter-spacing:2px;font-size:11px;font-weight:700}
        .intro h2{font-size:34px;letter-spacing:-1.2px;margin:9px 0}.intro p{color:#94a0b5;max-width:540px;margin:0 0 28px}
        .card{padding:24px;background:#131c2a;border:1px solid #263247;border-radius:20px;margin:18px 0}.sectiontitle{display:flex;align-items:center;justify-content:space-between;gap:10px}
        h2{font-size:17px;margin:0 0 14px}.sectiontitle h2{margin:0}.tag{color:#6fe6ca;background:#173b36;border-radius:30px;padding:4px 9px;font-size:11px;white-space:nowrap}
        .files{margin-top:18px}.file{display:flex;gap:14px;align-items:center;text-decoration:none;color:inherit;padding:16px 0;border-top:1px solid #263247}.file:first-child{border:0}.file:hover .filename{color:#72e6ce}
        .fileicon{display:grid;place-items:center;flex-shrink:0;width:42px;height:46px;border:1px solid #35455e;border-radius:10px;color:#72e6ce;font-size:22px}
        .filename{flex:1;min-width:0;overflow-wrap:anywhere;font-weight:600}.filename small{display:block;color:#8897ad;font-size:12px;font-weight:400;margin-top:3px}.download{font-size:12px;color:#79e7d0;white-space:nowrap}
        section p,.empty{color:#94a0b5;font-size:13px}.picker{display:flex;align-items:center;justify-content:space-between;cursor:pointer;border:1px dashed #40685f;background:#142e2b;border-radius:12px;padding:19px;color:#83e8d2;margin-top:20px;font-weight:600}.picker:hover{background:#1a3d35}
        .uploadrow{display:grid;grid-template-columns:1fr auto;gap:6px;margin-top:17px;font-size:13px}.uploadrow span{overflow-wrap:anywhere;min-width:0}.uploadrow small{color:#84dfc9}.uploadrow progress{grid-column:1/-1;width:100%;height:6px;accent-color:#58dcc1}
        .readonly,footer{font-size:12px;color:#7f8da4;margin:22px 0}.readonly{text-align:center}footer{border-top:1px solid #222e41;padding-top:20px;display:flex;gap:10px;align-items:center}.dot{width:7px;height:7px;border-radius:50%;background:#58dcc1}
        .savehint{font-size:13px;color:#a6b5cb;border-left:3px solid #58dcc1;padding:10px 14px;margin:20px 0;background:#101e2a;border-radius:0 8px 8px 0}.visuallyhidden{position:absolute;clip:rect(0,0,0,0);clip-path:inset(50%);width:1px;height:1px;overflow:hidden;white-space:nowrap}.file:focus-visible,.picker:focus-visible{outline:2px solid #83e8d2;outline-offset:4px}
        @media(max-width:520px){main{padding:26px 16px}.intro h2{font-size:29px}.card{padding:19px}.download{font-size:0}.download:after{content:'↓';font-size:20px}.sectiontitle{align-items:flex-start;flex-direction:column}.tag{font-size:10px}}
        </style><main><header><div class="logo">Z</div><div><h1>Z Hotspot · Files</h1><p>v0.1 · @zalem.j · Local file transfer</p></div></header>
        <div class="intro"><div class="eyebrow">LOCAL WI-FI TRANSFER</div><h2>Your files. One short hop.</h2><p>Download from this computer or send files back over the same hotspot. Keep Wi-Fi connected and this page open until each transfer finishes.</p></div>
        <div class="savehint">""" + save_hint + """</div>
        <section class="card"><div class="sectiontitle"><h2>Available files</h2><span class="tag">"""
                + str(len(self._files)) + " files</span></div><div class=\"files\">" + cards
                + "</div></section>" + receiving
                + '<footer><span class="dot"></span>Local connection &middot; The owner can stop sharing at any time.</footer></main></html>'
                ).encode("utf-8")


class _Handler(BaseHTTPRequestHandler):
    server_version = "HotspotShare"
    sys_version = ""
    protocol_version = "HTTP/1.1"

    @property
    def owner(self) -> FileShareServer:
        return self.server.owner

    def setup(self):
        super().setup()
        self.connection.settimeout(30)
        with self.owner._lock:
            self.owner._connections.add(self.connection)
            self.owner._workers.add(threading.current_thread())

    def finish(self):
        try:
            super().finish()
        except (OSError, ValueError):
            pass
        finally:
            with self.owner._lock:
                self.owner._connections.discard(self.connection)
                self.owner._workers.discard(threading.current_thread())

    def log_message(self, format, *args):
        # Access tokens, local filenames, and client addresses stay out of logs.
        pass

    def _headers(self, code: int, length: int, content_type="text/plain; charset=utf-8", extra=()):
        self._response_started = True
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(length))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", "default-src 'none'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; connect-src 'self'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'")
        self.send_header("Connection", "close")
        for key, value in extra:
            self.send_header(key, value)
        self.end_headers()
        self.close_connection = True

    def _respond(self, code: int, value: str, is_json=False):
        data = value.encode("utf-8")
        self._headers(code, len(data), "application/json; charset=utf-8" if is_json else "text/plain; charset=utf-8")
        if self.command != "HEAD":
            self.wfile.write(data)

    def _error(self, code: int, message: str):
        self._respond(code, json.dumps({"error": message}), is_json=True)

    def _authorize(self):
        if self.owner._stopping.is_set():
            self._error(503, "Sharing has stopped.")
            return None
        if not self.owner._client_allowed(self.client_address[0]):
            self._error(403, "Connect to the hotspot to access these files.")
            return None
        path = urlsplit(self.path).path
        parts = path.split("/")
        if len(parts) < 2 or not hmac.compare_digest(parts[1].encode("utf-8"), self.owner._token.encode("ascii")):
            self._error(404, "This sharing link is unavailable.")
            return None
        return parts[2:]

    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        parts = self._authorize()
        if parts is None:
            return
        if parts == []:
            self._headers(302, 0, extra=(("Location", f"/{self.owner._token}/"),))
            return
        if parts == [""]:
            page = self.owner._page(self.headers.get("User-Agent", ""))
            self._headers(200, len(page), "text/html; charset=utf-8")
            if self.command != "HEAD":
                self.wfile.write(page)
            return
        if len(parts) != 2 or parts[0] != "file" or parts[1] not in self.owner._files:
            self._error(404, "The selected file is unavailable.")
            return
        shared = self.owner._files[parts[1]]
        transfer = None
        success = False
        try:
            with self.owner._open_file(shared) as (descriptor, info):
                ascii_name = re.sub(r'[^A-Za-z0-9._ -]', '_', shared.name).strip(" .") or "download"
                disposition = f'attachment; filename="{ascii_name}"; filename*=UTF-8\'\'{quote(shared.name, safe="")}'
                self._headers(200, info.st_size, "application/octet-stream", (
                    ("Content-Disposition", disposition),
                    ("Last-Modified", formatdate(info.st_mtime, usegmt=True)),))
                if self.command == "HEAD":
                    return
                transfer = self.owner._transfer_start("download", shared.name, info.st_size,
                                                      self.client_address[0], self.headers.get("User-Agent", ""))
                remaining = info.st_size
                sent = 0
                while remaining and not self.owner._stopping.is_set():
                    data = os.read(descriptor, min(CHUNK_SIZE, remaining))
                    if not data:
                        break
                    self.wfile.write(data)
                    remaining -= len(data)
                    sent += len(data)
                    self.owner._transfer_progress(transfer, len(data))
                success = remaining == 0
        except (OSError, ValueError):
            # File-open failures occur before headers; transfer errors occur after.
            if not hasattr(self, "_response_started"):
                try:
                    self._error(410, "This file has changed or is no longer available. Ask the owner to select it again.")
                except OSError:
                    pass
        finally:
            if transfer is not None:
                self.owner._transfer_finish(transfer, success)

    def do_POST(self):
        parts = self._authorize()
        if parts is None:
            return
        if parts != ["upload"]:
            self._error(404, "Unknown sharing endpoint.")
            return
        if self.owner.upload_directory is None:
            self._error(403, "Receiving files is turned off.")
            return
        if self.headers.get("Transfer-Encoding"):
            self._error(400, "Send the upload with a Content-Length header.")
            return
        lengths = self.headers.get_all("Content-Length", [])
        if len(lengths) != 1 or not re.fullmatch(r"[0-9]{1,12}", lengths[0]):
            self._error(411, "A valid Content-Length header is required.")
            return
        length = int(lengths[0])
        if length > MAX_UPLOAD_BYTES:
            self._error(413, "Each file can be up to 2 GB.")
            return
        transfer = None
        success = False
        try:
            raw_name = self.headers.get("X-Filename", "upload.bin")
            if len(raw_name) > 4096:
                raise ValueError("The filename is too long.")
            name = _safe_filename(unquote(raw_name, encoding="utf-8", errors="strict"))
            transfer = self.owner._transfer_start("upload", name, length,
                                                  self.client_address[0], self.headers.get("User-Agent", ""))
            with self.owner._open_receiving_directory() as directory:
                temporary = ".hotspot-upload-" + secrets.token_hex(16) + ".part"
                descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                                     0o600, dir_fd=directory)
                try:
                    received = 0
                    with os.fdopen(descriptor, "wb") as stream:
                        while received < length and not self.owner._stopping.is_set():
                            data = self.rfile.read(min(CHUNK_SIZE, length - received))
                            if not data:
                                raise ValueError("The transfer ended before the file was received.")
                            stream.write(data)
                            received += len(data)
                            self.owner._transfer_progress(transfer, len(data))
                        if received != length:
                            raise ValueError("Sharing stopped before the transfer completed.")
                    # Link atomically without replacing an existing file or symlink.
                    stem, suffix = os.path.splitext(name)
                    for number in range(10000):
                        final_name = name if not number else f"{stem} ({number}){suffix}"
                        try:
                            os.link(temporary, final_name, src_dir_fd=directory, dst_dir_fd=directory,
                                    follow_symlinks=False)
                            break
                        except FileExistsError:
                            continue
                    else:
                        raise OSError("Too many files use that name. Rename the file and retry.")
                    success = True
                finally:
                    os.unlink(temporary, dir_fd=directory)
            self._respond(201, json.dumps({"received": final_name, "bytes": received}), is_json=True)
        except (OSError, UnicodeError, ValueError) as error:
            if isinstance(error, PermissionError):
                message = "The receiving folder is not writable. Ask the owner to choose another folder."
            elif isinstance(error, (UnicodeError, ValueError)):
                message = str(error)
            else:
                message = "The receiving folder is unavailable or the file could not be saved."
            try:
                self._error(400, message)
            except OSError:
                pass
        finally:
            if transfer is not None:
                self.owner._transfer_finish(transfer, success)
