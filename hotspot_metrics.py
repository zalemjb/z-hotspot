#!/usr/bin/env python3
"""Read-only Wi-Fi signal and hotspot traffic sampling.

Download/upload are from the connected devices' perspective: AP transmit bytes
are downloads, and AP receive bytes are uploads. Upstream traffic includes this
computer and its shared traffic, and is not an Internet speed test. Link rates
describe the negotiated Wi-Fi link, not the ISP's capacity.
"""

from __future__ import annotations

import math
from pathlib import Path
import re
import time
from typing import Callable


_INTERFACE = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9_.:-]{0,14}\Z")


def parse_wifi_link(output: str) -> dict:
    """Parse `iw dev IFACE link`; missing capabilities remain unavailable."""
    result = {"signal_percent": None, "signal_dbm": None,
              "rx_link_mbps": None, "tx_link_mbps": None,
              "upstream_ssid": None, "upstream_bssid": None,
              "upstream_frequency_mhz": None}
    if "Not connected" in output:
        return result
    for key, pattern in (("upstream_ssid", r"^\s*SSID:\s*(.+)$"),
                         ("upstream_bssid", r"^Connected to\s+([0-9a-f:]{17})\b")):
        match = re.search(pattern, output, re.M | re.I)
        if match:
            result[key] = "".join(c for c in match[1] if c.isprintable())[:128]
    frequency = re.search(r"^\s*freq:\s*(\d+)\s*$", output, re.M)
    if frequency:
        result["upstream_frequency_mhz"] = int(frequency[1])
    signal = re.search(r"^\s*signal:\s*(-?\d+)(?:\.\d+)?\s+dBm\b", output, re.M)
    if signal:
        dbm = int(signal[1])
        # This is an estimate, not a vendor-specific RSSI quality measurement.
        result["signal_dbm"] = dbm
        result["signal_percent"] = max(0, min(100, 2 * (dbm + 100)))
    for direction in ("rx", "tx"):
        match = re.search(rf"^\s*{direction}\s+bitrate:\s*([\d.]+)\s+([KMG]?)Bit/s\b",
                          output, re.M | re.I)
        if match:
            try:
                value = float(match[1]) * {"": 1e-6, "K": .001, "M": 1., "G": 1000.}[match[2].upper()]
                if math.isfinite(value) and value >= 0:
                    result[f"{direction}_link_mbps"] = value
            except ValueError:
                pass
    return result


