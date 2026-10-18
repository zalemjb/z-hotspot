from pathlib import Path
import unittest


import sys
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import hotspot_metrics as metrics


class FakeManager:
    settings = {"upstream": "wlp0s20f3"}

    def __init__(self, output=""):
        self.output = output
        self.commands = []

    def _run(self, args, **kwargs):
        self.commands.append((args, kwargs))
        return self.output


class MetricsTests(unittest.TestCase):
    def setUp(self):
        self.now = 100.0
        self.values = {"rx_bytes": "100", "tx_bytes": "500", "ifindex": "8", "operstate": "unknown"}

        def read(path):
            if path.name not in self.values:
                raise FileNotFoundError(path)
            return self.values[path.name]

        self.sampler = metrics.NetworkMetrics("/test", clock=lambda: self.now, reader=read)
        self.manager = FakeManager("Connected to 00:11:22:33:44:55\n\tsignal: -67 dBm\n"
                                   "\trx bitrate: 86.7 MBit/s VHT-MCS 3\n\ttx bitrate: 780.0 MBit/s\n")

    def test_wifi_signal_and_link_are_upstream_and_read_only(self):
        result = self.sampler.sample(self.manager)
        self.assertEqual(result["signal_dbm"], -67)
        self.assertEqual(result["signal_percent"], 66)
        self.assertEqual(result["rx_link_mbps"], 86.7)
        self.assertEqual(result["tx_link_mbps"], 780)
        self.assertEqual(self.manager.commands, [(["iw", "dev", "wlp0s20f3", "link"], {"check": False, "timeout": 2})])

    def test_client_direction_and_rate_use_monotonic_delta(self):
        initial = self.sampler.sample(self.manager)
        self.assertEqual(initial["download_bps"], 0)
        self.now += 2
        self.values.update(rx_bytes="300", tx_bytes="1500")
        result = self.sampler.sample(self.manager)
        self.assertEqual(result["download_bps"], 500)
        self.assertEqual(result["upload_bps"], 100)
        self.assertEqual(result["download_total_bytes"], 1500)
        self.assertEqual(result["upload_total_bytes"], 300)

    def test_counter_rollback_rebases_both_rates(self):
        self.sampler.sample(self.manager)
        self.now += 2
        self.values.update(rx_bytes="10", tx_bytes="2500")
        result = self.sampler.sample(self.manager)
        self.assertEqual((result["download_bps"], result["upload_bps"]), (0, 0))
        self.now += 2
        self.values.update(rx_bytes="50", tx_bytes="3500")
        self.assertEqual(self.sampler.sample(self.manager)["download_bps"], 500)

    def test_replaced_interface_does_not_count_previous_session(self):
        self.sampler.sample(self.manager)
        self.now += 2
        self.values.update(ifindex="13", rx_bytes="5000", tx_bytes="5000")
        result = self.sampler.sample(self.manager)
        self.assertEqual((result["download_bps"], result["upload_bps"]), (0, 0))

    def test_missing_or_down_interface_resets_baseline(self):
        self.sampler.sample(self.manager)
        del self.values["rx_bytes"]
        self.now += 2
        self.assertFalse(self.sampler.sample(self.manager)["traffic_available"])
        self.values.update(rx_bytes="1000", tx_bytes="10000")
        self.now += 2
        self.assertEqual(self.sampler.sample(self.manager)["download_bps"], 0)
        self.values.update(operstate="down", tx_bytes="30000")
        self.now += 2
        self.assertEqual(self.sampler.sample(self.manager)["download_bps"], 0)
        self.values.update(operstate="unknown", tx_bytes="40000")
        self.now += 2
        self.assertEqual(self.sampler.sample(self.manager)["download_bps"], 0)

    def test_disconnected_signal_and_optional_bitrates(self):
        self.assertIsNone(metrics.parse_wifi_link("Not connected.")["signal_percent"])
        self.assertEqual(metrics.parse_wifi_link("signal: -30 dBm")["signal_percent"], 100)
        self.assertEqual(metrics.parse_wifi_link("signal: -110 dBm")["signal_percent"], 0)
        self.assertEqual(metrics.parse_wifi_link("tx bitrate: 540 KBit/s")["tx_link_mbps"], .54)
        self.assertEqual(metrics.parse_wifi_link("rx bitrate: 1.2 GBit/s")["rx_link_mbps"], 1200)

    def test_invalid_upstream_is_not_passed_to_command(self):
        self.manager.settings = {"upstream": "../../invalid"}
        self.assertIsNone(self.sampler.sample(self.manager)["signal_percent"])
        self.assertEqual(self.manager.commands, [])

    def test_unavailable_iw_is_nonfatal(self):
        def unavailable(*args, **kwargs):
            raise RuntimeError("Missing command")
        self.manager._run = unavailable
        result = self.sampler.sample(self.manager)
        self.assertTrue(result["traffic_available"])
        self.assertIsNone(result["signal_dbm"])

    def test_upstream_counters_are_separate_and_have_source_direction(self):
        values = {
            'ap0': {'rx_bytes': '100', 'tx_bytes': '1000', 'ifindex': '8', 'operstate': 'up'},
            'wlp0s20f3': {'rx_bytes': '9000', 'tx_bytes': '2000', 'ifindex': '4', 'operstate': 'up'},
        }
        def read(path):
            interface = path.relative_to('/test').parts[0]
            return values[interface][path.name]
        sampler = metrics.NetworkMetrics('/test', clock=lambda: self.now, reader=read)
        sampler.sample(self.manager)
        self.now += 2
        values['ap0'].update(rx_bytes='300', tx_bytes='2000')
        values['wlp0s20f3'].update(rx_bytes='13000', tx_bytes='3000')
        sample = sampler.sample(self.manager)
        self.assertEqual((sample['download_bps'], sample['upload_bps']), (500, 100))
        self.assertEqual((sample['upstream_download_bps'], sample['upstream_upload_bps']), (2000, 500))
        self.assertEqual((sample['upstream_download_total_bytes'], sample['upstream_upload_total_bytes']), (13000, 3000))
        self.assertIsNotNone(sample['link_utilization_percent'])
        self.now += 2
        values['wlp0s20f3'].update(ifindex='15', rx_bytes='19000', tx_bytes='8000')
        sample = sampler.sample(self.manager)
        self.assertEqual((sample['upstream_download_bps'], sample['upstream_upload_bps']), (0, 0))
        values['wlp0s20f3'].update(rx_bytes='50', tx_bytes='100')
        self.now += 2
        sample = sampler.sample(self.manager)
        self.assertEqual((sample['upstream_download_bps'], sample['upstream_upload_bps']), (0, 0))
        self.manager.settings = {'upstream': 'enp1s0'}
        values['enp1s0'] = dict(values['wlp0s20f3'])
        self.now += 2
        self.assertEqual(sampler.sample(self.manager)['upstream_download_bps'], 0)

    def test_utilization_requires_measured_negotiated_capacity(self):
        self.manager.output = 'Not connected.'
        result = self.sampler.sample(self.manager)
        self.assertTrue(result['upstream_traffic_available'])
        self.assertIsNone(result['link_utilization_percent'])

    def test_per_device_rates_reset_on_disconnect_rejoin_and_counter_rollback(self):
        client = {'mac': '02:aa:bb:cc:dd:01', 'rx_bytes': 100, 'tx_bytes': 1000,
                  'connected_seconds': 10, 'signal': -67}
        self.sampler.sample(self.manager)
        initial = self.sampler.enrich_clients([client])[0]
        self.assertEqual(initial['download_bps'], 0)
        self.assertEqual(initial['signal_percent'], 66)
        self.now += 2
        client.update(rx_bytes=300, tx_bytes=2000, connected_seconds=12)
        self.sampler.sample(self.manager)
        device = self.sampler.enrich_clients([client])[0]
        self.assertEqual((device['download_bps'], device['upload_bps']), (500, 100))
        self.now += 2
        client.update(rx_bytes=500, tx_bytes=3000, connected_seconds=1)
        self.sampler.sample(self.manager)
        self.assertEqual(self.sampler.enrich_clients([client])[0]['download_bps'], 0)
        self.now += 2
        self.sampler.sample(self.manager)
        self.sampler.enrich_clients([])
        self.now += 2
        client.update(rx_bytes=600, tx_bytes=4000, connected_seconds=5)
        self.sampler.sample(self.manager)
        self.assertEqual(self.sampler.enrich_clients([client])[0]['download_bps'], 0)
        self.now += 2
        client.update(rx_bytes=5, tx_bytes=5, connected_seconds=7)
        self.sampler.sample(self.manager)
        self.assertEqual(self.sampler.enrich_clients([client])[0]['download_bps'], 0)
        self.assertNotIn('download_bps', client)

    def test_upstream_wifi_identity_and_frequency(self):
        result = metrics.parse_wifi_link('Connected to 02:11:22:33:44:55 (on wlan0)\n\tSSID: Home Wi-Fi\n\tfreq: 2442\n')
        self.assertEqual(result['upstream_ssid'], 'Home Wi-Fi')
        self.assertEqual(result['upstream_bssid'], '02:11:22:33:44:55')
        self.assertEqual(result['upstream_frequency_mhz'], 2442)

    def test_device_rates_use_station_sample_time_despite_later_queries(self):
        client = {'mac': '02:aa:bb:cc:dd:01', 'rx_bytes': 0, 'tx_bytes': 0,
                  'connected_seconds': 10}
        self.now = 110.0  # Another system query took ten seconds after stations.
        self.sampler.sample(self.manager)
        self.sampler.enrich_clients([client], sampled_at=100.0)
        client.update(rx_bytes=600, tx_bytes=1200, connected_seconds=22)
        self.now = 112.0
        self.sampler.sample(self.manager)
        result = self.sampler.enrich_clients([client], sampled_at=112.0)[0]
        self.assertEqual(result['download_bps'], 100)
        self.assertEqual(result['upload_bps'], 50)


if __name__ == "__main__":
    unittest.main()
