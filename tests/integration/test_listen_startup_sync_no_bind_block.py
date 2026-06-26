"""Regression: an unreachable static peer must NOT block the ``sac listen`` bind.

INCIDENT 2026-06-26
===================
The central ``sac listen`` daemon silently failed to serve — the process
was alive but port 7878 was never bound (curl returned 000), with NO error
logged → the whole fleet lost agent-to-agent comms.

Root cause (this vector): ``cli_pkg.listen_cmds._maybe_sync_on_start`` ran a
SYNCHRONOUS ``sac registry sync --all`` over ssh to every static peer
(``comms_nodes.peers``) BEFORE ``uvicorn.run`` was reached. That sync had no
overall timeout, so a single powered-off peer made the ssh call HANG, blocking
boot before the bind. PR #469's bind-watchdog could not catch this — it lives
inside ``create_app``, which runs *after* the pre-bind sync.

The fix proven here
===================
1. The blocking peer-sync no longer runs on the pre-bind path. It runs
   best-effort AFTER the bind, off the event loop, as a lifespan task
   (``_listen._startup_peer_sync.sync_peers_on_listen_startup``).
2. Defense in depth: each per-peer ssh has a hard ``ConnectTimeout`` + an
   overall ``subprocess.run(timeout=...)``, and the ``--all`` sweep honours an
   overall budget — so even a re-introduced pre-bind call fails fast.

No mocks (STX-NM002)
====================
These tests exercise REAL behaviour:

* The "unreachable peer" is a REAL ssh subprocess dialing a routable-but-dead
  address — ``192.0.2.1`` (RFC 5737 TEST-NET-1, guaranteed never routable). The
  kernel/ssh ``ConnectTimeout`` makes the connect fail fast; nothing is mocked.
* The bind is a REAL ephemeral TCP socket (``socket.socket().bind``), standing
  in for the port uvicorn owns, asserted to stay bound across the sync.

The whole point is timing: a hang would never return; we assert the sync
RETURNS within a wall-clock bound far below an infinite block.

TQ: module docstring states intent (TQ001); each test asserts exactly one fact
(TQ007); the shared (slow, real-ssh) work is computed once per scenario in a
fixture so the single-assert split costs no extra ssh round-trips.
"""

from __future__ import annotations

import asyncio
import importlib
import socket
import time
from dataclasses import dataclass
from pathlib import Path

import pytest
import yaml

# A routable-but-dead address: RFC 5737 TEST-NET-1. Packets to it are dropped
# (never refused-fast, never routed), so an ssh connect can ONLY terminate via
# ``ConnectTimeout`` — the realistic "host powered off" condition, with zero
# dependence on any external service being up or down.
_DEAD_PEER_SSH = "192.0.2.1"

# Hard ceiling for "did it hang?". The per-peer ssh ConnectTimeout is 5s and
# the overall sweep budget we pass is small; a correct implementation returns
# in well under this. An un-timed (buggy) sync would block effectively forever,
# so any value comfortably above one ConnectTimeout but far below "forever"
# proves the fix. We give generous headroom for slow CI while still being a
# decisive hang-vs-return discriminator.
_MAX_WALLCLOCK_S = 45.0


@pytest.fixture(scope="module")
def dead_peer_env(tmp_path_factory: pytest.TempPathFactory):
    """Real config.yaml whose only static peer is the dead TEST-NET-1 host.

    Module-scoped on purpose: the heavy work that depends on this (a REAL ssh
    connect to a dead host, bounded only by ConnectTimeout) is slow, so we set
    up the env ONCE and let the per-scenario fixtures below run their single
    ssh sweep once each — keeping the one-assert-per-test split (TQ007) from
    multiplying real connect-timeout waits. Env + state.db are saved and
    restored by hand because pytest's ``monkeypatch`` / the repo's
    ``env_save_restore`` are function-scoped.

    Also pins state.db so the sync's import side has a real, isolated DB to
    write to (it won't, since the peer is unreachable, but the code path must
    have a valid target).
    """
    import os

    tmp = tmp_path_factory.mktemp("dead_peer_env")
    cfg = tmp / "config.yaml"
    cfg.write_text(
        yaml.safe_dump(
            {
                "host": {"canonical": "test-local"},
                "peers": {
                    # Single static peer, pointed at the dead address. No
                    # ``via`` hops — we want to exercise the direct ssh path.
                    "deadpeer": {"ssh": _DEAD_PEER_SSH},
                },
            }
        )
    )
    db = tmp / "state.db"

    saved = {
        k: os.environ.get(k)
        for k in ("SCITEX_AGENT_CONTAINER_CONFIG", "SCITEX_AGENT_CONTAINER_STATE_DB")
    }
    os.environ["SCITEX_AGENT_CONTAINER_CONFIG"] = str(cfg)
    os.environ["SCITEX_AGENT_CONTAINER_STATE_DB"] = str(db)
    import scitex_agent_container._state.state_db as _state_db_mod

    importlib.reload(_state_db_mod)
    try:
        yield tmp
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        importlib.reload(_state_db_mod)


