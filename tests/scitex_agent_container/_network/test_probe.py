"""Tests for the WSL↔hub connectivity probe (todo#457).

All tests run offline by injecting fake behaviour into the module's
socket / urllib surface. Real network probes are exercised by
``tests/integration`` (not included here — this suite must stay green
on any CI host, including ones with no outbound egress).
"""

from __future__ import annotations

import json
import socket
from pathlib import Path

import pytest

from scitex_agent_container._network import probe as np


# ── probe_dns ────────────────────────────────────────────────────────────────


class TestProbeDNS:
    def test_ok_returns_sorted_addrs(self, monkeypatch):
        def fake_getaddrinfo(host, port, *a, **kw):
            return [
                (socket.AF_INET, 1, 6, "", ("1.2.3.4", 0)),
                (socket.AF_INET6, 1, 6, "", ("::1", 0, 0, 0)),
                (socket.AF_INET, 1, 6, "", ("1.2.3.4", 0)),  # dedup
            ]

        monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)
        r = np.probe_dns("example.test")
        assert r.ok is True
        assert r.name == "dns"
        assert r.extra["addrs"] == ["1.2.3.4", "::1"]
        assert r.latency_ms >= 0.0

    def test_failure_records_error(self, monkeypatch):
        def boom(*a, **kw):
            raise socket.gaierror("Name or service not known")

        monkeypatch.setattr(socket, "getaddrinfo", boom)
        r = np.probe_dns("example.test")
        assert r.ok is False
        assert "gaierror" in r.err


# ── probe_tcp ────────────────────────────────────────────────────────────────


class TestProbeTCP:
    def test_ok_closes_connection(self, monkeypatch):
        class FakeSock:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                pass

        def fake_create_connection(addr, timeout):
            assert addr == ("hub.test", 443)
            return FakeSock()

        monkeypatch.setattr(socket, "create_connection", fake_create_connection)
        r = np.probe_tcp("hub.test", 443)
        assert r.ok is True
        assert r.name == "tcp"

    def test_failure_records_error(self, monkeypatch):
        def boom(*a, **kw):
            raise TimeoutError("timed out")

        monkeypatch.setattr(socket, "create_connection", boom)
        r = np.probe_tcp("hub.test", 443, timeout=0.1)
        assert r.ok is False
        assert "TimeoutError" in r.err


# ── probe_https ──────────────────────────────────────────────────────────────


class TestProbeHTTPS:
    def test_2xx_ok(self, monkeypatch):
        class FakeResp:
            status = 200

            def __enter__(self):
                return self

            def __exit__(self, *a):
                pass

        monkeypatch.setattr(
            np.urllib.request,
            "urlopen",
            lambda *a, **kw: FakeResp(),
        )
        r = np.probe_https("https://hub.test/")
        assert r.ok is True
        assert r.extra["status"] == 200

    def test_404_counts_as_transport_ok(self, monkeypatch):
        """A 404 still proves TLS + HTTP handshake completed."""

        class FakeResp:
            status = 404

            def __enter__(self):
                return self

            def __exit__(self, *a):
                pass

        monkeypatch.setattr(
            np.urllib.request,
            "urlopen",
            lambda *a, **kw: FakeResp(),
        )
        r = np.probe_https("https://hub.test/")
        assert r.ok is True  # <500 is still transport ok
        assert r.extra["status"] == 404

    def test_expected_status_prefix_tightens(self, monkeypatch):
        class FakeResp:
            status = 404

            def __enter__(self):
                return self

            def __exit__(self, *a):
                pass

        monkeypatch.setattr(
            np.urllib.request,
            "urlopen",
            lambda *a, **kw: FakeResp(),
        )
        r = np.probe_https("https://hub.test/", expected_status_prefix="2")
        assert r.ok is False
        assert "status=404" in r.err

    def test_connection_refused_fails(self, monkeypatch):
        def boom(*a, **kw):
            raise ConnectionRefusedError("refused")

        monkeypatch.setattr(np.urllib.request, "urlopen", boom)
        r = np.probe_https("https://hub.test/")
        assert r.ok is False
        assert "ConnectionRefusedError" in r.err


# ── default-gateway parsing ──────────────────────────────────────────────────


class TestParseDefaultGateway:
    def test_extracts_ipv4_gateway(self):
        text = (
            "default via 192.168.11.1 dev eth0 proto kernel metric 25\n"
            "172.17.0.0/16 dev docker0 proto kernel scope link\n"
        )
        assert np._parse_default_gateway(text) == "192.168.11.1"

    def test_returns_none_when_no_default(self):
        assert np._parse_default_gateway("172.17.0.0/16 dev docker0\n") is None

    def test_empty_input(self):
        assert np._parse_default_gateway("") is None


