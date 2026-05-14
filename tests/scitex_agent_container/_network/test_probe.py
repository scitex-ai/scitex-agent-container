"""Tests for the WSL↔hub connectivity probe (todo#457).

No-mocks pattern: probe_dns / probe_tcp / probe_https / probe_gateway
all accept injection seams (``resolver=`` / ``connector=`` / ``opener=``)
so tests can substitute hand-rolled callables. ``probe_cloudflared``
shells out via real ``subprocess.run``; tests install a real fake
``pgrep`` binary on PATH via the ``subprocess_shim`` fixture.

Real network probes are exercised by ``tests/integration`` — this
suite stays green on any host (including offline CI) by either
injecting seams or pointing at the RFC2606-reserved ``hub.example``
domain (which fails locally, deterministically).
"""

from __future__ import annotations

import json
import socket
from pathlib import Path

from scitex_agent_container._network import probe as np

# ---------------------------------------------------------------------------
# Hand-rolled fakes (no mocks). Each is a real object/callable with the
# protocol the production code expects.
# ---------------------------------------------------------------------------


class _FakeConn:
    """Context-manager TCP/HTTPS connection stand-in."""

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _FakeResp:
    """urlopen response stand-in — supports ``with`` + ``.status``."""

    def __init__(self, status: int = 200):
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _resolver_returning(addrs):
    """Build a resolver that returns the given list of (af, ..., sockaddr)."""

    def resolver(host, port, *a, **kw):
        return addrs

    return resolver


def _raising(exc):
    def f(*a, **kw):
        raise exc

    return f


# ===========================================================================
# probe_dns
# ===========================================================================


class TestProbeDNS:
    def test_ok_marks_result_ok_true(self):
        # Arrange
        resolver = _resolver_returning(
            [
                (socket.AF_INET, 1, 6, "", ("1.2.3.4", 0)),
                (socket.AF_INET6, 1, 6, "", ("::1", 0, 0, 0)),
            ]
        )
        # Act
        r = np.probe_dns("example.test", resolver=resolver)
        # Assert
        assert r.ok is True

    def test_ok_sets_name_to_dns(self):
        # Arrange
        resolver = _resolver_returning([(socket.AF_INET, 1, 6, "", ("1.2.3.4", 0))])
        # Act
        r = np.probe_dns("example.test", resolver=resolver)
        # Assert
        assert r.name == "dns"

    def test_ok_dedupes_and_sorts_addrs(self):
        # Arrange
        resolver = _resolver_returning(
            [
                (socket.AF_INET, 1, 6, "", ("1.2.3.4", 0)),
                (socket.AF_INET6, 1, 6, "", ("::1", 0, 0, 0)),
                (socket.AF_INET, 1, 6, "", ("1.2.3.4", 0)),  # dup
            ]
        )
        # Act
        r = np.probe_dns("example.test", resolver=resolver)
        # Assert
        assert r.extra["addrs"] == ["1.2.3.4", "::1"]

    def test_ok_records_nonnegative_latency(self):
        # Arrange
        resolver = _resolver_returning([(socket.AF_INET, 1, 6, "", ("1.2.3.4", 0))])
        # Act
        r = np.probe_dns("example.test", resolver=resolver)
        # Assert
        assert r.latency_ms >= 0.0

    def test_failure_marks_result_ok_false(self):
        # Arrange
        resolver = _raising(socket.gaierror("Name or service not known"))
        # Act
        r = np.probe_dns("example.test", resolver=resolver)
        # Assert
        assert r.ok is False

    def test_failure_records_exception_type_in_err(self):
        # Arrange
        resolver = _raising(socket.gaierror("Name or service not known"))
        # Act
        r = np.probe_dns("example.test", resolver=resolver)
        # Assert
        assert "gaierror" in r.err


# ===========================================================================
# probe_tcp
# ===========================================================================


