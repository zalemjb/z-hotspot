"""Loopback-only integration tests for the unprivileged file-sharing service."""

import http.client
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import os
from pathlib import Path
import re
import socket
import sys
import tempfile
import time
import unittest
from unittest.mock import patch
from urllib.parse import quote, urlsplit


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import hotspot_share as module
FileShareServer = module.FileShareServer


class FileShareTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="hotspot-share-test-")
        self.root = Path(self.temporary.name)
        self.selected = self.root / 'notes <shared> "عربي".txt'
        self.payload = b"selected file\n" * 20000
        self.selected.write_bytes(self.payload)
        self.receiving = self.root / "received"
        self.receiving.mkdir()
        self.servers = []

    def tearDown(self):
        for server in self.servers:
            server.stop()
        self.temporary.cleanup()

    def server(self, receiving=False, **kwargs):
        server = FileShareServer([self.selected], "127.0.0.1", "127.0.0.0/8", port=0,
                                 upload_directory=self.receiving if receiving else None, **kwargs)
        server.start()
        self.servers.append(server)
        return server

    def request(self, server, route="", body=None, headers=None, method="GET"):
        url = urlsplit(server.url)
        connection = http.client.HTTPConnection(url.hostname, url.port, timeout=3)
        connection.request(method, url.path + route, body=body, headers=headers or {})
        response = connection.getresponse()
        result = response.status, dict(response.headers), response.read()
        connection.close()
        return result

    def file_route(self, server):
        return "file/" + next(iter(server._files))

    def test_page_and_selected_download(self):
        server = self.server()
        code, headers, page = self.request(server)
        self.assertEqual(code, 200)
        self.assertIn(b"&lt;shared&gt;", page)
        self.assertNotIn(b"notes <shared>", page)
        self.assertIn(b"Receiving files is turned off", page)
        self.assertIn("no-referrer", headers["Referrer-Policy"])
        code, headers, body = self.request(server, self.file_route(server))
        self.assertEqual((code, body), (200, self.payload))
        self.assertIn("filename*=UTF-8''", headers["Content-Disposition"])
        self.assertEqual(server.stats()["downloads"], 1)
        self.assertEqual(server.stats()["bytes_sent"], len(self.payload))

    def test_head_does_not_increment_downloads(self):
        server = self.server()
        code, headers, body = self.request(server, self.file_route(server), method="HEAD")
        self.assertEqual((code, body), (200, b""))
        self.assertEqual(int(headers["Content-Length"]), len(self.payload))
        self.assertEqual(server.stats()["downloads"], 0)

    def test_token_and_unknown_file_are_required(self):
        server = self.server()
        url = urlsplit(server.url)
        for route in ("/", "/wrong/", "/%ff/", "/wrong/file/../secret"):
            connection = http.client.HTTPConnection(url.hostname, url.port, timeout=3)
            connection.request("GET", route)
            response = connection.getresponse()
            self.assertEqual(response.status, 404)
            self.assertNotIn(self.payload[:30], response.read())
            connection.close()
        for route in ("file/../../secret", "file/%2e%2e%2fsecret", "file/not-selected", "../received"):
            self.assertEqual(self.request(server, route)[0], 404)

    def test_off_subnet_client_is_rejected(self):
        server = FileShareServer([self.selected], "127.0.0.2", "127.0.0.2/32", port=0)
        self.servers.append(server.start())
        self.assertEqual(self.request(server)[0], 403)

    def test_unsafe_bind_addresses_are_rejected(self):
        for address, network in (("0.0.0.0", "0.0.0.0/0"), ("224.0.0.1", "224.0.0.0/24"),
                                 ("127.0.0.1", "192.168.0.0/24"), ("127.0.0.0", "127.0.0.0/24")):
            with self.assertRaises(ValueError):
                FileShareServer([self.selected], address, network)

    def test_root_start_is_rejected(self):
        server = FileShareServer([self.selected], "127.0.0.1", "127.0.0.0/8", port=0)
        with patch.object(module.os, "geteuid", return_value=0), self.assertRaises(PermissionError):
            server.start()

    def test_unselected_files_never_appear(self):
        hidden = self.root / "unselected-secret.txt"
        hidden.write_text("SECRET")
        server = self.server()
        self.assertNotIn(b"unselected-secret", self.request(server)[2])
        self.assertEqual(self.request(server, "file/" + quote(str(hidden), safe=""))[0], 404)

    def test_changed_symlink_is_not_followed(self):
        server = self.server()
        target = self.root / "private.txt"
        target.write_text("PRIVATE")
        self.selected.unlink()
        self.selected.symlink_to(target)
        code, _, body = self.request(server, self.file_route(server))
        self.assertEqual(code, 410)
        self.assertNotIn(b"PRIVATE", body)

    def test_replaced_file_is_not_served(self):
        server = self.server()
        replacement = self.root / "replacement"
        replacement.write_text("different")
        replacement.replace(self.selected)
        self.assertEqual(self.request(server, self.file_route(server))[0], 410)

    def test_initial_symlink_and_fifo_are_rejected(self):
        linked = self.root / "linked"
        linked.symlink_to(self.selected)
        with self.assertRaises(OSError):
            FileShareServer([linked], "127.0.0.1", "127.0.0.0/8")
        fifo = self.root / "pipe"
        os.mkfifo(fifo)
        with self.assertRaises(ValueError):
            FileShareServer([fifo], "127.0.0.1", "127.0.0.0/8")

    def test_uploads_require_opt_in(self):
        server = self.server()
        code, _, _ = self.request(server, "upload", b"hi", {"X-Filename": "test.txt"}, "POST")
        self.assertEqual(code, 403)
        self.assertEqual(list(self.receiving.iterdir()), [])

    def test_normal_unicode_upload_and_no_overwrite(self):
        server = self.server(receiving=True)
        name = "ملف.txt"
        headers = {"X-Filename": quote(name, safe="")}
        for expected in (name, "ملف (1).txt"):
            code, _, body = self.request(server, "upload", self.payload, headers, "POST")
            self.assertEqual(code, 201)
            self.assertEqual(json.loads(body)["received"], expected)
            self.assertEqual((self.receiving / expected).read_bytes(), self.payload)
        stats = server.stats()
        self.assertEqual((stats["uploads"], stats["bytes_received"]), (2, len(self.payload) * 2))
        self.assertIn(b"Send to this computer", self.request(server)[2])

    def test_upload_path_traversal_is_basename_only(self):
        server = self.server(receiving=True)
        for filename in ("../../escape.txt", r"C:\outside\escape2.txt", "\x00...", "."):
            code, _, body = self.request(server, "upload", b"safe", {"X-Filename": quote(filename, safe="")}, "POST")
            self.assertEqual(code, 201)
            saved = json.loads(body)["received"]
            self.assertEqual((self.receiving / saved).read_bytes(), b"safe")
            self.assertNotIn("/", saved)
            self.assertNotIn("\\", saved)
        self.assertFalse((self.root / "escape.txt").exists())

    def test_existing_destination_symlink_is_not_followed(self):
        target = self.root / "secret"
        target.write_text("untouched")
        (self.receiving / "test.txt").symlink_to(target)
        server = self.server(receiving=True)
        code, _, body = self.request(server, "upload", b"new", {"X-Filename": "test.txt"}, "POST")
        self.assertEqual(code, 201)
        self.assertEqual(json.loads(body)["received"], "test (1).txt")
        self.assertEqual(target.read_text(), "untouched")

    def test_changed_receiving_directory_is_not_followed(self):
        server = self.server(receiving=True)
        moved = self.root / "moved"
        self.receiving.rename(moved)
        outside = self.root / "outside"
        outside.mkdir()
        self.receiving.symlink_to(outside, target_is_directory=True)
        self.assertEqual(self.request(server, "upload", b"data", {"X-Filename": "x"}, "POST")[0], 400)
        self.assertEqual(list(outside.iterdir()), [])

    def test_upload_size_and_chunked_restrictions(self):
        server = self.server(receiving=True)
        self.assertEqual(self.request(server, "upload", b"", {"Content-Length": str(module.MAX_UPLOAD_BYTES + 1)}, "POST")[0], 413)
        self.assertEqual(self.request(server, "upload", b"", {"Transfer-Encoding": "chunked"}, "POST")[0], 400)
        self.assertEqual(self.request(server, "upload", b"", {"Content-Length": "-1"}, "POST")[0], 411)

    def test_empty_upload_is_valid(self):
        server = self.server(receiving=True)
        self.assertEqual(self.request(server, "upload", b"", {"X-Filename": "empty"}, "POST")[0], 201)
        self.assertEqual((self.receiving / "empty").read_bytes(), b"")

    def test_interrupted_upload_is_removed(self):
        server = self.server(receiving=True)
        url = urlsplit(server.url)
        connection = socket.create_connection((url.hostname, url.port), timeout=3)
        connection.sendall((f"POST {url.path}upload HTTP/1.1\r\nHost: localhost\r\nContent-Length: 100\r\nX-Filename: partial.txt\r\n\r\n").encode() + b"short")
        connection.shutdown(socket.SHUT_WR)
        response = connection.recv(4096)
        self.assertIn(b"400 Bad Request", response)
        connection.close()
        self.assertEqual(list(self.receiving.iterdir()), [])

    def test_stop_interrupts_active_upload_and_invalidates_link(self):
        server = self.server(receiving=True)
        old_url = urlsplit(server.url)
        connection = socket.create_connection((old_url.hostname, old_url.port), timeout=3)
        connection.sendall((f"POST {old_url.path}upload HTTP/1.1\r\nHost: localhost\r\nContent-Length: 1000000\r\nX-Filename: partial\r\n\r\n").encode() + b"short")
        deadline = time.monotonic() + 2
        while not server.stats()["active_requests"] and time.monotonic() < deadline:
            time.sleep(.01)
        start = time.monotonic()
        server.stop()
        self.assertLess(time.monotonic() - start, 2.5)
        self.assertEqual(list(self.receiving.iterdir()), [])
        self.assertFalse(server.stats()["running"])
        self.assertEqual(server.stats()["active_requests"], 0)
        self.assertEqual(server.url, "")
        connection.close()
        with self.assertRaises(OSError):
            socket.create_connection((old_url.hostname, old_url.port), timeout=.3)
        server.start()
        self.assertNotEqual(urlsplit(server.url).path, old_url.path)

    def test_browser_profiles_transfer_files_with_matching_hashes(self):
        """Real loopback HTTP; UA strings simulate clients, not physical phones."""
        profiles = {
            "iphone": ("Mozilla/5.0 (iPhone; CPU iPhone OS 18_6 like Mac OS X) AppleWebKit/605.1.15 Version/18.6 Mobile/15E148 Safari/604.1", "iPhone · Safari"),
            "samsung": ("Mozilla/5.0 (Linux; Android 15; SM-S928B) AppleWebKit/537.36 Chrome/138.0.0.0 Mobile Safari/537.36", "Samsung / Android · Chrome"),
            "laptop": ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/138.0.0.0 Safari/537.36", "Linux · Chrome"),
        }
        server = self.server(receiving=True)
        digest = hashlib.sha256(self.payload).hexdigest()
        for profile, (agent, hint) in profiles.items():
            with self.subTest(profile=profile):
                headers = {"User-Agent": agent}
                code, _, page = self.request(server, headers=headers)
                self.assertEqual(code, 200)
                self.assertIn(b'download="', page)
                self.assertIn(b'type="file" multiple', page)
                if profile == "iphone":
                    self.assertIn(b"On iPhone:", page)
                    self.assertIn(b"Safari's Downloads", page)
                code, _, body = self.request(server, self.file_route(server), headers=headers)
                self.assertEqual(code, 200)
                self.assertEqual(hashlib.sha256(body).hexdigest(), digest)
                headers["X-Filename"] = f"from-{profile}.bin"
                code, _, body = self.request(server, "upload", self.payload, headers, "POST")
                self.assertEqual(code, 201)
                filename = json.loads(body)["received"]
                self.assertEqual(hashlib.sha256((self.receiving / filename).read_bytes()).hexdigest(), digest)
        deadline = time.monotonic() + 2
        while len(server.stats()["recent_transfers"]) < 6 and time.monotonic() < deadline:
            time.sleep(.005)
        stats = server.stats()
        self.assertEqual((stats["downloads"], stats["uploads"]), (3, 3))
        self.assertEqual((stats["bytes_sent"], stats["bytes_received"]), (3 * len(self.payload), 3 * len(self.payload)))
        events = stats["recent_transfers"]
        self.assertEqual({event["browser_hint"] for event in events}, {hint for _, hint in profiles.values()})
        self.assertEqual({event["status"] for event in events}, {"Sent to browser", "Saved to computer"})
        self.assertTrue(all("not verified" in event["source"] for event in events))
        self.assertNotIn(server._token, repr(stats))

    def test_simultaneous_uploads_and_downloads_preserve_file_hashes(self):
        server = self.server(receiving=True)
        digest = hashlib.sha256(self.payload).digest()
        def transfer(number):
            if number % 2:
                response = self.request(server, self.file_route(server))
                self.assertEqual(response[0], 200)
                self.assertEqual(hashlib.sha256(response[2]).digest(), digest)
            else:
                response = self.request(server, "upload", self.payload, {"X-Filename": f"parallel-{number}.bin"}, "POST")
                self.assertEqual(response[0], 201)
                path = self.receiving / json.loads(response[2])["received"]
                self.assertEqual(hashlib.sha256(path.read_bytes()).digest(), digest)
        with ThreadPoolExecutor(max_workers=6) as pool:
            list(pool.map(transfer, range(12)))
        self.assertEqual(server.stats()["downloads"], 6)
        self.assertEqual(server.stats()["uploads"], 6)

    def test_failed_upload_activity_and_stats_are_snapshots(self):
        server = self.server(receiving=True)
        self.receiving.rmdir()
        code, _, _ = self.request(server, "upload", b"data", {"X-Filename": "could-not-save.txt"}, "POST")
        self.assertEqual(code, 400)
        deadline = time.monotonic() + 2
        while server.stats()["recent_transfers"][0]["status"] == "Transferring" and time.monotonic() < deadline:
            time.sleep(.005)
        stats = server.stats()
        self.assertEqual(stats["recent_transfers"][0]["status"], "Interrupted / failed")
        stats["recent_transfers"][0]["name"] = "changed"
        self.assertEqual(server.stats()["recent_transfers"][0]["name"], "could-not-save.txt")


if __name__ == "__main__":
    unittest.main(verbosity=2)
