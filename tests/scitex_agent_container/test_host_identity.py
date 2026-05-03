"""Tests for host_identity (local-vs-remote resolver)."""

from __future__ import annotations

import os as _os

import pytest
import yaml

from scitex_agent_container._network import host_identity as hi
from scitex_agent_container.config import AgentConfig
from scitex_agent_container.config._types import RemoteSpec


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    path = tmp_path / "host-identity.yaml"
    monkeypatch.setattr(hi, "HOST_IDENTITY_PATH", path)
    hi._reset_cache_for_tests()
    yield path
    hi._reset_cache_for_tests()


def _patch_basics(monkeypatch, hostname="testhost"):
    monkeypatch.setattr("socket.gethostname", lambda: hostname)
    fake_uname = _os.uname_result(("Linux", hostname, "1.0", "", "x86_64"))
    monkeypatch.setattr("os.uname", lambda: fake_uname)
    # Suppress scitex_resource so host machine aliases don't leak into tests.
    monkeypatch.setattr(hi, "_load_resource_aliases", lambda: set())
    hi._reset_cache_for_tests()


def test_is_local_host_accepts_none_and_empty(monkeypatch):
    _patch_basics(monkeypatch)
    assert hi.is_local_host(None) is True
    assert hi.is_local_host("") is True
    assert hi.is_local_host("   ") is True


def test_loopback_does_not_leak_other_canonical(_isolate, monkeypatch):
    """Regression: loopback names must not pull in foreign alias lists."""
    _patch_basics(monkeypatch, hostname="DXP480TPLUS-994")
    _isolate.write_text(yaml.safe_dump({"aliases": ["nas", "DXP480TPLUS-994"]}))
    hi._reset_cache_for_tests()
    assert hi.is_local_host("nas") is True
    assert hi.is_local_host("mba") is False
    assert hi.is_local_host("spartan") is False


def test_is_local_host_matches_hostname(monkeypatch):
    _patch_basics(monkeypatch, hostname="mba")
    assert hi.is_local_host("mba") is True
    assert hi.is_local_host("nas") is False


def test_is_local_host_case_insensitive(monkeypatch):
    _patch_basics(monkeypatch, hostname="nas")
    assert hi.is_local_host("NAS") is True
    assert hi.is_local_host("Nas") is True


def test_yaml_file_aliases(_isolate, monkeypatch):
    _patch_basics(monkeypatch, hostname="DXP480TPLUS-994")
    _isolate.write_text(yaml.safe_dump({"aliases": ["nas", "ugreen"]}))
    hi._reset_cache_for_tests()
    assert hi.is_local_host("nas") is True
    assert hi.is_local_host("ugreen") is True
    assert hi.is_local_host("DXP480TPLUS-994") is True


def test_yaml_file_malformed_raises(_isolate, monkeypatch):
    _isolate.write_text("aliases: [unterminated")
    hi._reset_cache_for_tests()
    with pytest.raises(RuntimeError, match="Invalid YAML"):
        hi.get_local_identities()


def test_yaml_file_non_mapping_raises(_isolate, monkeypatch):
    _isolate.write_text("- just\n- a\n- list\n")
    hi._reset_cache_for_tests()
    with pytest.raises(RuntimeError, match="must be a YAML mapping"):
        hi.get_local_identities()


def test_yaml_file_aliases_not_list_raises(_isolate, monkeypatch):
    _isolate.write_text(yaml.safe_dump({"aliases": "not-a-list"}))
    hi._reset_cache_for_tests()
    with pytest.raises(RuntimeError, match="must be a list"):
        hi.get_local_identities()


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
    _patch_basics(monkeypatch, hostname="hostA")
    assert hi.is_local_host("hostA") is True
    assert hi.is_local_host("hostB") is False
    hi._reset_cache_for_tests()
    _patch_basics(monkeypatch, hostname="hostB")
    assert hi.is_local_host("hostB") is True
