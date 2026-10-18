"""Tray lifecycle tests: no network, no privilege helper, no system changes."""

from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from hotspot_background import BackgroundController
from gi.repository import Gtk


class FakeApplication:
    def __init__(self):
        self.holds = 0

    def hold(self):
        self.holds += 1

    def release(self):
        self.holds -= 1


class FakeWindow:
    _closing = _busy = _connecting = _refresh_pending = False

    def __init__(self):
        self.visible = True
        self.received = []

    def get_visible(self):
        return self.visible

    def hide(self):
        self.visible = False

    def _receive(self, payload):
        self.received.append(payload)


class FakeTray:
    def __init__(self, menu):
        self.visible = False
        self.status = None

    def show(self):
        self.visible = True

    def hide(self):
        self.visible = False

    def set_status(self, text):
        self.status = text


class FakeBridge:
    def __init__(self):
        self.connected = False
        self.calls = []
        self.running = True
        self.pending = None

    def request(self, action, params, callback):
        self.calls.append(action)
        self.pending = callback

    def complete(self, running=True):
        callback, self.pending = self.pending, None
        callback({"snapshot": {"running": running, "clients": []}}, None)


class BackgroundTests(unittest.TestCase):
    def setUp(self):
        Gtk.init([])
        self.application = FakeApplication()
        self.windows = {"compact": FakeWindow(), "full": FakeWindow()}
        self.windows["full"].hide()
        self.bridge = FakeBridge()
        self.shown, self.quits, self.errors = [], [], []
        self.watcher = True

        def show(mode):
            self.shown.append(mode)
            self.windows[mode].visible = True

        self.controller = BackgroundController(
            self.application, lambda: self.bridge, lambda: self.windows,
            show, lambda: self.quits.append(True), tray_factory=FakeTray,
            watcher_available=lambda: self.watcher)
        self.controller._show_error = self.errors.append

    def tearDown(self):
        self.controller.close()
        self.assertEqual(self.application.holds, 0)

    def test_background_keeps_one_hold_and_never_authenticates(self):
        self.assertTrue(self.controller.enter())
        self.assertTrue(self.controller.enter())
        self.assertEqual(self.application.holds, 1)
        self.assertTrue(self.controller.enabled)
        self.assertFalse(any(window.visible for window in self.windows.values()))
        self.assertTrue(self.controller._tray.visible)
        self.controller._tick()
        self.assertEqual(self.bridge.calls, [])
        self.assertFalse(self.controller.start_item.get_sensitive())
        self.assertFalse(self.controller.stop_item.get_sensitive())

    def test_missing_tray_never_hides_window(self):
        self.watcher = False
        self.assertFalse(self.controller.enter())
        self.assertTrue(self.windows["compact"].visible)
        self.assertFalse(self.controller.enabled)
        self.assertEqual(self.application.holds, 0)
        self.assertEqual(len(self.errors), 1)

    def test_missing_indicator_never_hides_window(self):
        def fail(_menu):
            raise ImportError("missing")
        self.controller._tray_factory = fail
        self.assertFalse(self.controller.enter())
        self.assertTrue(self.windows["compact"].visible)
        self.assertEqual(self.application.holds, 0)

    def test_background_poll_uses_unlocked_helper_once_and_updates_visible_view(self):
        self.bridge.connected = True
        self.controller.enter()
        self.controller._tick()
        self.controller._tick()
        self.assertEqual(self.bridge.calls, ["snapshot"])
        self.assertTrue(self.controller._request_pending)
        self.bridge.complete()
        self.assertFalse(self.controller._request_pending)
        self.assertTrue(self.controller.stop_item.get_sensitive())
        self.assertFalse(self.controller.start_item.get_sensitive())
        self.windows["full"].visible = True
        self.controller._tick()
        self.assertEqual(self.bridge.calls, ["snapshot"])
        self.controller._perform("stop")
        self.assertEqual(self.bridge.calls[-1], "stop")
        self.bridge.complete(running=False)
        self.assertFalse(self.windows["full"].received[-1]["snapshot"]["running"])
        self.assertTrue(self.controller.start_item.get_sensitive())

    def test_busy_window_prevents_concurrent_tray_commands(self):
        self.bridge.connected = True
        self.windows["full"]._busy = True
        self.controller._perform("stop")
        self.assertEqual(self.bridge.calls, [])
        self.windows["full"]._busy = False
        self.controller._perform("stop")
        self.controller._perform("stop")
        self.assertEqual(self.bridge.calls, ["stop"])

    def test_disabling_background_restores_window_before_releasing_hold(self):
        self.controller.enter()
        self.controller.disable()
        self.assertEqual(self.shown, ["compact"])
        self.assertTrue(self.windows["compact"].visible)
        self.assertFalse(self.controller.enabled)
        self.assertFalse(self.controller._tray.visible)
        self.assertEqual(self.application.holds, 0)

    def test_lost_tray_reopens_window(self):
        self.controller.enter()
        self.watcher = False
        self.assertFalse(self.controller._tick())
        self.assertEqual(self.shown, ["compact"])
        self.assertFalse(self.controller.enabled)
        self.assertEqual(len(self.errors), 1)

    def test_shutdown_releases_tray_and_ignores_late_response(self):
        self.bridge.connected = True
        self.controller.enter()
        self.controller._tick()
        self.controller.close()
        self.controller.close()
        self.bridge.complete()
        self.assertEqual(self.application.holds, 0)
        self.assertFalse(self.controller._tray.visible)
        self.assertFalse(self.controller._tick())


if __name__ == "__main__":
    unittest.main(verbosity=2)
