import sys
import time
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import hotspot_gui as g


def pump_until(predicate, timeout=5):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        while g.Gtk.events_pending():
            g.Gtk.main_iteration_do(False)
        if predicate():
            return
        time.sleep(.01)
    raise AssertionError('GUI action timed out')


class FullPagesTests(unittest.TestCase):
    def test_pages_details_settings_and_qr_use_applied_credentials(self):
        bridge = g.BackendBridge(demo=True)
        window = g.HotspotWindow(bridge, demo=True)
        window._confirm = lambda *_: True
        try:
            pump_until(lambda: bridge.connected and len(window.device_store) == 3)
            self.assertEqual(set(window.pages), {'overview', 'devices', 'files', 'settings'})
            self.assertEqual(window.page_stack.get_visible_child_name(), 'overview')
            self.assertNotEqual(window.traffic_values['upstream_download_bps'].get_text(), '—')
            self.assertNotEqual(window.info_values['ap_address'].get_text(), '—')
            window.device_view.get_selection().select_path(g.Gtk.TreePath.new_from_indices([0]))
            self.assertNotEqual(window.device_detail_values['mac'].get_text(), '—')
            self.assertNotEqual(window.device_detail_values['connected'].get_text(), '—')
            window.files_button.emit('clicked')
            self.assertEqual(window.page_stack.get_visible_child_name(), 'files')
            self.assertFalse(window.share_qr_button.get_sensitive())
            applied = dict(window._saved_settings)
            window.ssid_entry.set_text('Unsaved network')
            with patch.object(g, 'show_qr_dialog') as show:
                window.join_qr_button.emit('clicked')
                self.assertEqual(show.call_args.args[2], g.wifi_payload(applied['ssid'], applied['password']))
            window.hidden_toggle.set_active(True)
            window.isolation_toggle.set_active(True)
            window.apply_button.emit('clicked')
            pump_until(lambda: not window._busy and not window._dirty)
            self.assertEqual(window._saved_options, {'hidden': True, 'isolate_clients': True})
            self.assertTrue(window.hidden_toggle.get_active())
            window.refresh_combo.set_active_id('8')
            self.assertEqual(window._refresh_seconds, 8)
            window._receive({'metrics': {'upstream_download_bps': None, 'link_utilization_percent': float('nan')}})
            self.assertEqual(window.traffic_values['link_utilization_percent'].get_text(), 'Unavailable')
        finally:
            window._on_close()
            window.destroy()


if __name__ == '__main__':
    unittest.main(verbosity=2)