class NetworkMetrics:
    """Keep one sampler for the lifetime of a backend service.

    Injectable sysfs root, reader and clock allow deterministic tests. Rates use
    monotonic time and reset after counter rollback, interface replacement or
    disappearance. The first sample establishes a baseline and reports zero.
    """

    def __init__(self, sysfs_root: Path | str = "/sys/class/net", *,
                 clock: Callable[[], float] = time.monotonic,
                 reader: Callable[[Path], str] | None = None,
                 interface: str = "ap0"):
        if not _INTERFACE.fullmatch(interface):
            raise ValueError("Invalid hotspot interface")
        self.sysfs_root = Path(sysfs_root)
        self.clock = clock
        self.reader = reader or (lambda path: path.read_text())
        self.interface = interface
        self._previous: tuple[str, float, int, int] | None = None
        self._upstream_previous: tuple[str, str, float, int, int] | None = None
        self._client_previous: dict[str, tuple[str, float, int, int, int]] = {}
        self._ap_identity = ""
        self._last_now = None

    def _read(self, path: Path) -> str | None:
        try:
            return self.reader(path).strip()
        except (OSError, ValueError, UnicodeError):
            return None

    def _traffic(self, now: float) -> dict:
        result = {"download_bps": 0.0, "upload_bps": 0.0,
                  "download_total_bytes": 0, "upload_total_bytes": 0,
                  "traffic_available": False}
        base = self.sysfs_root / self.interface
        rx_text = self._read(base / "statistics" / "rx_bytes")
        tx_text = self._read(base / "statistics" / "tx_bytes")
        try:
            rx, tx = int(rx_text), int(tx_text)
            if rx < 0 or tx < 0:
                raise ValueError("Negative interface counters")
        except (TypeError, ValueError):
            self._previous = None
            self._ap_identity = ""
            return result
        identity = self._read(base / "ifindex") or self.interface
        self._ap_identity = identity
        result.update(download_total_bytes=tx, upload_total_bytes=rx, traffic_available=True)
        if self._read(base / "operstate") == "down":
            self._previous = None
            return result
        previous = self._previous
        self._previous = (identity, now, rx, tx)
        if previous is not None:
            old_identity, old_time, old_rx, old_tx = previous
            elapsed = now - old_time
            if (identity == old_identity and elapsed > 0 and math.isfinite(elapsed)
                    and rx >= old_rx and tx >= old_tx):
                result["download_bps"] = (tx - old_tx) / elapsed
                result["upload_bps"] = (rx - old_rx) / elapsed
        return result

    def _upstream_traffic(self, upstream: str, now: float) -> dict:
        result = {"upstream_download_bps": 0.0, "upstream_upload_bps": 0.0,
                  "upstream_download_total_bytes": 0, "upstream_upload_total_bytes": 0,
                  "upstream_traffic_available": False}
        if not _INTERFACE.fullmatch(upstream):
            self._upstream_previous = None
            return result
        base = self.sysfs_root / upstream
        try:
            rx = int(self._read(base / "statistics" / "rx_bytes"))
            tx = int(self._read(base / "statistics" / "tx_bytes"))
            if rx < 0 or tx < 0:
                raise ValueError("Negative interface counters")
        except (TypeError, ValueError):
            self._upstream_previous = None
            return result
        result.update(upstream_download_total_bytes=rx, upstream_upload_total_bytes=tx,
                      upstream_traffic_available=True)
        if self._read(base / "operstate") == "down":
            self._upstream_previous = None
            return result
        identity = self._read(base / "ifindex") or upstream
        previous = self._upstream_previous
        self._upstream_previous = (upstream, identity, now, rx, tx)
        if previous:
            old_iface, old_id, old_time, old_rx, old_tx = previous
            elapsed = now - old_time
            if (upstream == old_iface and identity == old_id and elapsed > 0
                    and math.isfinite(elapsed) and rx >= old_rx and tx >= old_tx):
                result["upstream_download_bps"] = (rx - old_rx) / elapsed
                result["upstream_upload_bps"] = (tx - old_tx) / elapsed
        return result

    def enrich_clients(self, clients: list[dict], *, running=True, sampled_at=None) -> list[dict]:
        """Add per-station rates, rebasing when clients leave/rejoin or reset.

        Station counters are AP-side: TX is the client's download, RX its upload.
        The sampler does not infer a manufacturer or a person's identity.
        """
        now = (float(sampled_at) if isinstance(sampled_at, (int, float))
               and math.isfinite(sampled_at) else self.clock())
        previous, current, result = self._client_previous, {}, []
        for raw in clients if running else []:
            client = dict(raw)
            client.update(download_bps=0.0, upload_bps=0.0)
            signal = client.get("signal")
            client["signal_percent"] = max(0, min(100, 2 * (signal + 100))) if isinstance(signal, int) else None
            mac = client.get("mac")
            rx, tx = client.get("rx_bytes"), client.get("tx_bytes")
            connected = client.get("connected_seconds", 0)
            if (isinstance(mac, str) and type(rx) is int and type(tx) is int
                    and type(connected) is int and min(rx, tx, connected) >= 0):
                current[mac] = (self._ap_identity, now, rx, tx, connected)
                old = previous.get(mac)
                if old:
                    identity, old_time, old_rx, old_tx, old_connected = old
                    elapsed = now - old_time
                    if (identity == self._ap_identity and elapsed > 0 and math.isfinite(elapsed)
                            and rx >= old_rx and tx >= old_tx and connected >= old_connected):
                        client["download_bps"] = (tx - old_tx) / elapsed
                        client["upload_bps"] = (rx - old_rx) / elapsed
            result.append(client)
        self._client_previous = current
        return result

    def sample(self, manager) -> dict:
        """Return JSON-safe measurements without changing any network state.

        The manager must expose `settings` and `_run(args, check=, timeout=)`.
        Missing `iw`, unsupported Wi-Fi attributes, unplugged adapters and
        Ethernet upstreams produce unavailable signal/link values, not errors.
        """
        metrics = parse_wifi_link("")
        upstream = str(manager.settings.get("upstream", ""))
        if _INTERFACE.fullmatch(upstream):
            try:
                output = manager._run(["iw", "dev", upstream, "link"], check=False, timeout=2)
                metrics.update(parse_wifi_link(output))
            except (RuntimeError, OSError, ValueError):
                pass
        now = self.clock()
        self._last_now = now
        metrics.update(self._traffic(now))
        metrics.update(self._upstream_traffic(upstream, now))
        metrics["link_utilization_percent"] = None
        rx_capacity, tx_capacity = metrics["rx_link_mbps"], metrics["tx_link_mbps"]
        if (metrics["upstream_traffic_available"] and rx_capacity and tx_capacity):
            # Approximation from negotiated PHY rates; MAC overhead, retries and
            # half-duplex airtime mean this is not an ISP utilization figure.
            utilization = (metrics["upstream_download_bps"] * 8 / (rx_capacity * 1_000_000)
                           + metrics["upstream_upload_bps"] * 8 / (tx_capacity * 1_000_000)) * 100
            metrics["link_utilization_percent"] = round(min(100.0, max(0.0, utilization)), 1)
        return metrics
