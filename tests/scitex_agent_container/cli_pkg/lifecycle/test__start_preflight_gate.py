#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""An unloadable spec must be refused by NAME, never by the lead credential.

Measured 2026-08-19. ``POST /agents`` for an agent name with no spec on
this host answered::

    HTTP 502
    {"returncode": 1, "stdout": "",
     "stderr": "Error: OAuth token in /home/ywatanabe/.claude/.credentials.json
                expired 257594 seconds ago. Run `claude login` to refresh."}

Every word of that is true about the FILE and none of it is about the
REQUEST. The caller (hub, trying to stand up a scholar agent) could not
tell whether to fix their call or wait for the daemon owner, and spent
the interval reporting a permissions bug that did not exist.

Cause: ``_iter_target_configs`` swallowed the spec load error and yielded
a bare ``None``; the gate then asked the only question it had left — is
the lead's ``~/.claude/.credentials.json`` fresh? — and reported THAT.
Two unrelated faults printed the same sentence, and only one of them was
fixable by the caller.

Operator ruling, 2026-08-19: 「勝手にデフォルトのクレデンシャルズを使わない」
— a silent fall back to a default is what the constitution forbids, and
the requirement is to fail loudly and early instead.

These tests pin both halves: the refusal NAMES the load error, and the
lead credential file is not consulted on any path through this gate.

PA-306: no ``unittest.mock`` / ``monkeypatch``. Production collaborators
are swapped at the module namespace with an explicit save/restore
``_swap``, matching ``test__restart_noop_reports_false.py``.
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import Callable, Iterator

import scitex_agent_container._state._preflight_creds as creds_mod
import scitex_agent_container.config as config_mod
import scitex_agent_container.config._resolve as resolve_mod
from scitex_agent_container.cli_pkg.lifecycle._start_preflight_gate import (
    any_target_needs_anthropic_oauth,
    make_preflight_runner,
)

_MISSING_SPEC = "spec file is absent on this host"


@contextmanager
def _swap(module: object, name: str, fn: Callable) -> Iterator[None]:
    saved = getattr(module, name)
    setattr(module, name, fn)
    try:
        yield
    finally:
        setattr(module, name, saved)


class _Claude:
    def __init__(self, provider=None):
        self.provider = provider


class _Cfg:
    def __init__(self, name="agent", provider=None):
        self.name = name
        self.claude = _Claude(provider)


def _runner(target: str):
    return make_preflight_runner(
        single_targets=[target],
        bulk_yamls=[],
        no_redispatch=False,
        broker_self=False,
    )


@contextmanager
def _spec_that_will_not_load(message: str) -> Iterator[None]:
    def _boom(_resolved):
        raise FileNotFoundError(message)

    with _swap(resolve_mod, "resolve_with_prefix", lambda raw: raw):
        with _swap(config_mod, "load_config", _boom):
            yield


@contextmanager
def _spec_that_loads(cfg: _Cfg) -> Iterator[None]:
    with _swap(resolve_mod, "resolve_with_prefix", lambda raw: raw):
        with _swap(config_mod, "load_config", lambda _r: cfg):
            yield


def _refuse(target: str = "scitex-scholar", message: str = _MISSING_SPEC) -> str:
    """Run the gate against an unloadable spec; return its exit status word.

    Swallows the ``SystemExit`` here so each test spends its single
    permitted assertion on the BEHAVIOUR rather than on the raise.
    """
    run = _runner(target)
    with _spec_that_will_not_load(message):
        with _swap(creds_mod, "check_spec_oauth_credentials", lambda *a, **k: None):
            try:
                run()
            except SystemExit:
                return "refused"
    return "dispatched"


def _refusal_stderr(capsys, message: str = _MISSING_SPEC) -> str:
    _refuse(message=message)
    return capsys.readouterr().err


def _lead_check_calls() -> list[int]:
    calls: list[int] = []
    run = _runner("scitex-scholar")
    with _spec_that_will_not_load(_MISSING_SPEC):
        with _swap(creds_mod, "check_oauth_token_expiry", lambda *a, **k: calls.append(1)):
            with _swap(creds_mod, "check_spec_oauth_credentials", lambda *a, **k: None):
                try:
                    run()
                except SystemExit:
                    pass
    return calls


def _spec_checked_with(cfg: _Cfg) -> list[object]:
    seen: list[object] = []
    run = _runner(cfg.name)
    with _spec_that_loads(cfg):
        with _swap(
            creds_mod,
            "check_spec_oauth_credentials",
            lambda c, *a, **k: seen.append(c),
        ):
            run()
    return seen


def test_an_unloadable_spec_is_refused_rather_than_dispatched():
    # Arrange
    target = "scitex-scholar"
    # Act
    outcome = _refuse(target)
    # Assert
    assert outcome == "refused"


def test_the_refusal_names_the_target(capsys):
    # Arrange
    expected = "scitex-scholar"
    # Act
    err = _refusal_stderr(capsys)
    # Assert
    assert expected in err


def test_the_refusal_carries_the_underlying_load_error(capsys):
    # Arrange
    expected = _MISSING_SPEC
    # Act
    err = _refusal_stderr(capsys, message=expected)
    # Assert
    assert expected in err


def test_the_refusal_does_not_blame_an_expired_credential(capsys):
    # Arrange: the old gate reported the lead file's expiry here.
    forbidden = "expired"
    # Act
    err = _refusal_stderr(capsys)
    # Assert
    assert forbidden not in err


def test_the_lead_credential_check_is_never_called_for_an_unloadable_spec():
    # Arrange: a tripwire on the exact function the old gate called.
    expected: list[int] = []
    # Act
    calls = _lead_check_calls()
    # Assert
    assert calls == expected


def test_a_loadable_spec_still_reaches_the_spec_credential_check():
    # Arrange
    cfg = _Cfg(name="paper-scitex-clew")
    # Act
    seen = _spec_checked_with(cfg)
    # Assert
    assert seen == [cfg]


def test_a_provider_backed_spec_skips_the_credential_check():
    # Arrange
    cfg = _Cfg(name="handyman-01", provider="qwen38")
    # Act
    seen = _spec_checked_with(cfg)
    # Assert
    assert seen == []


def test_an_unloadable_spec_still_counts_as_needing_oauth():
    # Arrange
    target = "scitex-scholar"
    # Act
    with _spec_that_will_not_load(_MISSING_SPEC):
        result = any_target_needs_anthropic_oauth([target], [])
    # Assert
    assert result is True


def test_a_provider_backed_spec_does_not_need_oauth():
    # Arrange
    cfg = _Cfg(name="handyman-01", provider="qwen38")
    # Act
    with _spec_that_loads(cfg):
        result = any_target_needs_anthropic_oauth([cfg.name], [])
    # Assert
    assert result is False