class TestProbeGateway:
    def test_no_default_route_is_fail(self, monkeypatch):
        r = np.probe_gateway(ip_route_reader=lambda: "")
        assert r.ok is False
        assert "no default route" in r.err

    def test_reachable_gateway_ok(self, monkeypatch):
        class FakeSock:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                pass

        monkeypatch.setattr(
            socket,
            "create_connection",
            lambda addr, timeout: FakeSock(),
        )
        r = np.probe_gateway(
            ip_route_reader=lambda: "default via 10.0.0.1 dev eth0\n"
        )
        assert r.ok is True
        assert r.extra["gateway"] == "10.0.0.1"

    def test_unreachable_gateway_fail(self, monkeypatch):
        def boom(addr, timeout):
            raise TimeoutError("timed out")

        monkeypatch.setattr(socket, "create_connection", boom)
        r = np.probe_gateway(
            ip_route_reader=lambda: "default via 10.0.0.1 dev eth0\n",
            timeout=0.1,
        )
        assert r.ok is False
        assert "TimeoutError" in r.err
        assert r.extra["gateway"] == "10.0.0.1"


# ── probe_cloudflared ───────────────────────────────────────────────────────


class TestProbeCloudflared:
    def test_process_found_returns_ok(self, monkeypatch):
        import subprocess
        from unittest.mock import MagicMock

        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = "12345\n"
        monkeypatch.setattr(subprocess, "run", lambda *a, **kw: mock_result)
        r = np.probe_cloudflared()
        assert r.ok is True
        assert r.name == "cloudflared"
        assert r.extra.get("pid") == 12345

    def test_process_not_found_returns_failure(self, monkeypatch):
        import subprocess
        from unittest.mock import MagicMock

        mock_result = MagicMock()
        mock_result.returncode = 1
        mock_result.stdout = ""
        monkeypatch.setattr(subprocess, "run", lambda *a, **kw: mock_result)
        r = np.probe_cloudflared()
        assert r.ok is False
        assert "not found" in r.err

    def test_pgrep_missing_returns_failure(self, monkeypatch):
        import subprocess

        def raise_fnf(*a, **kw):
            raise FileNotFoundError("pgrep")

        monkeypatch.setattr(subprocess, "run", raise_fnf)
        r = np.probe_cloudflared()
        assert r.ok is False
        assert "pgrep" in r.err


# ── run_all_probes / summarise ───────────────────────────────────────────────


class TestRunAll:
    def test_order_is_dns_gateway_tcp_https_cloudflared(self, monkeypatch):
        calls: list[str] = []

        def log(name):
            def _inner(*a, **kw):
                calls.append(name)
                return np.ProbeResult(name=name, ok=True, latency_ms=1.0)

            return _inner

        monkeypatch.setattr(np, "probe_dns", log("dns"))
        monkeypatch.setattr(np, "probe_gateway", log("gateway"))
        monkeypatch.setattr(np, "probe_tcp", log("tcp"))
        monkeypatch.setattr(np, "probe_https", log("https"))
        monkeypatch.setattr(np, "probe_cloudflared", log("cloudflared"))

        np.run_all_probes()
        assert calls == ["dns", "gateway", "tcp", "https", "cloudflared"]

    def test_summarise_all_ok(self):
        results = [
            np.ProbeResult(name="dns", ok=True, latency_ms=1.0),
            np.ProbeResult(name="https", ok=True, latency_ms=2.0),
        ]
        s = np.summarise(results)
        assert s["ok"] is True
        assert len(s["probes"]) == 2
        assert s["probes"][0]["name"] == "dns"

    def test_summarise_any_fail(self):
        results = [
            np.ProbeResult(name="dns", ok=True, latency_ms=1.0),
            np.ProbeResult(name="https", ok=False, latency_ms=2.0, err="refused"),
        ]
        s = np.summarise(results)
        assert s["ok"] is False


# ── append_result / run_and_log ──────────────────────────────────────────────


class TestLogging:
    def test_append_result_writes_jsonl(self, tmp_path: Path):
        summary = {"ts": "2026-04-21T00:00:00+00:00", "ok": True, "probes": []}
        path = np.append_result("head-ywata-note-win", summary, root=tmp_path)
        assert path is not None
        text = path.read_text()
        assert json.loads(text.strip()) == summary

    def test_append_result_appends_multiple(self, tmp_path: Path):
        np.append_result("a", {"i": 1}, root=tmp_path)
        np.append_result("a", {"i": 2}, root=tmp_path)
        lines = (tmp_path / "a.jsonl").read_text().splitlines()
        assert [json.loads(l) for l in lines] == [{"i": 1}, {"i": 2}]

    def test_append_result_sanitises_agent_name(self, tmp_path: Path):
        path = np.append_result("weird/name with spaces", {}, root=tmp_path)
        assert path is not None
        assert "/" not in path.name
        assert " " not in path.name

    def test_run_and_log_end_to_end(self, tmp_path: Path, monkeypatch):
        monkeypatch.setattr(
            np,
            "run_all_probes",
            lambda **kw: [np.ProbeResult(name="dns", ok=True, latency_ms=1.0)],
        )
        summary = np.run_and_log("head-ywata-note-win", root=tmp_path)
        assert summary["ok"] is True
        path = tmp_path / "head-ywata-note-win.jsonl"
        assert path.exists()
