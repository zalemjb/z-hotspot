from pathlib import Path
import tempfile
import unittest
from unittest import mock
import sys
import io
import json

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import hotspot_manager as m


class IsolatedManager(m.HotspotManager):
    def _default_upstream(self): return 'wlp0s20f3'
    def _runtime(self): return None


class BackendTests(unittest.TestCase):
    def test_new_install_uses_a_private_random_password_and_preserves_saved_credentials(self):
        with tempfile.TemporaryDirectory() as directory:
            first = IsolatedManager(Path(directory) / 'first')
            second = IsolatedManager(Path(directory) / 'second')
            self.assertEqual(first.settings['ssid'], 'Z-Hotspot')
            self.assertNotEqual(first.settings['password'], second.settings['password'])
            self.assertGreaterEqual(len(first.settings['password']), 16)
            self.assertEqual(m.validate_settings(**first.settings), first.settings)
            first.save_settings('Saved network', first.settings['password'], first.settings['upstream'])
            self.assertEqual(IsolatedManager(first.state_dir).settings, first.settings)

    def test_settings_reject_configuration_injection(self):
        for ssid in ('a\nwpa=0', 'x' * 33, '\x1btest', ''):
            with self.subTest(ssid=repr(ssid)), self.assertRaises(ValueError):
                m.validate_settings(ssid, 'password12', 'wlp0s20f3')
        for password in ('short', '12345678\n', '12345678\x00', 'å' * 8):
            with self.subTest(password=repr(password)), self.assertRaises(ValueError):
                m.validate_settings('Hotspot', password, 'wlp0s20f3')
        for iface in ('--help', 'wlp0; touch /tmp/nope', 'a/b'):
            with self.assertRaises(ValueError):
                m.validate_settings('Hotspot', 'password12', iface)

    def test_regular_password_symbols_are_preserved(self):
        password = ' hi \\ $`"#;okay '
        self.assertEqual(m.validate_settings('Cafe Wi-Fi', password, 'wlp0s20f3')['password'], password)

    def test_multibyte_ssid_limit_is_bytes(self):
        self.assertEqual(m.validate_settings('شبكتي', 'password12', 'wlp0s20f3')['ssid'], 'شبكتي')
        with self.assertRaises(ValueError):
            m.validate_settings('ش' * 17, 'password12', 'wlp0s20f3')

    def test_mac_validation_rejects_broadcast_multicast_and_injection(self):
        self.assertEqual(m.validate_mac('02:AA:BB:CC:DD:01'), '02:aa:bb:cc:dd:01')
        for mac in ('ff:ff:ff:ff:ff:ff', '01:00:5e:00:00:01', '00:00:00:00:00:00',
                    '02:aa:bb:cc:dd:01; reboot'):
            with self.assertRaises(ValueError): m.validate_mac(mac)

    def test_station_parser_filters_unauthorized_and_tracks_usage(self):
        text = '''Station 02:aa:bb:cc:dd:01 (on ap0)
        rx bytes: 1024
        tx bytes: 4096
        signal: -44 [-44] dBm
        authorized: yes
        connected time: 23 seconds
        inactive time: 105 ms
        signal avg: -46 dBm
        rx bitrate: 54.0 MBit/s
        tx bitrate: 1.2 GBit/s
        rx packets: 100
        tx packets: 200
        tx retries: 3
        tx failed: 1
Station 02:aa:bb:cc:dd:02 (on ap0)
        authorized: no
'''
        result = m.parse_stations(text)
        self.assertEqual(len(result), 1)
        self.assertEqual((result[0]['signal'], result[0]['rx_bytes'], result[0]['connected_seconds']), (-44, 1024, 23))
        self.assertEqual((result[0]['inactive_ms'], result[0]['signal_average_dbm']), (105, -46))
        self.assertEqual((result[0]['rx_link_mbps'], result[0]['tx_link_mbps']), (54, 1200))
        self.assertEqual((result[0]['rx_packets'], result[0]['tx_packets'],
                          result[0]['tx_retries'], result[0]['tx_failed']), (100, 200, 3, 1))

    def test_options_default_when_loading_old_settings_and_persist_separately(self):
        with tempfile.TemporaryDirectory() as directory:
            directory = Path(directory)
            (directory / 'settings.json').write_text(json.dumps({
                'ssid': 'Old hotspot', 'password': 'Password82', 'upstream': 'wlp0s20f3'}))
            manager = IsolatedManager(directory)
            self.assertEqual(manager.options, {'hidden': False, 'isolate_clients': False})
            manager.save_settings(**manager.settings, hidden=True, isolate_clients=True)
            self.assertEqual(IsolatedManager(directory).options, {'hidden': True, 'isolate_clients': True})
            self.assertEqual(set(manager.settings), {'ssid', 'password', 'upstream'})
            manager.save_settings('Renamed', 'Password82', 'wlp0s20f3')
            self.assertEqual(manager.options, {'hidden': True, 'isolate_clients': True})

    def test_network_option_validation_is_strict_and_does_not_mutate_settings(self):
        with tempfile.TemporaryDirectory() as directory:
            manager = IsolatedManager(Path(directory))
            before = manager.settings.copy()
            for bad in (None, 0, 1, 'true', [], {}):
                with self.subTest(bad=bad), self.assertRaises(ValueError):
                    manager.save_settings(**manager.settings, hidden=bad)
                self.assertEqual(manager.settings, before)
                self.assertFalse(manager.options['hidden'])

    def test_option_restart_failure_restores_old_options_and_settings(self):
        with tempfile.TemporaryDirectory() as directory:
            manager = IsolatedManager(Path(directory))
            before = manager.settings.copy()
            with mock.patch.object(manager, '_active_pid', return_value=123), \
                 mock.patch.object(manager, '_preflight'), \
                 mock.patch.object(manager, 'stop'), \
                 mock.patch.object(manager, 'start', side_effect=[RuntimeError('driver'), None]):
                with self.assertRaisesRegex(RuntimeError, 'restored and restarted'):
                    manager.save_settings(**manager.settings, hidden=True, isolate_clients=True)
                self.assertEqual(manager.settings, before)
                self.assertEqual(manager.options, {'hidden': False, 'isolate_clients': False})
                saved = json.loads(manager.config_file.read_text())
                self.assertEqual(saved['options'], manager.options)

    def test_demo_enriched_snapshot_has_no_system_reads(self):
        with mock.patch.object(m.Path, 'read_text', side_effect=AssertionError('system read')), \
             mock.patch.object(m.subprocess, 'run', side_effect=AssertionError('command')):
            manager = m.DemoManager()
            manager.save_settings(**manager.settings, hidden=True)
            state = manager.snapshot()
            self.assertTrue(state['hidden'])
            self.assertEqual(state['ap_ipv4'], '192.168.12.1')
            self.assertEqual(len(state['clients']), 3)

    def test_leases_do_not_become_connected_clients(self):
        leases = m.parse_leases('123 02:aa:bb:cc:dd:01 192.168.12.2 Pixel *\n123 02:aa:bb:cc:dd:02 192.168.12.3 * *\ninvalid\n')
        self.assertEqual(leases['02:aa:bb:cc:dd:01']['name'], 'Pixel')
        self.assertEqual(leases['02:aa:bb:cc:dd:02']['name'], 'Unknown device')
        self.assertEqual(m.parse_stations(''), [])

    def test_block_unblock_persist_with_private_permissions(self):
        with tempfile.TemporaryDirectory() as directory:
            manager = IsolatedManager(Path(directory) / 'state')
            mac = '02:aa:bb:cc:dd:01'
            manager.block(mac)
            manager.block(mac)
            self.assertEqual(manager.blocked, [mac])
            self.assertEqual(manager.config_file.stat().st_mode & 0o777, 0o600)
            self.assertEqual(manager.state_dir.stat().st_mode & 0o777, 0o700)
            loaded = IsolatedManager(manager.state_dir)
            self.assertEqual(loaded.blocked, [mac])
            loaded.unblock(mac)
            self.assertEqual(IsolatedManager(manager.state_dir).blocked, [])

    def test_block_rolls_back_if_persistence_fails(self):
        with tempfile.TemporaryDirectory() as directory:
            manager = IsolatedManager(Path(directory))
            with mock.patch.object(manager, '_runtime', return_value=Path('/fake')), \
                 mock.patch.object(manager, '_ctrl') as ctrl, \
                 mock.patch.object(manager, '_persist', side_effect=OSError('disk full')):
                with self.assertRaises(OSError): manager.block('02:aa:bb:cc:dd:01')
                self.assertEqual(manager.blocked, [])
                self.assertEqual(ctrl.call_args_list[-1].args[:2], ('deny_acl', 'DEL_MAC'))

    def test_saved_settings_roll_back_after_failed_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            manager = IsolatedManager(Path(directory))
            original = manager.settings.copy()
            with mock.patch.object(manager, '_active_pid', return_value=123), \
                 mock.patch.object(manager, '_preflight', return_value=('wlp0s20f3', 7)), \
                 mock.patch.object(manager, 'stop') as stop, \
                 mock.patch.object(manager, 'start', side_effect=[RuntimeError('driver refused'), None]) as start:
                with self.assertRaisesRegex(RuntimeError, 'restored and restarted'):
                    manager.save_settings('New SSID', 'NewPassword98', 'wlp0s20f3')
                self.assertEqual(manager.settings, original)
                self.assertEqual(start.call_count, 2)
                stop.assert_called_once()

    def test_preflight_failure_does_not_stop_existing_hotspot(self):
        with tempfile.TemporaryDirectory() as directory:
            manager = IsolatedManager(Path(directory))
            original = manager.settings.copy()
            with mock.patch.object(manager, '_active_pid', return_value=123), \
                 mock.patch.object(manager, '_preflight', side_effect=RuntimeError('5 GHz')), \
                 mock.patch.object(manager, 'stop') as stop:
                with self.assertRaisesRegex(RuntimeError, '5 GHz'):
                    manager.save_settings('New SSID', 'NewPassword98', 'wlp0s20f3')
                self.assertEqual(manager.settings, original)
                stop.assert_not_called()

    def test_save_failure_attempts_previous_hotspot_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            manager = IsolatedManager(Path(directory))
            original = manager.settings.copy()
            with mock.patch.object(manager, '_active_pid', return_value=123), \
                 mock.patch.object(manager, '_preflight', return_value=('wlp0s20f3', 7)), \
                 mock.patch.object(manager, 'stop') as stop, \
                 mock.patch.object(manager, '_persist', side_effect=[OSError('disk full'), None]), \
                 mock.patch.object(manager, 'start') as start:
                with self.assertRaisesRegex(RuntimeError, 'restored and restarted'):
                    manager.save_settings('New SSID', 'NewPassword98', 'wlp0s20f3')
                self.assertEqual(manager.settings, original)
                start.assert_called_once()
                stop.assert_called_once()

    def test_offline_save_failure_restores_in_memory_settings(self):
        with tempfile.TemporaryDirectory() as directory:
            manager = IsolatedManager(Path(directory))
            original = manager.settings.copy()
            with mock.patch.object(manager, '_active_pid', return_value=None), \
                 mock.patch.object(manager, '_persist', side_effect=OSError('disk full')), \
                 mock.patch.object(manager, 'start') as start:
                with self.assertRaisesRegex(RuntimeError, 'Saving the restored settings also failed'):
                    manager.save_settings('New SSID', 'NewPassword98', 'wlp0s20f3')
                self.assertEqual(manager.settings, original)
                start.assert_not_called()

    def test_kick_rejects_devices_not_connected(self):
        with tempfile.TemporaryDirectory() as directory:
            manager = IsolatedManager(Path(directory))
            with mock.patch.object(manager, 'snapshot', return_value={'clients': []}), \
                 mock.patch.object(manager, '_ctrl') as ctrl:
                with self.assertRaisesRegex(ValueError, 'no longer connected'):
                    manager.kick('02:aa:bb:cc:dd:01')
                ctrl.assert_not_called()

    def test_start_passes_credentials_privately_and_preloads_blocks(self):
        class CapturePipe(io.StringIO):
            def close(self): pass
        with tempfile.TemporaryDirectory() as directory:
            directory = Path(directory)
            manager = IsolatedManager(directory / 'state')
            manager.settings['password'] = ' private \\ #$ pass '
            manager.options = {'hidden': True, 'isolate_clients': True}
            manager.blocked = ['02:aa:bb:cc:dd:01']
            process = mock.Mock(pid=424242)
            process.stdin = CapturePipe()
            process.poll.return_value = None
            with mock.patch.object(manager, '_preflight', return_value=('wlp0s20f3', 7)), \
                 mock.patch.object(manager, '_prepare_ap'), \
                 mock.patch.object(manager, '_runtime', side_effect=[None, Path('/fake')]), \
                 mock.patch.object(manager, '_ctrl', return_value='state=ENABLED'), \
                 mock.patch.object(m, 'PID_FILE', directory / 'pid'), \
                 mock.patch.object(m, 'LOG_FILE', directory / 'log'), \
                 mock.patch.object(m.subprocess, 'Popen', return_value=process) as popen:
                manager.start()
                arguments = popen.call_args.args[0]
                self.assertIn('--hidden', arguments)
                self.assertIn('--isolate-clients', arguments)
                self.assertNotIn(manager.settings['password'], arguments)
                self.assertNotIn(manager.settings['ssid'], arguments)
                self.assertEqual(process.stdin.getvalue(), manager.settings['ssid'] + '\n' + manager.settings['password'] + '\n')
                deny_file = Path(popen.call_args.kwargs['env']['MINT_HOTSPOT_DENY_FILE'])
                self.assertEqual(deny_file.read_text().strip(), manager.blocked[0])


if __name__ == '__main__': unittest.main()
