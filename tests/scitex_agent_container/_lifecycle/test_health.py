"""Tests for _lifecycle.health — probe-style helpers + monitor loop.

Covers:
* health_check dispatcher (sdk-alive, a2a-card, unknown)
* _check_sdk_alive happy/sad
* _check_a2a_card happy, missing config, HTTP error, URL error, JSON error,
  name mismatch
* health_monitor loop: never policy, on-failure with restart, exit on
  registry removal, exit when max_retries reached, restart_fn exception
  is swallowed.
"""

from __future__ import annotations

import json
import urllib.error
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from scitex_agent_container._lifecycle import health as health_mod
from scitex_agent_container.config._types import (
    AgentConfig,
    HealthSpec,
    RestartSpec,
)


@pytest.fixture(autouse=True)
def _home_to_tmp(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))


def _make_cfg(
    name: str = "ag1",
    method: str = "sdk-alive",
    policy: str = "never",
    max_retries: int = 3,
) -> AgentConfig:
    cfg = AgentConfig(name=name)
    cfg.health = HealthSpec(method=method, interval=0)
    cfg.restart = RestartSpec(
        policy=policy,
        max_retries=max_retries,
        backoff_initial=0,
        backoff_max=0,
        backoff_multiplier=2,
    )
    return cfg


# ---------------------------------------------------------------------------
# health_check dispatcher
# ---------------------------------------------------------------------------


def test_health_check_unknown_method_returns_false() -> None:
    cfg = _make_cfg(method="bogus")
    ok, msg = health_mod.health_check(cfg)
    assert ok is False
    assert "Unknown health method" in msg


def test_health_check_sdk_alive_routes_to_helper() -> None:
    cfg = _make_cfg(method="sdk-alive")
    with patch.object(health_mod, "_check_sdk_alive", return_value=(True, "ok")):
        ok, msg = health_mod.health_check(cfg)
    assert (ok, msg) == (True, "ok")


def test_health_check_a2a_card_routes_to_helper() -> None:
    cfg = _make_cfg(method="a2a-card")
    with patch.object(health_mod, "_check_a2a_card", return_value=(False, "bad")):
        ok, msg = health_mod.health_check(cfg)
    assert (ok, msg) == (False, "bad")


# ---------------------------------------------------------------------------
# _check_sdk_alive
# ---------------------------------------------------------------------------


def test_check_sdk_alive_healthy() -> None:
    cfg = _make_cfg()
    fake_runtime = MagicMock()
    fake_runtime.return_value.is_running.return_value = True
    with patch(
        "scitex_agent_container.runtimes.claude_session.ClaudeSessionRuntime",
        fake_runtime,
    ):
        ok, msg = health_mod._check_sdk_alive(cfg)
    assert ok is True
    assert msg == "healthy"


def test_check_sdk_alive_unhealthy() -> None:
    cfg = _make_cfg()
    fake_runtime = MagicMock()
    fake_runtime.return_value.is_running.return_value = False
    with patch(
        "scitex_agent_container.runtimes.claude_session.ClaudeSessionRuntime",
        fake_runtime,
    ):
        ok, msg = health_mod._check_sdk_alive(cfg)
    assert ok is False
    assert "SDK runner not running" in msg


# ---------------------------------------------------------------------------
# _check_a2a_card
# ---------------------------------------------------------------------------


def _patch_a2a_block(monkeypatch, block: Any) -> None:
    import scitex_agent_container.runtimes.a2a_sidecar as side

    monkeypatch.setattr(side, "_read_a2a_block", lambda _cfg: block)


