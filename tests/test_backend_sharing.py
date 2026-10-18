import io
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import hotspot_gui as g


class FakeMetrics:
    def sample(self, manager):
        return {"signal_percent": 73, "download_bps": 125.0, "upload_bps": 40.0}

    def enrich_clients(self, clients, *, running=True, sampled_at=None):
        return list(clients) if running else []


class FakeManager:
    def __init__(self):
        self.settings = {"ssid": "Test", "password": "HiddenPassword22", "upstream": "wlan0"}
        self.running = True
        self.owned = True
        self.commands = []
        self.events = []
        self.fail_delete = False
        self._gui_metrics = FakeMetrics()
        self.ip = [{"ifname": "ap0", "addr_info": [{"family": "inet", "local": "192.168.12.1", "prefixlen": 24, "scope": "global"}]}]

    def snapshot(self):
        return {"running": self.running, "clients": []}

    def _runtime(self):
        return "/tmp/owned-runtime" if self.owned else None

    def interfaces(self):
        return []

    def _run(self, args, **kwargs):
        self.commands.append(args)
        if args[0] == "ip":
            return json.dumps(self.ip)
        self.events.append(args[3])
        if args[3] == "-D" and self.fail_delete:
            raise RuntimeError("Firewall busy")
        return ""

    def stop(self):
        self.events.append("stop")
        self.running = False


