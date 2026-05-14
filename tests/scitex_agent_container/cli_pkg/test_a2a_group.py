"""Tests for cli_pkg.a2a_group (a2a serve + a2a doctor)."""

from __future__ import annotations

import json
import urllib.error
from pathlib import Path

from click.testing import CliRunner

import scitex_agent_container.cli_pkg.a2a_group as a2a_mod
from scitex_agent_container.cli_pkg.a2a_group import _emit, a2a


def _write_spec(tmp_path: Path, name: str, port: int | None = 8888) -> Path:
    d = tmp_path / name
    d.mkdir()
    spec = d / "spec.yaml"
    if port is None:
        spec.write_text("spec:\n  a2a:\n    host: 127.0.0.1\n")
    else:
        spec.write_text(f"spec:\n  a2a:\n    host: 127.0.0.1\n    port: {port}\n")
    return spec


# ---------------------------------------------------------------------------
# _emit
# ---------------------------------------------------------------------------


def test_emit_human_healthy(capsys):
    _emit(
        {
            "ok": True,
            "agent": "foo",
            "elapsed_ms": 7,
            "url": "http://h/x",
        },
        as_json=False,
    )
    out = capsys.readouterr().out
    assert "[foo] healthy" in out
    assert "7 ms" in out


def test_emit_human_unhealthy(capsys):
    _emit(
        {
            "ok": False,
            "agent": "foo",
            "url": "http://h/x",
            "error": "boom",
        },
        as_json=False,
    )
    err = capsys.readouterr().err
    assert "[foo] unhealthy" in err
    assert "boom" in err


def test_emit_human_unhealthy_no_url(capsys):
    _emit({"ok": False, "agent": "foo", "error": "x"}, as_json=False)
    err = capsys.readouterr().err
    assert "(no URL)" in err


def test_emit_json(capsys):
    _emit({"ok": True, "agent": "foo"}, as_json=True)
    out = capsys.readouterr().out.strip()
    assert json.loads(out) == {"ok": True, "agent": "foo"}


# ---------------------------------------------------------------------------
# a2a serve — call dispatched
# ---------------------------------------------------------------------------


def test_a2a_serve_calls_serve(monkeypatch, tmp_path):
    spec = _write_spec(tmp_path, "ag")
    calls = {}

    def fake_serve(yamls, host, port, handler):
        calls["host"] = host
        calls["port"] = port
        calls["handler"] = handler
        calls["yamls"] = list(yamls)

    monkeypatch.setattr(a2a_mod, "serve", fake_serve)
    runner = CliRunner()
    result = runner.invoke(a2a, ["serve", str(spec), "--port", "9000"])
    assert result.exit_code == 0, result.output
    assert calls["port"] == 9000
    assert calls["yamls"][0] == spec


def test_a2a_serve_verbose_calls_serve(monkeypatch, tmp_path):
    spec = _write_spec(tmp_path, "ag")
    monkeypatch.setattr(a2a_mod, "serve", lambda *a, **kw: None)
    runner = CliRunner()
    result = runner.invoke(a2a, ["serve", str(spec), "-v"])
    assert result.exit_code == 0


# ---------------------------------------------------------------------------
# a2a doctor
# ---------------------------------------------------------------------------


def test_doctor_no_port_exits_2(tmp_path):
    spec = _write_spec(tmp_path, "ag", port=None)
    runner = CliRunner()
    result = runner.invoke(a2a, ["doctor", str(spec)])
    assert result.exit_code == 2


def test_doctor_no_port_json_exits_2(tmp_path):
    spec = _write_spec(tmp_path, "ag", port=None)
    runner = CliRunner()
    result = runner.invoke(a2a, ["doctor", str(spec), "--json"])
    assert result.exit_code == 2
    # Body is on stdout as JSON
    payload = json.loads(result.output.strip())
    assert payload["ok"] is False
    assert "port" in payload["error"]


def _fake_response(body: dict):
    class _R:
        def __init__(self, data):
            self._data = json.dumps(data).encode()

        def read(self):
            return self._data

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    return _R(body)


def test_doctor_healthy(monkeypatch, tmp_path):
    spec = _write_spec(tmp_path, "ag")
    monkeypatch.setattr(
        a2a_mod.urllib.request,
        "urlopen",
        lambda url, timeout: _fake_response({"name": "ag", "url": "http://x"}),
    )
    runner = CliRunner()
    result = runner.invoke(a2a, ["doctor", str(spec), "--json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output.strip())
    assert payload["ok"] is True
    assert payload["agent"] == "ag"


def test_doctor_name_mismatch(monkeypatch, tmp_path):
    spec = _write_spec(tmp_path, "ag")
    monkeypatch.setattr(
        a2a_mod.urllib.request,
        "urlopen",
        lambda url, timeout: _fake_response({"name": "other"}),
    )
    runner = CliRunner()
    result = runner.invoke(a2a, ["doctor", str(spec), "--json"])
    assert result.exit_code == 1
    payload = json.loads(result.output.strip())
    assert payload["ok"] is False
    assert "name mismatch" in payload["error"]


def test_doctor_http_error(monkeypatch, tmp_path):
    spec = _write_spec(tmp_path, "ag")

    def raise_http(url, timeout):
        raise urllib.error.HTTPError(url, 503, "Service Unavailable", {}, None)

    monkeypatch.setattr(a2a_mod.urllib.request, "urlopen", raise_http)
    runner = CliRunner()
    result = runner.invoke(a2a, ["doctor", str(spec), "--json"])
    assert result.exit_code == 1
    payload = json.loads(result.output.strip())
    assert "HTTP 503" in payload["error"]


def test_doctor_url_error(monkeypatch, tmp_path):
    spec = _write_spec(tmp_path, "ag")

    def raise_url(url, timeout):
        raise urllib.error.URLError("connection refused")

    monkeypatch.setattr(a2a_mod.urllib.request, "urlopen", raise_url)
    runner = CliRunner()
    result = runner.invoke(a2a, ["doctor", str(spec), "--json"])
    assert result.exit_code == 1
    payload = json.loads(result.output.strip())
    assert "URLError" in payload["error"]


def test_doctor_invalid_json_response(monkeypatch, tmp_path):
    spec = _write_spec(tmp_path, "ag")

    class _R:
        def read(self):
            return b"not json"

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(a2a_mod.urllib.request, "urlopen", lambda u, timeout: _R())
    runner = CliRunner()
    result = runner.invoke(a2a, ["doctor", str(spec), "--json"])
    assert result.exit_code == 1
    payload = json.loads(result.output.strip())
    assert "JSONDecodeError" in payload["error"]


def test_doctor_port_override(monkeypatch, tmp_path):
    """--port overrides the spec's port."""
    spec = _write_spec(tmp_path, "ag", port=None)
    captured = {}

    def fake_open(url, timeout):
        captured["url"] = url
        return _fake_response({"name": "ag"})

    monkeypatch.setattr(a2a_mod.urllib.request, "urlopen", fake_open)
    runner = CliRunner()
    result = runner.invoke(a2a, ["doctor", str(spec), "--port", "9999", "--json"])
    assert result.exit_code == 0, result.output
    assert ":9999/" in captured["url"]
