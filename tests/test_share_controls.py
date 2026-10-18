"""Exercise the real demo bridge with file-sharing controls and no listeners."""

from pathlib import Path
import sys
import tempfile
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import hotspot_gui as backend
import hotspot_gui as controls


def pump_until(predicate, timeout=3):
    context = backend.GLib.MainContext.default()
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        while context.pending():
            context.iteration(False)
        if predicate():
            return
        time.sleep(.005)
    raise AssertionError("Demo bridge operation timed out")


class Widget:
    def __init__(self, active=False):
        self.active = active
        self.text = ""
        self.sensitive = True

    def get_active(self): return self.active
    def set_sensitive(self, value): self.sensitive = value
    def set_text(self, value): self.text = value
    def get_text(self): return self.text
    def get_style_context(self): return self
    def remove_class(self, name): pass
    def add_class(self, name): pass
    def set_tooltip_text(self, value): pass


class Harness(controls.FileSharingControls):
    def __init__(self, bridge):
        self.manager = bridge
        self.demo = True
        self._closing = False
        self._busy = False
        self._snapshot = {"running": True}
        self._share_files = []
        self._share_upload_directory = None
        self._share_server = None
        self._share_demo_active = False
        self._share_busy = False
        self._share_generation = 0
        self._share_port = 8765
        self.messages = []
        for name in ("share_start_button", "share_stop_button", "share_choose_button",
                     "share_receive_toggle", "share_folder_button", "share_copy_button",
                     "share_badge", "share_selection_label", "share_folder_label",
                     "share_status_label", "share_link_entry"):
            setattr(self, name, Widget())
        self.share_qr_button = Widget()
        self.share_activity_label = Widget()

    def _toast(self, message, error=False):
        self.messages.append((message, error))

    def receive(self, result):
        self._snapshot = result["snapshot"]
        self._update_file_sharing()


class SharingControlTests(unittest.TestCase):
    def setUp(self):
        self.bridge = backend.BackendBridge(demo=True)
        self.window = Harness(self.bridge)
        self.bridge.on_state = self.window.receive
        self.bridge.connect(lambda result, error: self.assertIsNone(error))
        pump_until(lambda: self.bridge.connected)
        self.block_listener = patch.object(controls, "FileShareServer", side_effect=AssertionError("Demo must not expose a listener"))
        self.mock_server = self.block_listener.start()

    def tearDown(self):
        self.window._closing = True
        self.window._stop_file_share()
        self.bridge.close()
        self.bridge._worker.join(timeout=2)
        self.block_listener.stop()

    def barrier(self):
        completed = []
        self.bridge.request("snapshot", {}, lambda *_: completed.append(True))
        pump_until(lambda: bool(completed))

    def test_receive_only_start_stop(self):
        with tempfile.TemporaryDirectory() as directory:
            window = self.window
            window._share_upload_directory = Path(directory)
            window.share_receive_toggle.active = True
            window._update_file_sharing()
            self.assertTrue(window.share_start_button.sensitive)
            window._start_file_share()
            pump_until(window._share_active)
            self.assertEqual(window.share_badge.text, "SHARING")
            self.assertIn("demo-preview", window.share_link_entry.text)
            self.assertIn("No files or network ports", window.share_status_label.text)
            self.assertFalse(window.share_receive_toggle.sensitive)
            self.assertIn(8765, self.bridge._manager._gui_share_firewall.rules)
            window._stop_file_share()
            self.barrier()
            self.assertFalse(window._share_active())
            self.assertEqual(window.share_link_entry.text, "")
            self.assertEqual(self.bridge._manager._gui_share_firewall.rules, {})
            self.assertTrue(window.share_receive_toggle.sensitive)
            self.mock_server.assert_not_called()

    def test_stop_then_restart_before_old_callback_preserves_new_rule(self):
        window = self.window
        window._share_files = [Path("/demo/selected-file.txt")]
        window._start_file_share()
        window._stop_file_share()
        window._start_file_share()
        pump_until(window._share_active)
        self.barrier()
        self.assertTrue(window._share_active())
        self.assertIn(8765, self.bridge._manager._gui_share_firewall.rules)
        self.mock_server.assert_not_called()

    def test_hotspot_stop_automatically_stops_file_sharing(self):
        window = self.window
        window._share_files = [Path("/demo/selected-file.txt")]
        window._start_file_share()
        pump_until(window._share_active)
        self.bridge.request("stop", {}, lambda *_: None)
        pump_until(lambda: not window._snapshot["running"])
        self.barrier()
        self.assertFalse(window._share_active())
        self.assertEqual(window.share_badge.text, "OFF")
        self.assertEqual(self.bridge._manager._gui_share_firewall.rules, {})

    def test_no_selection_does_not_prepare_firewall(self):
        self.window._start_file_share()
        self.assertFalse(self.window._share_busy)
        self.assertEqual(self.window.messages[-1], ("Choose files or a receive folder first.", True))
        self.assertFalse(hasattr(self.bridge._manager, "_gui_share_firewall"))

    def test_transfer_qr_requires_active_sharing_and_uses_current_link(self):
        captured = []
        qr = SimpleNamespace(show_qr_dialog=lambda *args: captured.append(args))
        with patch.dict(sys.modules, {"hotspot_qr": qr}):
            self.window._share_qr_code()
            self.assertEqual(captured, [])
            self.window._share_files = [Path("/demo/selected-file.txt")]
            self.window._start_file_share()
            pump_until(self.window._share_active)
            self.assertTrue(self.window.share_qr_button.sensitive)
            self.window._share_qr_code()
            self.assertEqual(captured[0][2], self.window.share_link_entry.get_text())
            self.assertIn("connect your phone", captured[0][3])
            self.window._stop_file_share()
            self.assertFalse(self.window.share_qr_button.sensitive)

    def test_open_share_page_and_join_wifi_action(self):
        pages, joins = [], []
        self.window.page_stack = SimpleNamespace(set_visible_child_name=pages.append)
        self.window._show_join_qr = lambda: joins.append(True)
        self.window._focus_file_sharing()
        self.window._share_join_wifi()
        self.assertEqual(pages, ["files"])
        self.assertEqual(joins, [True])


if __name__ == "__main__":
    unittest.main(verbosity=2)
