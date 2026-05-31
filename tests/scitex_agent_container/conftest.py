"""Package-wide test conftest — neutralise production-env pollution.

The test process itself runs inside the proj-scitex-agent-container
apptainer SIF (the dev / agent runtime ships pre-built). Production-
intended env vars (``APPTAINER_CONTAINER`` for in-SIF detection,
``SCITEX_AGENT_CONTAINER_AGENT`` for the agent's own identity) are
therefore set in the test process's env. Tests that read those vars
expecting "bare host, no agent identity" silently pick up the running
agent's values and fail with hard-to-debug "wrong file path" /
"unexpected branch" errors.

Two autouse fixtures here. Both yield with the polluting vars
cleared and restore on teardown — never delete pre-existing values
permanently. Tests that *want* the polluting var set drop it back
themselves (e.g. ``_lifecycle/test__in_sif_broker.py::sif_env``).

(1) ``_clear_in_sif_env`` — clears ``APPTAINER_CONTAINER`` /
``SINGULARITY_CONTAINER``. Required by the SAC-from-SAC broker
(operator-mandated 2026-06-01): every ``agent_start`` call now
detects in-SIF and routes through the host listen instead of the
local runtime. Without clearing here, the lifecycle tests'
hand-rolled runtime fakes are never reached.

(2) ``_clear_agent_identity`` — clears ``SCITEX_AGENT_CONTAINER_AGENT``
/ ``SAC_AGENT``. The statusline ``_agent_name()`` reads these to
discover which agent the running session belongs to. With the running
agent's name leaking through, tests that set ``CLAUDE_AGENT_ID`` to
control the persist filename end up writing to the running agent's
file instead — assertions on the test-controlled filename then fail
with ``FileNotFoundError``.

Both fixtures cover the WHOLE package's test tree (this conftest is
at ``tests/scitex_agent_container/``). Putting them here is simpler
than re-implementing under every nested test dir.

No production code is mutated by these fixtures; they only touch the
test process env.
"""

from __future__ import annotations

import os
from typing import Iterator

import pytest

_IN_SIF_KEYS = ("APPTAINER_CONTAINER", "SINGULARITY_CONTAINER")
_AGENT_IDENTITY_KEYS = (
    "SCITEX_AGENT_CONTAINER_AGENT",
    "SAC_AGENT",
)


def _save_restore_yield(keys: tuple[str, ...]) -> Iterator[None]:
    """Real save / clear / yield / restore for a set of env keys."""
    saved = {k: os.environ.get(k) for k in keys}
    for k in keys:
        os.environ.pop(k, None)
    try:
        yield
    finally:
        for k, prev in saved.items():
            if prev is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = prev


@pytest.fixture(autouse=True)
def _clear_in_sif_env() -> Iterator[None]:
    """Yield with in-SIF env vars cleared; restore on teardown."""
    yield from _save_restore_yield(_IN_SIF_KEYS)


@pytest.fixture(autouse=True)
def _clear_agent_identity() -> Iterator[None]:
    """Yield with ``SAC_AGENT`` / long-form cleared; restore on teardown.

    Statusline + a couple of identity-keyed CLIs read this; the running
    agent's value would otherwise win over test-controlled overrides.
    """
    yield from _save_restore_yield(_AGENT_IDENTITY_KEYS)
