"""The three pollers must not boot in a process that only wants the app.

``sac listen``'s lifespan launches six background loops. Three of them —
the GitHub-CI verdict poller and the two heartbeat writers — need nothing
from the host to start, so they start EVERYWHERE, including in every
foreign process that boots this app to test something else. They then log
through scitex-logging, whose handler re-resolves ``sys.stderr`` at every
emit, so a line lands in whatever stream is installed at that instant —
up to and including a ``CliRunner.invoke`` buffer, where it corrupts a
``--json`` assertion. That is the trunk-red this suite already paid for
(develop 312975ec, run 31867365078).

``tests/conftest.py`` therefore floors ``SAC_LISTEN_POLLER_LOOPS_DISABLED``
for the whole suite — which means NOTHING ELSE in the tree exercises the
launch site any more. This file is the exception that keeps that guard
honest, and it pins BOTH directions: absence alone would pass just as
happily if the loops had stopped launching for some entirely different
reason.

Why not the three published ``SAC_*_DISABLED`` switches? Because those are
read by the COROUTINES too, so a suite-wide floor built on them reaches
every test that calls a loop function directly. It did: it reddened both
matrix legs on ``test__{sdk,tui}_heartbeat_loop_unknown_is_not_dead.py``,
two files a per-file opt-in list had missed. The group switch is read at
the launch site and nowhere else, so it cannot reach them.

No mocks (STX-NM002): the REAL lifespan, driven to startup and shut down.
AAA markers (TQ002); 3+-word test names.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from scitex_agent_container._lifecycle._listen_lifespan import build_listen_lifespan

_GROUP_SWITCH = "SAC_LISTEN_POLLER_LOOPS_DISABLED"

# The task handles the lifespan stashes on ``app.state`` for the three
# loops under test.
_POLLER_TASKS = (
    "github_ci_poller_task",
    "tui_heartbeat_task",
    "sdk_heartbeat_task",
)

# Everything else the lifespan launches. Each already honours its own
# launch-site switch, so turning them all off keeps this test about the
# pollers and nothing else — and keeps it from touching ssh, tmux, the
# registry sweep or a bind watchdog.
_OTHER_LOOPS = (
    "SAC_LISTEN_STARTUP_SYNC_DISABLED",
    "SAC_PERIODIC_DRIVE_DISABLED",
    "SAC_LIVENESS_TICK_DISABLED",
    "SAC_DEPLOY_FRESHNESS_DISABLED",
    "SAC_LISTEN_BIND_WATCHDOG_DISABLED",
)

# The published per-loop switches. Cleared in both cases so that what this
# test measures is the GROUP switch, never one of them leaking in from the
# suite floor.
_PER_LOOP_SWITCHES = (
    "SAC_GITHUB_CI_POLLER_DISABLED",
    "SAC_TUI_HEARTBEAT_DISABLED",
    "SAC_SDK_HEARTBEAT_DISABLED",
)


def _arrange_env(env, group_switch: str) -> None:
    for key in _OTHER_LOOPS:
        env.set(key, "1")
    for key in _PER_LOOP_SWITCHES:
        env.delete(key)
    if group_switch:
        env.set(_GROUP_SWITCH, group_switch)
    else:
        env.delete(_GROUP_SWITCH)


async def _pollers_launched_by_a_real_lifespan() -> set[str]:
    """Boot the REAL lifespan and report which poller tasks it created."""
    app = SimpleNamespace(state=SimpleNamespace())
    lifespan = build_listen_lifespan()
    async with lifespan(app):
        return {
            name
            for name in _POLLER_TASKS
            if getattr(app.state, name, None) is not None
        }


@pytest.mark.asyncio
async def test_group_switch_launches_no_poller_loops(env_save_restore):
    # Arrange — the suite's own floor, stated explicitly.
    _arrange_env(env_save_restore, "1")
    # Act
    launched = await _pollers_launched_by_a_real_lifespan()
    # Assert
    assert launched == set()


@pytest.mark.asyncio
async def test_without_the_group_switch_every_poller_launches(env_save_restore):
    # Arrange — the production default: nothing set, so nothing is skipped.
    # This is the half that makes the test above non-vacuous. Without it,
    # a lifespan that had quietly stopped launching these loops for an
    # unrelated reason would still look "fixed".
    _arrange_env(env_save_restore, "")
    # Act
    launched = await _pollers_launched_by_a_real_lifespan()
    # Assert
    assert launched == set(_POLLER_TASKS)