class ShareAccessTests(unittest.TestCase):
    def setUp(self):
        self.manager = FakeManager()

    def request(self, action="share_prepare", port=8787):
        return g.dispatch_request(self.manager, action, {"port": port})

    def test_settings_accept_only_base_strings_and_optional_boolean_network_options(self):
        self.manager.save_settings = Mock()
        self.manager.options = {'hidden': True, 'isolate_clients': False}
        params = {**self.manager.settings, 'hidden': True, 'isolate_clients': False}
        state = g.dispatch_request(self.manager, 'settings', params)
        self.manager.save_settings.assert_called_once_with(**params)
        self.assertEqual(state['options'], self.manager.options)
        self.assertIsNot(state['options'], self.manager.options)
        self.manager.save_settings.reset_mock()
        g.dispatch_request(self.manager, 'settings', dict(self.manager.settings))
        self.manager.save_settings.assert_called_once_with(**self.manager.settings)
        self.manager.save_settings.reset_mock()
        for key in ('hidden', 'isolate_clients'):
            for bad in (None, 0, 1, 'yes', [], {}):
                with self.subTest(key=key, bad=bad), self.assertRaises(ValueError):
                    g.dispatch_request(self.manager, 'settings', {**self.manager.settings, key: bad})
        for params in ({**self.manager.settings, 'command': 'reboot'},
                       {**self.manager.settings, 'ssid': True}):
            with self.assertRaises(ValueError):
                g.dispatch_request(self.manager, 'settings', params)
        self.manager.save_settings.assert_not_called()

    def test_exact_interface_source_destination_tcp_rule(self):
        result = self.request()
        self.assertEqual(result, {"address": "192.168.12.1", "network": "192.168.12.0/24", "port": 8787})
        insert = self.manager.commands[-1]
        self.assertEqual(insert, ["iptables", "-w", "3", "-I", "INPUT", "1", "-i", "ap0", "-s", "192.168.12.0/24",
                                  "-d", "192.168.12.1", "-p", "tcp", "--dport", "8787", "-m", "comment", "--comment",
                                  "mint-hotspot-share-" + str(g.os.getpid()), "-j", "ACCEPT"])
        self.request("share_close")
        self.assertEqual(self.manager.commands[-1], ["iptables", "-w", "3", "-D", "INPUT", *insert[6:]])
        self.assertEqual(g._share_firewall(self.manager).rules, {})

    def test_duplicate_prepare_has_one_rule_and_unknown_close_does_nothing(self):
        self.request()
        self.request()
        self.assertEqual(len([args for args in self.manager.commands if args[0] == "iptables"]), 1)
        self.request("share_close", 8788)
        self.assertEqual(len([args for args in self.manager.commands if args[0] == "iptables"]), 1)

    def test_strict_port_parameters_before_any_commands(self):
        for port in (True, False, 8787.0, "8787", 80, 0, -1, 65536, None, []):
            with self.subTest(port=port), self.assertRaises(ValueError):
                self.request(port=port)
        for params in ({}, {"port": 8787, "path": "/root"}, {"command": "run"}):
            with self.assertRaises(ValueError):
                g.dispatch_request(self.manager, "share_prepare", params)
        self.assertEqual(self.manager.commands, [])

    def test_requires_running_and_owned_ap(self):
        self.manager.running = False
        with self.assertRaises(RuntimeError):
            self.request()
        self.manager.running, self.manager.owned = True, False
        with self.assertRaises(RuntimeError):
            self.request()
        self.assertEqual(self.manager.commands, [])

    def test_rejects_unspecified_multicast_loopback_ambiguous_or_invalid_addresses(self):
        for address, prefix in (("0.0.0.0", 24), ("127.0.0.1", 8), ("224.0.0.1", 24), ("169.254.1.1", 16),
                                ("192.168.12.1", 0), ("192.168.12.1", True), ("192.168.12.1", "24"), ("::1", 24)):
            self.manager.ip[0]["addr_info"][0].update(local=address, prefixlen=prefix)
            with self.subTest(address=address, prefix=prefix), self.assertRaises(RuntimeError):
                self.request()
        self.manager.ip = [{"ifname": "wlan0", "addr_info": []}]
        with self.assertRaises(RuntimeError):
            self.request()
        self.assertFalse(any(args[0] == "iptables" for args in self.manager.commands))

    def test_all_unique_ports_cleaned_before_stop(self):
        self.request(port=8787)
        self.request(port=8788)
        result = g.dispatch_request(self.manager, "stop", {})
        self.assertFalse(result["snapshot"]["running"])
        self.assertEqual(self.manager.events, ["-I", "-I", "-D", "-D", "stop"])
        self.assertEqual(g._share_firewall(self.manager).rules, {})

    def test_failed_deletion_stays_tracked_for_cleanup_retry(self):
        self.request()
        self.manager.fail_delete = True
        firewall = g._share_firewall(self.manager)
        self.assertEqual(len(firewall.close_all()), 1)
        self.assertIn(8787, firewall.rules)
        self.manager.fail_delete = False
        self.assertEqual(firewall.close_all(), [])
        self.assertEqual(firewall.rules, {})

    def test_backend_eof_removes_rules_without_stopping_hotspot(self):
        self.request()
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(g, "RPC_LOCK_PATH", str(Path(directory) / "helper.lock")), \
                 patch.object(g, "HotspotManager", return_value=self.manager), \
                 patch.object(g.os, "geteuid", return_value=0), \
                 patch.object(g.os, "umask"), \
                 patch.object(g.os, "fstat", return_value=SimpleNamespace(st_mode=g.stat.S_IFREG, st_uid=0)), \
                 patch.object(g.sys, "stdin", SimpleNamespace(buffer=io.BytesIO(b""))):
                self.assertEqual(g.run_backend_service(), 0)
        self.assertEqual(g._share_firewall(self.manager).rules, {})
        self.assertTrue(self.manager.running)
        self.assertEqual(self.manager.events, ["-I", "-D"])

    def test_state_metrics_shared_snapshot_and_root(self):
        state = g._manager_state(self.manager)
        self.assertEqual(state["metrics"]["signal_percent"], 73)
        self.assertIs(state["metrics"], state["snapshot"]["metrics"])

    def test_demo_uses_no_metrics_reader_or_system_commands(self):
        demo = g.DemoManager()
        with patch.object(g, "NetworkMetrics", side_effect=AssertionError("No real sampling in demo")):
            state = g.dispatch_request(demo, "boot", {})
            self.assertEqual(state["metrics"]["signal_percent"], 88)
            self.assertEqual(g.dispatch_request(demo, "share_prepare", {"port": 8787})["address"], "192.168.12.1")
            g.dispatch_request(demo, "share_close", {"port": 8787})
            offline = g.dispatch_request(demo, "stop", {})
            self.assertEqual(offline["metrics"]["download_bps"], 0)

    def test_state_observer_runs_before_target_callback_even_without_target(self):
        bridge = g.BackendBridge.__new__(g.BackendBridge)
        bridge._closing = False
        calls = []
        bridge.on_state = lambda payload: calls.append("state")
        bridge._deliver(lambda result, error: calls.append("target"), {"snapshot": {}}, None)
        context = g.GLib.MainContext.default()
        while context.pending():
            context.iteration(False)
        self.assertEqual(calls, ["state", "target"])
        bridge._deliver(None, {"snapshot": {}}, None)
        while context.pending():
            context.iteration(False)
        self.assertEqual(calls, ["state", "target", "state"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