def test_check_a2a_card_missing_block(monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = _make_cfg(method="a2a-card")
    _patch_a2a_block(monkeypatch, None)
    ok, msg = health_mod._check_a2a_card(cfg)
    assert ok is False
    assert "spec.a2a not set" in msg


def _make_resp(payload: dict | bytes) -> MagicMock:
    resp = MagicMock()
    if isinstance(payload, dict):
        payload = json.dumps(payload).encode()
    resp.read.return_value = payload
    resp.__enter__ = lambda s: s
    resp.__exit__ = MagicMock(return_value=False)
    return resp


def test_check_a2a_card_happy(monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = _make_cfg(name="ag1", method="a2a-card")
    _patch_a2a_block(monkeypatch, {"host": "127.0.0.1", "port": 9999})
    resp = _make_resp({"name": "ag1"})
    with patch("urllib.request.urlopen", return_value=resp):
        ok, msg = health_mod._check_a2a_card(cfg)
    assert ok is True
    assert "healthy" in msg
    assert "127.0.0.1:9999" in msg


def test_check_a2a_card_http_error(monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = _make_cfg(name="ag1", method="a2a-card")
    _patch_a2a_block(monkeypatch, {"port": 9999})
    err = urllib.error.HTTPError(
        url="http://x", code=503, msg="boom", hdrs=None, fp=None
    )
    with patch("urllib.request.urlopen", side_effect=err):
        ok, msg = health_mod._check_a2a_card(cfg)
    assert ok is False
    assert "HTTP 503" in msg


def test_check_a2a_card_url_error(monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = _make_cfg(name="ag1", method="a2a-card")
    _patch_a2a_block(monkeypatch, {"port": 9999})
    with patch(
        "urllib.request.urlopen",
        side_effect=urllib.error.URLError("dead"),
    ):
        ok, msg = health_mod._check_a2a_card(cfg)
    assert ok is False
    assert "unreachable" in msg


def test_check_a2a_card_oserror(monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = _make_cfg(name="ag1", method="a2a-card")
    _patch_a2a_block(monkeypatch, {"port": 9999})
    with patch("urllib.request.urlopen", side_effect=OSError("conn refused")):
        ok, msg = health_mod._check_a2a_card(cfg)
    assert ok is False
    assert "unreachable" in msg


def test_check_a2a_card_bad_json(monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = _make_cfg(name="ag1", method="a2a-card")
    _patch_a2a_block(monkeypatch, {"port": 9999})
    resp = _make_resp(b"not json{")
    with patch("urllib.request.urlopen", return_value=resp):
        ok, msg = health_mod._check_a2a_card(cfg)
    assert ok is False
    assert "malformed JSON" in msg


def test_check_a2a_card_name_mismatch(monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = _make_cfg(name="ag1", method="a2a-card")
    _patch_a2a_block(monkeypatch, {"port": 9999})
    resp = _make_resp({"name": "different"})
    with patch("urllib.request.urlopen", return_value=resp):
        ok, msg = health_mod._check_a2a_card(cfg)
    assert ok is False
    assert "name mismatch" in msg


@pytest.mark.xfail(
    strict=True,
    reason=(
        "Bug surfaced: _check_a2a_card calls data.get('name') in the "
        "mismatch-branch f-string before checking isinstance, so a list "
        "payload raises AttributeError instead of returning (False, mismatch)."
    ),
)
def test_check_a2a_card_non_dict_payload(monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = _make_cfg(name="ag1", method="a2a-card")
    _patch_a2a_block(monkeypatch, {"port": 9999})
    resp = _make_resp(b"[]")
    with patch("urllib.request.urlopen", return_value=resp):
        ok, msg = health_mod._check_a2a_card(cfg)
    assert ok is False
    assert "name mismatch" in msg


# ---------------------------------------------------------------------------
# health_monitor loop
# ---------------------------------------------------------------------------


class _FakeRegistry:
    """Minimal Registry stand-in: existence flips off after N calls."""

    def __init__(self, exists_for: int = 1) -> None:
        self._calls = 0
        self._exists_for = exists_for

    def exists(self, _name: str) -> bool:
        self._calls += 1
        return self._calls <= self._exists_for


def _fake_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(health_mod.time, "sleep", lambda _s: None)


def test_health_monitor_exits_when_registry_removes_agent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cfg = _make_cfg(policy="never")
    _fake_sleep(monkeypatch)
    reg = _FakeRegistry(exists_for=0)  # removed before first check
    # health_check should never be called
    with patch.object(
        health_mod, "health_check", side_effect=AssertionError("must not call")
    ):
        health_mod.health_monitor("ag1", cfg, reg)  # returns


def test_health_monitor_never_policy_does_not_restart(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cfg = _make_cfg(policy="never")
    _fake_sleep(monkeypatch)
    reg = _FakeRegistry(exists_for=2)  # exists once, then removed
    restart_calls: list[Any] = []
    with patch.object(health_mod, "health_check", return_value=(False, "bad")):
        health_mod.health_monitor(
            "ag1",
            cfg,
            reg,
            restart_fn=lambda c: restart_calls.append(c),
        )
    assert restart_calls == []


def test_health_monitor_on_failure_calls_restart_then_gives_up(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cfg = _make_cfg(policy="on-failure", max_retries=2)
    _fake_sleep(monkeypatch)
    reg = _FakeRegistry(exists_for=100)
    restart_calls: list[Any] = []
    with patch.object(health_mod, "health_check", return_value=(False, "bad")):
        health_mod.health_monitor(
            "ag1",
            cfg,
            reg,
            restart_fn=lambda c: restart_calls.append(c),
        )
    # Should have called restart up to max_retries times then exited.
    assert len(restart_calls) == 2


def test_health_monitor_resets_retries_after_healthy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cfg = _make_cfg(policy="always", max_retries=2)
    _fake_sleep(monkeypatch)
    reg = _FakeRegistry(exists_for=100)

    # 1 unhealthy → restart → healthy (resets retries) → unhealthy →
    # restart → unhealthy → restart → give up (max_retries=2 after reset).
    results = iter(
        [
            (False, "x"),
            (True, "ok"),
            (False, "x"),
            (False, "x"),
            (False, "x"),
        ]
    )

    def _check(_cfg):
        try:
            return next(results)
        except StopIteration:
            return (False, "x")

    restart_calls: list[Any] = []
    with patch.object(health_mod, "health_check", side_effect=_check):
        health_mod.health_monitor(
            "ag1",
            cfg,
            reg,
            restart_fn=lambda c: restart_calls.append(c),
        )
    # 1 (first fail) + 2 more after reset = 3
    assert len(restart_calls) == 3


def test_health_monitor_swallows_restart_fn_exception(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cfg = _make_cfg(policy="on-failure", max_retries=1)
    _fake_sleep(monkeypatch)
    reg = _FakeRegistry(exists_for=100)

    def _bad_restart(_c):
        raise RuntimeError("kaboom")

    with patch.object(health_mod, "health_check", return_value=(False, "bad")):
        # Should not raise.
        health_mod.health_monitor("ag1", cfg, reg, restart_fn=_bad_restart)


def test_health_monitor_no_restart_fn_with_unhealthy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If restart_fn is None and policy is on-failure, monitor still
    increments retries and eventually returns."""
    cfg = _make_cfg(policy="on-failure", max_retries=1)
    _fake_sleep(monkeypatch)
    reg = _FakeRegistry(exists_for=100)
    with patch.object(health_mod, "health_check", return_value=(False, "bad")):
        health_mod.health_monitor("ag1", cfg, reg, restart_fn=None)