class TestProbeTCP:
    def test_ok_marks_result_ok_true(self):
        # Arrange
        def connector(addr, timeout):
            return _FakeConn()

        # Act
        r = np.probe_tcp("hub.test", 443, connector=connector)
        # Assert
        assert r.ok is True

    def test_ok_sets_name_to_tcp(self):
        # Arrange
        def connector(addr, timeout):
            return _FakeConn()

        # Act
        r = np.probe_tcp("hub.test", 443, connector=connector)
        # Assert
        assert r.name == "tcp"

    def test_ok_passes_host_and_port_to_connector(self):
        # Arrange
        seen = {}

        def connector(addr, timeout):
            seen["addr"] = addr
            return _FakeConn()

        # Act
        np.probe_tcp("hub.test", 443, connector=connector)
        # Assert
        assert seen["addr"] == ("hub.test", 443)

    def test_failure_marks_result_ok_false(self):
        # Arrange
        connector = _raising(TimeoutError("timed out"))
        # Act
        r = np.probe_tcp("hub.test", 443, timeout=0.1, connector=connector)
        # Assert
        assert r.ok is False

    def test_failure_records_exception_type_in_err(self):
        # Arrange
        connector = _raising(TimeoutError("timed out"))
        # Act
        r = np.probe_tcp("hub.test", 443, timeout=0.1, connector=connector)
        # Assert
        assert "TimeoutError" in r.err


# ===========================================================================
# probe_https
# ===========================================================================


def _opener_returning(status):
    def opener(req, timeout=None, context=None):
        return _FakeResp(status)

    return opener


class TestProbeHTTPS:
    def test_2xx_marks_result_ok_true(self):
        # Arrange
        opener = _opener_returning(200)
        # Act
        r = np.probe_https("https://hub.test/", opener=opener)
        # Assert
        assert r.ok is True

    def test_2xx_records_status_200(self):
        # Arrange
        opener = _opener_returning(200)
        # Act
        r = np.probe_https("https://hub.test/", opener=opener)
        # Assert
        assert r.extra["status"] == 200

    def test_404_counts_as_transport_ok(self):
        """A 404 still proves TLS + HTTP handshake completed."""
        # Arrange
        opener = _opener_returning(404)
        # Act
        r = np.probe_https("https://hub.test/", opener=opener)
        # Assert
        assert r.ok is True

    def test_404_records_status_404(self):
        # Arrange
        opener = _opener_returning(404)
        # Act
        r = np.probe_https("https://hub.test/", opener=opener)
        # Assert
        assert r.extra["status"] == 404

    def test_expected_status_prefix_tightens_404_fails(self):
        # Arrange
        opener = _opener_returning(404)
        # Act
        r = np.probe_https(
            "https://hub.test/", expected_status_prefix="2", opener=opener
        )
        # Assert
        assert r.ok is False

    def test_expected_status_prefix_records_status_in_err(self):
        # Arrange
        opener = _opener_returning(404)
        # Act
        r = np.probe_https(
            "https://hub.test/", expected_status_prefix="2", opener=opener
        )
        # Assert
        assert "status=404" in r.err

    def test_connection_refused_marks_result_ok_false(self):
        # Arrange
        opener = _raising(ConnectionRefusedError("refused"))
        # Act
        r = np.probe_https("https://hub.test/", opener=opener)
        # Assert
        assert r.ok is False

    def test_connection_refused_records_exception_type_in_err(self):
        # Arrange
        opener = _raising(ConnectionRefusedError("refused"))
        # Act
        r = np.probe_https("https://hub.test/", opener=opener)
        # Assert
        assert "ConnectionRefusedError" in r.err


# ===========================================================================
# _parse_default_gateway (pure function)
# ===========================================================================


class TestParseDefaultGateway:
    def test_extracts_ipv4_gateway(self):
        # Arrange
        text = (
            "default via 192.168.11.1 dev eth0 proto kernel metric 25\n"
            "172.17.0.0/16 dev docker0 proto kernel scope link\n"
        )
        # Act
        gw = np._parse_default_gateway(text)
        # Assert
        assert gw == "192.168.11.1"

    def test_returns_none_when_no_default(self):
        # Arrange
        text = "172.17.0.0/16 dev docker0\n"
        # Act
        gw = np._parse_default_gateway(text)
        # Assert
        assert gw is None

    def test_empty_input_returns_none(self):
        # Arrange
        text = ""
        # Act
        gw = np._parse_default_gateway(text)
        # Assert
        assert gw is None


# ===========================================================================
# probe_gateway
# ===========================================================================


