"""``sac agents list`` must look where ``sac agents find`` looks.

WHY THIS FILE EXISTS. Measured 2026-08-23 on scitex-compute-04: from inside a
container the fleet listing reported ONE local agent while
``/home/ywatanabe/.scitex/agent-container/agents`` held 121 ``spec.yaml``. The
walk hardcoded ``Path.home() / ".scitex" / "agent-container" / "agents"``, and
inside a container ``Path.home()`` is the AGENT's home (``/home/agent``), which
has no ``agents/`` tree at all. So the only root it looked at did not exist, and
the loop that skips a non-directory root skipped the entire search.

The runtime already exports ``$SCITEX_AGENT_CONTAINER_YAML_DIRS`` pointing at
the operator's bind-mounted tree for exactly this reason, and
``config._resolve._search_dirs`` already honours it — which is why
``sac agents find`` and ``sac agents start`` could see agents that
``sac agents list`` swore did not exist. Two commands in one CLI, one truth,
one of them wrong.

WHY IT IS PINNED RATHER THAN COMMENTED. The failure produces a CONFIDENT EMPTY
ANSWER, which reads exactly like a finding: three agents on 2026-08-09 concluded
the fleet registry had been wiped and two escalated it as P1 data loss against a
healthy database; on 2026-08-23 a fourth read the same zero and reported "107 of
123 agents are down" to the operator; a fifth was one control away from telling
him three peers had abandoned their work. Nobody investigates a zero.

The sharpest symptom, and the one the fix is measured against: the listing
reported the agent MAKING THE QUERY as not-running. An instrument that cannot
see the hand holding it has no business being trusted about anything else.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from scitex_agent_container.cli_pkg._helpers import _agent_list_roots

_ENV_VAR = "SCITEX_AGENT_CONTAINER_YAML_DIRS"


@pytest.fixture()
def container_shaped_env(tmp_path):
    """A real container shape: specs live where the env var points, NOT in HOME.

    Sets the real ``os.environ`` and restores it on teardown rather than
    rewriting production internals, so the code under test resolves its roots
    exactly as it does in production.
    """
    operator_tree = tmp_path / "operator" / "agents"
    (operator_tree / "peer-agent").mkdir(parents=True)
    (operator_tree / "peer-agent" / "spec.yaml").write_text("apiVersion: v1\n")
    container_home = tmp_path / "container-home"
    container_home.mkdir()

    saved = {k: os.environ.get(k) for k in (_ENV_VAR, "HOME", "SAC_AGENT_SCOPE")}
    os.environ[_ENV_VAR] = str(operator_tree)
    os.environ["HOME"] = str(container_home)
    os.environ.pop("SAC_AGENT_SCOPE", None)
    try:
        yield operator_tree
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def test_the_operator_declared_agents_dir_is_searched(
    container_shaped_env,
) -> None:
    # Arrange: the container shape — $HOME has no agents tree, the specs are
    # only reachable through $SCITEX_AGENT_CONTAINER_YAML_DIRS.

    # Act
    roots = _agent_list_roots.user_scope_roots()

    # Assert: before the fix the sole root was $HOME/.scitex/.../agents, which
    # does not exist here, and the walk found nothing.
    assert container_shaped_env in roots


def test_resolver_failure_falls_back_to_the_historical_root(tmp_path) -> None:
    # Arrange: a resolver that fails, injected as the collaborator. Degrading
    # to the OLD root must beat degrading to no search at all — a resolver
    # hiccup must never silently report zero agents, which is the exact
    # failure mode this module exists to prevent.
    def exploding_resolver() -> tuple[Path, list[Path], list[Path]]:
        raise RuntimeError("resolver unavailable")

    # Act
    roots = _agent_list_roots.user_scope_roots(search_dirs=exploding_resolver)

    # Assert
    assert roots == [Path.home() / ".scitex" / "agent-container" / "agents"]