def _bind_ephemeral_socket() -> socket.socket:
    """Bind+listen on a real ephemeral 127.0.0.1 port (stands in for uvicorn)."""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind(("127.0.0.1", 0))
    s.listen(8)
    return s


# ---------------------------------------------------------------------------
# Defense-in-depth: the synchronous all-peers sync returns fast on a dead peer.
# ---------------------------------------------------------------------------


@dataclass
class _SyncOutcome:
    rc: int
    elapsed_s: float
    output: str


@pytest.fixture(scope="module")
def sync_all_outcome(dead_peer_env: Path) -> _SyncOutcome:
    """Run the all-peers sync ONCE against the dead peer; capture the outcome.

    Module-scoped so the single real-ssh round-trip is shared by every
    assertion below — the one-assert-per-test split (TQ007) adds no extra
    connect-timeout waits. Output is captured via ``redirect_std*`` into a
    buffer (not ``capsys``, which is function-scoped) so the sync's loud
    ``click.echo(..., err=True)`` [FAIL] line is observable at module scope.
    """
    import contextlib
    import io

    from scitex_agent_container.cli_pkg._registry_sync import registry_sync_impl

    buf_out, buf_err = io.StringIO(), io.StringIO()
    start = time.monotonic()
    with contextlib.redirect_stdout(buf_out), contextlib.redirect_stderr(buf_err):
        rc = registry_sync_impl(
            from_peer=None,
            to_peer=None,
            all_peers=True,
            dry_run=False,
            as_json=False,
            overall_budget_s=_MAX_WALLCLOCK_S,
        )
    elapsed = time.monotonic() - start
    return _SyncOutcome(
        rc=rc, elapsed_s=elapsed, output=buf_err.getvalue() + buf_out.getvalue()
    )


def test_registry_sync_all_returns_instead_of_hanging_on_unreachable_peer(
    sync_all_outcome: _SyncOutcome,
) -> None:
    # Arrange — the fixture wrote the dead-peer config + pinned state.db.
    # Act — the fixture ran the real-ssh all-peers sync once, timing it.
    elapsed_s = sync_all_outcome.elapsed_s
    # Assert — it RETURNED well within the ceiling. A pre-fix un-timed sync
    # would block here forever.
    assert elapsed_s < _MAX_WALLCLOCK_S, (
        f"all-peers sync took {sync_all_outcome.elapsed_s:.1f}s against an "
        f"unreachable peer — it hung instead of failing fast "
        f"(ceiling {_MAX_WALLCLOCK_S:.0f}s)."
    )


def test_registry_sync_all_reports_unreachable_peer_as_failure(
    sync_all_outcome: _SyncOutcome,
) -> None:
    # Arrange — the fixture wrote the dead-peer config + pinned state.db.
    # Act — the fixture ran the real-ssh all-peers sync once.
    rc = sync_all_outcome.rc
    # Assert — non-zero rc: an unreachable peer is surfaced as a failure, not a
    # false success that silently swallows the lost sync.
    assert rc != 0, (
        "sync against an unreachable peer returned rc=0 (false success) — an "
        "unreachable peer must be surfaced as a failure, not silently swallowed."
    )


