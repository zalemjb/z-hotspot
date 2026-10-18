import os
from pathlib import Path
import sys
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import hotspot_gui as g


def pump_until(predicate, timeout=7):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        while g.Gtk.events_pending():
            g.Gtk.main_iteration_do(False)
        if predicate():
            return
        time.sleep(.01)
    raise AssertionError('GUI action timed out')


class DispatchTests(unittest.TestCase):
    def setUp(self): self.manager = g.DemoManager()

    def test_rejects_arbitrary_method_dispatch(self):
        for action in ('__dict__', '_run', 'settings.clear', 'interfaces'):
            with self.assertRaises(ValueError): g.dispatch_request(self.manager, action, {})

    def test_rejects_extra_or_malformed_params(self):
        for action, params in [('start', {'command': 'touch /tmp/no'}),
                               ('kick', {'mac': '02:aa:bb:cc:dd:01', 'other': 1}),
                               ('block', {'mac': '--help'}), ('settings', {'ssid': 'X'})]:
            with self.assertRaises(ValueError): g.dispatch_request(self.manager, action, params)

    def test_state_and_persistent_block_actions(self):
        state = g.dispatch_request(self.manager, 'boot', {})
        mac = state['snapshot']['clients'][0]['mac']
        blocked = g.dispatch_request(self.manager, 'block', {'mac': mac})
        self.assertIn(mac, blocked['snapshot']['blocked'])
        self.assertNotIn(mac, [c['mac'] for c in blocked['snapshot']['clients']])
        self.assertNotIn(mac, g.dispatch_request(self.manager, 'unblock', {'mac': mac})['snapshot']['blocked'])

    def test_errors_redact_all_known_passwords(self):
        message = g._rpc_error_text('OldPassword89 NewPassword98\x1b', 'OldPassword89', 'NewPassword98')
        self.assertNotIn('OldPassword89', message)
        self.assertNotIn('NewPassword98', message)
        self.assertNotIn('\x1b', message)

    def test_gui_native_controls_and_refresh(self):
        g.Gtk.init([])
        bridge = g.BackendBridge(demo=True)
        window = g.HotspotWindow(bridge, demo=True)
        window._confirm = lambda *_: True
        try:
            pump_until(lambda: bridge.connected and len(window.device_store) == 3)
            self.assertEqual(window.status_label.get_text(), 'ONLINE')
            self.assertFalse(window.password_entry.get_visibility())
            window.ssid_entry.set_text('My new hotspot')
            window.password_entry.set_text('GuiPassword2026')
            window._refresh(manual=True)
            pump_until(lambda: not window._refresh_pending)
            self.assertEqual(window.ssid_entry.get_text(), 'My new hotspot')
            self.assertTrue(window._dirty)
            window.apply_button.emit('clicked')
            pump_until(lambda: not window._busy and not window._dirty)
            self.assertEqual(window._saved_settings['password'], 'GuiPassword2026')
            window.device_view.get_selection().select_path(g.Gtk.TreePath.new_from_indices([0]))
            window.disconnect_button.emit('clicked')
            pump_until(lambda: not window._busy and len(window.device_store) == 2)
            window.device_view.get_selection().select_path(g.Gtk.TreePath.new_from_indices([0]))
            window.block_button.emit('clicked')
            pump_until(lambda: not window._busy and len(window._snapshot['blocked']) == 1)
            mac = window._snapshot['blocked'][0]
            window._perform('unblock', {'mac': mac}, 'Testing unblock', 'Unblocked')
            pump_until(lambda: not window._busy and not window._snapshot['blocked'])
            window.stop_button.emit('clicked')
            pump_until(lambda: not window._busy and window.status_label.get_text() == 'OFFLINE')
            window.start_button.emit('clicked')
            pump_until(lambda: not window._busy and window.status_label.get_text() == 'ONLINE')
            self.assertFalse(window.password_entry.get_visibility())
        finally:
            window._on_close()
            window.destroy()
            bridge.close()


if __name__ == '__main__': unittest.main(verbosity=2)
