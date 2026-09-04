"""``--engine`` must survive the hop to the peer, on BOTH lifecycle legs.

THE BUG THESE PIN, measured 2026-09-05 on scitex-compute-04.

``sac agents restart business --engine qwen38-27b --yes`` printed:

    --engine 'qwen38-27b' cannot be honoured for 'business': that agent runs
    on a PEER, and the cross-host restart re-runs `sac agents restart` there
    through an argv that carries no engine field -- the engine would be
    silently dropped and the agent would restart on its DEFAULT engine.

Two things were wrong with that, and the second is the serious one.

1. The refusal was placed AFTER the dispatch it claimed to prevent. By the
   time the text appeared the peer had already restarted the agent on its
   default engine, the lead had closed the old instances row and opened a
   new one, and the CLI then reported ``restarted: false``. "The flag was
   refused" and "the flag was ignored and the work was done anyway" are not
   the same event, and only the first was reported.

2. The sibling START dispatch dropped ``--engine`` with NO message at all,
   which is worse: the agent comes up, looks healthy, and runs the wrong
   backend.

The fix forwards the flag instead of refusing it, so the command means the
same thing from any machine. These tests fail loudly if either argv reverts
to a literal.

No mocks: the restart argv is asserted through the extracted pure builder,
and the start leg through the injected ``dispatcher`` seam that
``try_dispatch`` already exposes for hermetic routing tests.
"""

from __future__ import annotations

from scitex_agent_container.cli_pkg.lifecycle._restart_remote import (
    remote_restart_argv,
)

_ENGINE = "qwen38-27b"


# ---------------------------------------------------------------------------
# the RESTART leg
# ---------------------------------------------------------------------------


def test_restart_argv_carries_the_engine_flag():
    # Arrange
    name = "business"
    # Act
    argv = remote_restart_argv(name, _ENGINE)
    # Assert
    assert "--engine" in argv


def test_restart_argv_carries_the_engine_value():
    # Arrange
    name = "business"
    # Act
    argv = remote_restart_argv(name, _ENGINE)
    # Assert
    assert argv[argv.index("--engine") + 1] == _ENGINE


def test_restart_argv_without_an_engine_is_unchanged():
    # Arrange -- the historical argv, byte for byte.
    name = "business"
    # Act
    argv = remote_restart_argv(name)
    # Assert
    assert argv == ["sac", "agents", "restart", "business", "--yes", "--json"]


def test_restart_argv_still_names_the_agent_first():
    # Arrange -- the engine must not displace the positional.
    name = "business"
    # Act
    argv = remote_restart_argv(name, _ENGINE)
    # Assert
    assert argv[:4] == ["sac", "agents", "restart", "business"]


def test_restart_argv_keeps_json_so_the_peer_verdict_stays_parseable():
    # Arrange -- the caller parses the peer's JSON envelope.
    name = "business"
    # Act
    argv = remote_restart_argv(name, _ENGINE)
    # Assert
    assert "--json" in argv


def test_restart_argv_treats_an_empty_engine_as_no_engine():
    # Arrange -- an empty string must not become a bare `--engine`.
    name = "business"
    # Act
    argv = remote_restart_argv(name, "")
    # Assert
    assert "--engine" not in argv


# ---------------------------------------------------------------------------
# the START leg
# ---------------------------------------------------------------------------


def _dispatcher():
    """Recording stand-in for the ssh handoff that RECORDS the engine."""
    calls: list[dict] = []

    def _fn(
        *,
        name: str,
        peer: str,
        dry_run: bool,
        force: bool,
        engine: str | None = None,
    ) -> int:
        calls.append({"name": name, "peer": peer, "engine": engine})
        return 0

    _fn.calls = calls  # type: ignore[attr-defined]
    return _fn


def _try_dispatch_with(engine, dispatcher):
    """Route a peer-pinned agent through the real ``try_dispatch``."""
    from scitex_agent_container._state.host_config import PeerSpec
    from scitex_agent_container.cli_pkg.lifecycle._dispatch import try_dispatch

    class _Hosts:
        host = "peer-a"

    class _Cfg:
        name = "alpha"
        hosts_spec = _Hosts()

    return try_dispatch(
        _Cfg(),
        "this-machine",
        {"peer-a": PeerSpec(name="peer-a", ssh="peer-a")},
        dry_run=False,
        force=False,
        engine=engine,
        local_names={"this-machine"},
        dispatcher=dispatcher,
    )


def test_start_dispatch_forwards_the_engine_to_the_peer():
    # Arrange
    dispatcher = _dispatcher()
    # Act
    _try_dispatch_with(_ENGINE, dispatcher)
    # Assert
    assert dispatcher.calls[0]["engine"] == _ENGINE


def test_start_dispatch_without_an_engine_forwards_none():
    # Arrange -- the control: no engine stays no engine, never a default.
    dispatcher = _dispatcher()
    # Act
    _try_dispatch_with(None, dispatcher)
    # Assert
    assert dispatcher.calls[0]["engine"] is None


def test_start_dispatch_still_routes_to_the_pinned_peer():
    # Arrange -- the engine must not disturb the routing decision.
    dispatcher = _dispatcher()
    # Act
    _try_dispatch_with(_ENGINE, dispatcher)
    # Assert
    assert dispatcher.calls[0]["peer"] == "peer-a"