def test_registry_sync_all_names_unreachable_peer_in_loud_failure_line(
    sync_all_outcome: _SyncOutcome,
) -> None:
    # Arrange — the fixture wrote the dead-peer config + captured sync output.
    # Act — read the captured stderr/stdout from the single real-ssh sweep.
    output = sync_all_outcome.output
    # Assert — FAIL LOUD: the dead peer is named in a [FAIL] line so the
    # operator can act, rather than the failure being silent.
    has_named_fail = "deadpeer" in output and "FAIL" in output
    assert has_named_fail, (
        "the unreachable peer was not named in a loud [FAIL] line; failures "
        f"must be surfaced, not swallowed. Output was:\n{sync_all_outcome.output}"
    )


# ---------------------------------------------------------------------------
# Primary fix: the post-bind startup-sync task completes while a real bound
# socket (the uvicorn stand-in) stays bound — i.e. the sync can never block it.
# ---------------------------------------------------------------------------


@dataclass
class _PostBindOutcome:
    elapsed_s: float
    bound_port: int


@pytest.fixture
def post_bind_sync_outcome(dead_peer_env: Path) -> _PostBindOutcome:
    """Bind a REAL socket, then drive the actual post-bind sync coroutine once.

    Mirrors uvicorn owning the port BEFORE the best-effort sync runs. The
    socket stays bound for the duration so the assertions below can probe it.
    """
    from scitex_agent_container._listen._startup_peer_sync import (
        sync_peers_on_listen_startup,
    )

    sock = _bind_ephemeral_socket()
    bound_port = sock.getsockname()[1]
    try:

        async def _drive() -> None:
            # The real coroutine the lifespan schedules. It dispatches the
            # blocking ssh sweep via asyncio.to_thread and is bounded; the
            # outer wait_for here is the test's own hang-detector.
            await asyncio.wait_for(
                sync_peers_on_listen_startup(timeout_s=_MAX_WALLCLOCK_S),
                timeout=_MAX_WALLCLOCK_S,
            )

        start = time.monotonic()
        # Never raises on best-effort failure; must simply RETURN.
        asyncio.run(_drive())
        elapsed = time.monotonic() - start

        # Capture whether the bind is still held WHILE the socket is open — a
        # fresh bind to the same port must fail (port already in use).
        port_still_bound = False
        probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            probe.bind(("127.0.0.1", bound_port))
        except OSError:
            port_still_bound = True
        finally:
            probe.close()
    finally:
        sock.close()

    if not port_still_bound:
        pytest.fail(
            "the ephemeral bind was lost during the sync — the sync disturbed "
            "the bound port, which it must never do."
        )
    return _PostBindOutcome(elapsed_s=elapsed, bound_port=bound_port)


def test_post_bind_startup_sync_returns_within_budget_on_unreachable_peer(
    post_bind_sync_outcome: _PostBindOutcome,
) -> None:
    # Arrange — the fixture bound a real ephemeral socket (uvicorn stand-in).
    # Act — the fixture drove the real post-bind sync coroutine once, timing it.
    elapsed_s = post_bind_sync_outcome.elapsed_s
    # Assert — the post-bind sync returned within budget; on the old path this
    # same work ran BEFORE the bind and would have blocked it forever.
    assert elapsed_s < _MAX_WALLCLOCK_S, (
        f"post-bind startup sync took {post_bind_sync_outcome.elapsed_s:.1f}s "
        f"against an unreachable peer — it would have blocked uvicorn's bind on "
        f"the old path."
    )


def test_startup_sync_is_not_invoked_on_the_pre_bind_path(dead_peer_env: Path) -> None:
    # Arrange — guard the structural invariant the incident turned on: the
    # daemon-start path must reach the bind WITHOUT first running the blocking
    # peer-sync. We assert the pre-bind helper no longer calls the synchronous
    # sync, so a powered-off peer can never wedge boot before ``uvicorn.run``.
    import inspect

    from scitex_agent_container.cli_pkg import listen_cmds

    # Act
    src = inspect.getsource(listen_cmds._do_start_listen)

    # Assert — the synchronous pre-bind sync call is gone from the start path.
    assert "_maybe_sync_on_start()" not in src, (
        "_do_start_listen still calls the synchronous _maybe_sync_on_start() "
        "before uvicorn.run — that is the exact pre-bind hang vector "
        "(INCIDENT 2026-06-26). The peer-sync must run AFTER the bind, off the "
        "event loop, via the lifespan task."
    )