class TestProbeGateway:
    def test_no_default_route_marks_ok_false(self):
        # Arrange
        reader = lambda: ""
        # Act
        r = np.probe_gateway(ip_route_reader=reader)
        # Assert
        assert r.ok is False

    def test_no_default_route_records_err_message(self):
        # Arrange
        reader = lambda: ""
        # Act
        r = np.probe_gateway(ip_route_reader=reader)
        # Assert
        assert "no default route" in r.err

    def test_reachable_gateway_marks_ok_true(self):
        # Arrange
        reader = lambda: "default via 10.0.0.1 dev eth0\n"
        connector = lambda addr, timeout: _FakeConn()
        # Act
        r = np.probe_gateway(ip_route_reader=reader, connector=connector)
        # Assert
        assert r.ok is True

    def test_reachable_gateway_records_gateway_in_extra(self):
        # Arrange
        reader = lambda: "default via 10.0.0.1 dev eth0\n"
        connector = lambda addr, timeout: _FakeConn()
        # Act
        r = np.probe_gateway(ip_route_reader=reader, connector=connector)
        # Assert
        assert r.extra["gateway"] == "10.0.0.1"

    def test_unreachable_gateway_marks_ok_false(self):
        # Arrange
        reader = lambda: "default via 10.0.0.1 dev eth0\n"
        connector = _raising(TimeoutError("timed out"))
        # Act
        r = np.probe_gateway(ip_route_reader=reader, connector=connector, timeout=0.1)
        # Assert
        assert r.ok is False

    def test_unreachable_gateway_records_exception_type_in_err(self):
        # Arrange
        reader = lambda: "default via 10.0.0.1 dev eth0\n"
        connector = _raising(TimeoutError("timed out"))
        # Act
        r = np.probe_gateway(ip_route_reader=reader, connector=connector, timeout=0.1)
        # Assert
        assert "TimeoutError" in r.err

    def test_unreachable_gateway_still_records_gateway_in_extra(self):
        # Arrange
        reader = lambda: "default via 10.0.0.1 dev eth0\n"
        connector = _raising(TimeoutError("timed out"))
        # Act
        r = np.probe_gateway(ip_route_reader=reader, connector=connector, timeout=0.1)
        # Assert
        assert r.extra["gateway"] == "10.0.0.1"


# ===========================================================================
# probe_cloudflared — uses real subprocess.run + a PATH-installed fake
# binary (subprocess_shim) instead of monkeypatching subprocess.
# ===========================================================================


class TestProbeCloudflared:
    def test_process_found_marks_ok_true(self, subprocess_shim):
        # Arrange
        subprocess_shim.install("pgrep", exit=0, stdout="12_345\n")
        # Act
        r = np.probe_cloudflared()
        # Assert
        assert r.ok is True

    def test_process_found_sets_name_to_cloudflared(self, subprocess_shim):
        # Arrange
        subprocess_shim.install("pgrep", exit=0, stdout="12_345\n")
        # Act
        r = np.probe_cloudflared()
        # Assert
        assert r.name == "cloudflared"

    def test_process_found_records_pid_in_extra(self, subprocess_shim):
        # Arrange
        subprocess_shim.install("pgrep", exit=0, stdout="12_345\n")
        # Act
        r = np.probe_cloudflared()
        # Assert
        assert r.extra.get("pid") == 12_345

    def test_process_not_found_marks_ok_false(self, subprocess_shim):
        # Arrange
        subprocess_shim.install("pgrep", exit=1, stdout="")
        # Act
        r = np.probe_cloudflared()
        # Assert
        assert r.ok is False

    def test_process_not_found_records_err_message(self, subprocess_shim):
        # Arrange
        subprocess_shim.install("pgrep", exit=1, stdout="")
        # Act
        r = np.probe_cloudflared()
        # Assert
        assert "not found" in r.err

    def test_pgrep_missing_marks_ok_false(
        self, subprocess_shim, env_save_restore, tmp_path
    ):
        # Arrange: point PATH at an empty directory so pgrep is unfindable.
        empty = tmp_path / "_empty_path"
        empty.mkdir()
        env_save_restore.set("PATH", str(empty))
        # Act
        r = np.probe_cloudflared()
        # Assert
        assert r.ok is False

    def test_pgrep_missing_records_pgrep_in_err(
        self, subprocess_shim, env_save_restore, tmp_path
    ):
        # Arrange
        empty = tmp_path / "_empty_path"
        empty.mkdir()
        env_save_restore.set("PATH", str(empty))
        # Act
        r = np.probe_cloudflared()
        # Assert
        assert "pgrep" in r.err


