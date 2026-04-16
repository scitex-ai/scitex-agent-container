"""Tests for host_identity and runtime-selection local fallback (todo#294)."""

from __future__ import annotations

import logging
from pathlib import Path
from unittest.mock import patch

import pytest

from scitex_agent_container import host_identity
from scitex_agent_container.config import AgentConfig
from scitex_agent_container.config._types import RemoteSpec
from scitex_agent_container.host_identity import (
    get_local_identities,
    is_local_host,
    _reset_cache_for_tests,
)


@pytest.fixture(autouse=True)
def _reset_cache(monkeypatch):
    _reset_cache_for_tests()
    # Isolate from real env + real HOME by default
    monkeypatch.delenv("SCITEX_AGENT_LOCAL_HOSTS", raising=False)
    yield
    _reset_cache_for_tests()


def _patch_basics(monkeypatch, hostname="testhost", fqdn="testhost.local"):
    monkeypatch.setattr("socket.gethostname", lambda: hostname)
    monkeypatch.setattr("socket.getfqdn", lambda: fqdn)


def test_is_local_host_accepts_none_and_empty(monkeypatch):
    _patch_basics(monkeypatch)
    assert is_local_host(None) is True
    assert is_local_host("") is True
    assert is_local_host("   ") is True


def test_local_does_not_leak_other_canonical_via_localhost(monkeypatch):
    """Regression: loopback names must not pull in foreign alias lists.

    NAS's 'localhost' / '127.0.0.1' / '::1' are universally present and
    previously caused is_local_host('mba') to return True on the NAS,
    because DEFAULT_HOST_ALIASES['mba'] happened to include 'localhost'.
    """
    import os as _os

    _reset_cache_for_tests()
    monkeypatch.setattr("socket.gethostname", lambda: "DXP480TPLUS-994")
    monkeypatch.setattr("socket.getfqdn", lambda: "DXP480TPLUS-994")

    # os.uname().nodename on the test host could otherwise leak a real
    # hostname (e.g. "Yusukes-MacBook-Air.local") that happens to match
    # another canonical alias list.
    _fake_uname = _os.uname_result(
        ("Linux", "DXP480TPLUS-994", "6.1.27", "", "x86_64")
    )
    monkeypatch.setattr("os.uname", lambda: _fake_uname)
    monkeypatch.delenv("SCITEX_AGENT_LOCAL_HOSTS", raising=False)
    assert is_local_host("nas") is True     # legitimate auto-detect
    assert is_local_host("mba") is False    # must not leak via loopback
    assert is_local_host("spartan") is False
    assert is_local_host("ywata-note-win") is False


def test_is_local_host_matches_hostname(monkeypatch):
    _patch_basics(monkeypatch, hostname="mba", fqdn="mba")
    assert is_local_host("mba") is True
    _reset_cache_for_tests()
    _patch_basics(monkeypatch, hostname="mba", fqdn="mba")
    assert is_local_host("nas") is False


def test_is_local_host_matches_fqdn(monkeypatch):
    _patch_basics(monkeypatch, hostname="mba", fqdn="Yusukes-MacBook-Air.local")
    assert is_local_host("Yusukes-MacBook-Air.local") is True
    assert is_local_host("Yusukes-MacBook-Air") is True
    assert is_local_host("mba") is True


def test_is_local_host_case_insensitive(monkeypatch):
    _patch_basics(monkeypatch, hostname="nas", fqdn="nas")
    assert is_local_host("NAS") is True
    assert is_local_host("Nas") is True


def test_env_var_adds_aliases(monkeypatch):
    _patch_basics(monkeypatch)
    monkeypatch.setenv("SCITEX_AGENT_LOCAL_HOSTS", "foo,bar, baz ")
    assert is_local_host("foo") is True
    assert is_local_host("bar") is True
    assert is_local_host("baz") is True


def test_env_var_overrides_empty(monkeypatch):
    _patch_basics(monkeypatch, hostname="uniquehost", fqdn="uniquehost")
    monkeypatch.delenv("SCITEX_AGENT_LOCAL_HOSTS", raising=False)
    assert is_local_host("foo") is False
    assert is_local_host("uniquehost") is True


def test_yaml_file_aliases(monkeypatch, tmp_path):
    home = tmp_path / "home"
    (home / ".scitex" / "agent-container").mkdir(parents=True)
    (home / ".scitex" / "agent-container" / "host_aliases.yaml").write_text(
        "local:\n  - alpha\n  - beta\n"
    )
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    _patch_basics(monkeypatch)
    assert is_local_host("alpha") is True
    assert is_local_host("beta") is True


def test_yaml_file_malformed_fallback(monkeypatch, tmp_path, caplog):
    home = tmp_path / "home"
    (home / ".scitex" / "agent-container").mkdir(parents=True)
    (home / ".scitex" / "agent-container" / "host_aliases.yaml").write_text(
        "not: valid: yaml: ::\n  - [unclosed\n"
    )
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    _patch_basics(monkeypatch, hostname="somehost", fqdn="somehost")
    with caplog.at_level(logging.WARNING, logger="scitex_agent_container.host_identity"):
        ids = get_local_identities()
    assert "somehost" in ids
    assert any("host_aliases.yaml" in r.message for r in caplog.records)


def test_default_auto_detect_by_seed_match(monkeypatch):
    _patch_basics(monkeypatch, hostname="DXP480TPLUS-994", fqdn="DXP480TPLUS-994")
    assert is_local_host("nas") is True
    assert is_local_host("ugreen") is True


def test_runtime_selection_falls_back_to_local(monkeypatch):
    from scitex_agent_container.runtimes import claude_code as cc

    cfg = AgentConfig(name="t")
    cfg.remote = RemoteSpec(host="nas")
    monkeypatch.setattr(cc, "is_local_host", lambda name: True)
    assert cc._should_dispatch_remote(cfg) is False


def test_runtime_selection_stays_remote_when_not_local(monkeypatch):
    from scitex_agent_container.runtimes import claude_code as cc

    cfg = AgentConfig(name="t")
    cfg.remote = RemoteSpec(host="nas")
    monkeypatch.setattr(cc, "is_local_host", lambda name: False)
    assert cc._should_dispatch_remote(cfg) is True


def test_cache_reset_for_tests(monkeypatch):
    _patch_basics(monkeypatch, hostname="hostA", fqdn="hostA")
    assert is_local_host("hostA") is True
    assert is_local_host("hostB") is False
    _reset_cache_for_tests()
    _patch_basics(monkeypatch, hostname="hostB", fqdn="hostB")
    monkeypatch.setenv("SCITEX_AGENT_LOCAL_HOSTS", "hostB")
    assert is_local_host("hostB") is True
