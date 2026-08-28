"""A send to a name that was never an agent must say so, not blame the port.

Delivery declines identically for "defined agent, no reachable port" and for
"this name was never an agent", so both reach the same refusal. Until
2026-08-20 both also got the SAME text — "no A2A port is recorded", prescribing
`sac agents start <name>` — which presupposes the agent exists.

MEASURED: ci-watch dispatched to five names that had never been registered
(definitions=0, instances=0 EVER), 351 times, 0 successes. A peer read this
refusal, took the named cause at face value, and reported the failures as
instances of an unrelated port-registration defect. The remedy was trusted
because it was specific, and it pointed at the wrong thing.

The controls here carry the weight: it is easy to write a "not defined" branch
that fires for everything, and the resulting message would still look correct
in the one case you tested.

Separate file because ``test_send_cmds.py`` is 566 lines against a 512 cap.
PA-306: no mocks — real spec files under ``tmp_path`` and a real isolated
SQLite state db.
"""

from __future__ import annotations

import os
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

import pytest
from click.testing import CliRunner

from scitex_agent_container.cli_pkg.send_cmds import send
from tests.scitex_agent_container._helpers.explicit_spec import explicitize_yaml

_YAML_DIRS = "SCITEX_AGENT_CONTAINER_YAML_DIRS"
_STATE_DB = "SCITEX_AGENT_CONTAINER_STATE_DB"


def _write_spec(yaml_root: Path, name: str) -> None:
    agent_dir = yaml_root / name
    agent_dir.mkdir(parents=True)
    (agent_dir / "spec.yaml").write_text(
        explicitize_yaml(f"""apiVersion: scitex-agent-container/v3
kind: Agent
spec:
  runtime: apptainer
  host: ${{HOSTNAME}}
  workdir: {yaml_root.parent / "workdir"}
  apptainer:
    image: /x.sif
    binds: []
  claude:
    model: sonnet
""")
    )


@contextmanager
def _env(key: str, value: str) -> Iterator[None]:
    saved = os.environ.get(key)
    os.environ[key] = value
    try:
        yield
    finally:
        if saved is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = saved


@pytest.fixture
def only_alpha_defined(tmp_path: Path) -> Iterator[Path]:
    """Exactly one agent (``alpha``) exists; the state db is empty.

    So ``alpha`` is defined-but-unreachable and any other name is unknown —
    the two states this refusal must tell apart.
    """
    import importlib

    import scitex_agent_container._state.state_db as _state_db_mod

    yaml_root = tmp_path / "agents"
    yaml_root.mkdir()
    _write_spec(yaml_root, "alpha")
    (tmp_path / "workdir").mkdir()
    with _env(_YAML_DIRS, str(yaml_root)), _env(
        _STATE_DB, str(tmp_path / "isolated-state.db")
    ):
        importlib.reload(_state_db_mod)
        try:
            yield tmp_path
        finally:
            importlib.reload(_state_db_mod)


def _refuse(name: str) -> str:
    return CliRunner().invoke(send, [name, "hi"]).output


# ---------------------------------------------------------------------------
# The defect: an unknown name was told its port was missing
# ---------------------------------------------------------------------------


def test_an_unknown_name_is_reported_as_not_defined(pg_schema: str, only_alpha_defined: Path) -> None:
    # Arrange
    unknown = "proj-scitex-stats"
    # Act
    out = _refuse(unknown)
    # Assert
    assert "NOT DEFINED" in out, out


def test_an_unknown_name_is_not_told_to_start_the_agent(
    only_alpha_defined: Path,
) -> None:
    # Arrange
    unknown = "proj-scitex-stats"
    # Act
    out = _refuse(unknown)
    # Assert — the remedy that sent a reader to the wrong cause
    assert "sac agents start proj-scitex-stats" not in out, out


def test_an_unknown_name_does_not_claim_a_missing_port(
    only_alpha_defined: Path,
) -> None:
    # Arrange
    unknown = "proj-scitex-stats"
    # Act
    out = _refuse(unknown)
    # Assert — naming an unestablished cause is the whole defect
    assert "no A2A port is recorded" not in out, out


# ---------------------------------------------------------------------------
# Controls — the branch must DISCRIMINATE, not fire for everything
# ---------------------------------------------------------------------------


def test_a_defined_agent_still_gets_the_missing_port_message(
    pg_schema: str, only_alpha_defined: Path,
) -> None:
    # Arrange — alpha has a spec and no reachable port
    defined = "alpha"
    # Act
    out = _refuse(defined)
    # Assert
    assert "no A2A port is recorded" in out, out


def test_a_defined_agent_is_never_called_not_defined(
    only_alpha_defined: Path,
) -> None:
    # Arrange
    defined = "alpha"
    # Act
    out = _refuse(defined)
    # Assert — the false negative that would break every real send
    assert "NOT DEFINED" not in out, out


def test_both_refusals_keep_the_containment_guarantee(
    pg_schema: str, only_alpha_defined: Path,
) -> None:
    # Arrange
    both = ("alpha", "proj-scitex-stats")
    # Act
    outs = [_refuse(name) for name in both]
    # Assert — sac never runs the turn on the bare host, either way
    assert all("apptainer" in out for out in outs), outs