# ===========================================================================
# run_all_probes — order is visible in the returned list. We verify it
# end-to-end against the RFC2606-reserved ``hub.example`` host so the
# real probes deterministically fail without any network.
# ===========================================================================


class TestRunAllProbes:
    def test_order_is_dns_gateway_tcp_https_cloudflared(self):
        # Arrange
        # Act
        results = np.run_all_probes(
            hub_host="hub.example",
            hub_url="https://hub.example/",
            timeout=0.1,
        )
        # Assert
        assert [r.name for r in results] == [
            "dns",
            "gateway",
            "tcp",
            "https",
            "cloudflared",
        ]


# ===========================================================================
# summarise (pure)
# ===========================================================================


class TestSummarise:
    def test_all_ok_marks_summary_ok_true(self):
        # Arrange
        results = [
            np.ProbeResult(name="dns", ok=True, latency_ms=1.0),
            np.ProbeResult(name="https", ok=True, latency_ms=2.0),
        ]
        # Act
        s = np.summarise(results)
        # Assert
        assert s["ok"] is True

    def test_all_ok_records_each_probe_in_summary(self):
        # Arrange
        results = [
            np.ProbeResult(name="dns", ok=True, latency_ms=1.0),
            np.ProbeResult(name="https", ok=True, latency_ms=2.0),
        ]
        # Act
        s = np.summarise(results)
        # Assert
        assert [p["name"] for p in s["probes"]] == ["dns", "https"]

    def test_any_failure_marks_summary_ok_false(self):
        # Arrange
        results = [
            np.ProbeResult(name="dns", ok=True, latency_ms=1.0),
            np.ProbeResult(name="https", ok=False, latency_ms=2.0, err="refused"),
        ]
        # Act
        s = np.summarise(results)
        # Assert
        assert s["ok"] is False


# ===========================================================================
# append_result / run_and_log — real tmp_path + real probes injection
# ===========================================================================


class TestAppendResult:
    def test_writes_jsonl_file(self, tmp_path: Path):
        # Arrange
        summary = {"ts": "2026-04-21T00:00:00+00:00", "ok": True, "probes": []}
        # Act
        path = np.append_result("head-ywata-note-win", summary, root=tmp_path)
        # Assert
        assert path is not None

    def test_writes_summary_payload(self, tmp_path: Path):
        # Arrange
        summary = {"ts": "2026-04-21T00:00:00+00:00", "ok": True, "probes": []}
        # Act
        path = np.append_result("head-ywata-note-win", summary, root=tmp_path)
        # Assert
        assert json.loads(path.read_text().strip()) == summary

    def test_appends_multiple_lines(self, tmp_path: Path):
        # Arrange
        np.append_result("a", {"i": 1}, root=tmp_path)
        np.append_result("a", {"i": 2}, root=tmp_path)
        # Act
        lines = (tmp_path / "a.jsonl").read_text().splitlines()
        # Assert
        assert [json.loads(l) for l in lines] == [{"i": 1}, {"i": 2}]

    def test_sanitises_slash_in_agent_name(self, tmp_path: Path):
        # Arrange
        # Act
        path = np.append_result("weird/name with spaces", {}, root=tmp_path)
        # Assert
        assert "/" not in path.name

    def test_sanitises_spaces_in_agent_name(self, tmp_path: Path):
        # Arrange
        # Act
        path = np.append_result("weird/name with spaces", {}, root=tmp_path)
        # Assert
        assert " " not in path.name


class TestRunAndLog:
    def test_end_to_end_writes_summary_ok_true(self, tmp_path: Path):
        # Arrange: inject a probes callable that returns one ok result.
        def fake_probes(**_kw):
            return [np.ProbeResult(name="dns", ok=True, latency_ms=1.0)]

        # Act
        summary = np.run_and_log(
            "head-ywata-note-win", root=tmp_path, probes=fake_probes
        )
        # Assert
        assert summary["ok"] is True

    def test_end_to_end_writes_jsonl_to_root(self, tmp_path: Path):
        # Arrange
        def fake_probes(**_kw):
            return [np.ProbeResult(name="dns", ok=True, latency_ms=1.0)]

        # Act
        np.run_and_log("head-ywata-note-win", root=tmp_path, probes=fake_probes)
        # Assert
        assert (tmp_path / "head-ywata-note-win.jsonl").exists()
