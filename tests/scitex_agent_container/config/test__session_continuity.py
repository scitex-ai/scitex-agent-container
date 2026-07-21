"""Tests for ``config._session_continuity`` and the loader role-default.

"Fresh by default, opt-in continue" (2026-06-22): ``claude.session``
defaults to ``fresh`` so experiment trials start hermetic, but long-lived
coordinator roles (lead / head / worker / telegrammer /
project-maintainer / …) whose specs OMIT ``session`` are mapped back to
``continue`` by ``role_wants_continuity`` — applied in ``_loaders.py`` where
the role label / env is visible. ``wants_continue`` translates a resolved
mode to the bare ``-c`` decision (continue → True; fresh/resume → False).

Each test pins exactly one behaviour with explicit AAA markers.
"""

from __future__ import annotations

import pytest

from scitex_agent_container.config import load_config
from scitex_agent_container.config._session_continuity import (
    default_session_for_role,
    role_wants_continuity,
    wants_continue,
)

# ---------------------------------------------------------------------------
# role_wants_continuity — coordinator roles → True, others → False
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "role",
    [
        "lead",
        "head",
        "worker",
        "telegrammer",
        "worker-telegrammer",
        "coordinator",
        "project-maintainer",
        "quality-agent",
        "dev-agent",
        "contributor",
    ],
)
def test_exact_coordinator_role_wants_continuity(role):
    # Arrange
    candidate = role
    # Act
    result = role_wants_continuity(candidate)
    # Assert
    assert result is True


@pytest.mark.parametrize(
    "role",
    [
        "worker-telegrammer-orochi",
        "lead-ywata-note-win",
        "head-ywata-note-win",
        "contributor-figrecipe",
    ],
)
def test_project_suffixed_coordinator_role_wants_continuity(role):
    # Arrange
    candidate = role
    # Act
    result = role_wants_continuity(candidate)
    # Assert
    assert result is True


@pytest.mark.parametrize(
    "role", ["capsule-5286757", "benchmark", "tmp-fleet", "scratch"]
)
def test_non_coordinator_role_does_not_want_continuity(role):
    # Arrange
    candidate = role
    # Act
    result = role_wants_continuity(candidate)
    # Assert
    assert result is False


@pytest.mark.parametrize("role", [None, "", "   "])
def test_absent_role_does_not_want_continuity(role):
    # Arrange
    candidate = role
    # Act
    result = role_wants_continuity(candidate)
    # Assert
    assert result is False


def test_role_match_ignores_letter_case():
    # Arrange
    candidate = "Project-Maintainer"
    # Act
    result = role_wants_continuity(candidate)
    # Assert
    assert result is True


# ---------------------------------------------------------------------------
# default_session_for_role — the role-based half of the precedence chain
# ---------------------------------------------------------------------------


def test_default_session_for_coordinator_role_is_continue():
    # Arrange
    candidate = "lead"
    # Act
    result = default_session_for_role(candidate)
    # Assert
    assert result == "continue"


def test_default_session_for_absent_role_is_fresh():
    # Arrange
    candidate = None
    # Act
    result = default_session_for_role(candidate)
    # Assert
    assert result == "fresh"


# ---------------------------------------------------------------------------
# wants_continue — resolved mode → bare ``-c`` decision
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("mode", "expected"),
    [
        ("continue", True),
        ("fresh", False),
        ("resume", False),
        ("new-session", False),
        ("", False),
        (None, False),
    ],
)
def test_wants_continue_only_true_for_continue_mode(mode, expected):
    # Arrange
    candidate = mode
    # Act
    result = wants_continue(candidate)
    # Assert
    assert result is expected


# ---------------------------------------------------------------------------
# Loader role-default end-to-end (scenario (e))
# ---------------------------------------------------------------------------


def _write_spec(tmp_path, body: str):
    # Red-start ruling 2026-07-21: merge the validator's paste defaults
    # beneath the fixture body (body wins) so every field is explicit.
    # The merge NEVER adds claude.session unless already authored —
    # paste value is null == unauthored — so the role-default scenarios
    # under test keep their meaning.
    from tests.scitex_agent_container._helpers.explicit_spec import (
        explicitize_yaml,
    )

    p = tmp_path / "spec.yaml"
    p.write_text(explicitize_yaml(body), encoding="utf-8")
    return p


_OMIT_SESSION_WITH_ROLE = """\
apiVersion: scitex-agent-container/v3
kind: Agent
metadata:
  labels:
    role: project-maintainer
spec:
  runtime: tui
  host: ${HOSTNAME}
  workdir: /home/agent/work
  apptainer:
    image: /x.sif
    binds: []
  health:
    enabled: true
    interval: 60
  restart:
    policy: on-failure
    max_retries: 3
  claude:
    model: sonnet
"""

_OMIT_SESSION_NO_ROLE = """\
apiVersion: scitex-agent-container/v3
kind: Agent
metadata:
  labels:
    project: cohort
spec:
  runtime: tui
  host: ${HOSTNAME}
  workdir: /home/agent/work
  apptainer:
    image: /x.sif
    binds: []
  health:
    enabled: true
    interval: 60
  restart:
    policy: on-failure
    max_retries: 3
  claude:
    model: haiku
"""

_EXPLICIT_FRESH_WITH_COORDINATOR_ROLE = """\
apiVersion: scitex-agent-container/v3
kind: Agent
metadata:
  labels:
    role: lead
spec:
  runtime: tui
  host: ${HOSTNAME}
  workdir: /home/agent/work
  apptainer:
    image: /x.sif
    binds: []
  health:
    enabled: true
    interval: 60
  restart:
    policy: on-failure
    max_retries: 3
  claude:
    session: fresh
    model: sonnet
"""

_FLEET_ROLE_VIA_ENV = """\
apiVersion: scitex-agent-container/v3
kind: Agent
metadata:
  labels:
    project: x
spec:
  runtime: tui
  host: ${HOSTNAME}
  workdir: /home/agent/work
  apptainer:
    image: /x.sif
    binds: []
    env:
      SCITEX_AGENT_CONTAINER_ROLE: worker
  claude:
    model: sonnet
  health:
    enabled: true
    interval: 60
  restart:
    policy: on-failure
    max_retries: 3
"""


def test_loader_coordinator_role_defaults_omitted_session_to_continue(tmp_path):
    # Arrange
    spec = _write_spec(tmp_path, _OMIT_SESSION_WITH_ROLE)
    # Act
    cfg = load_config(str(spec))
    # Assert
    assert cfg.claude.session == "continue"


def test_loader_absent_role_keeps_omitted_session_fresh(tmp_path):
    # Arrange
    spec = _write_spec(tmp_path, _OMIT_SESSION_NO_ROLE)
    # Act
    cfg = load_config(str(spec))
    # Assert
    assert cfg.claude.session == "fresh"


def test_loader_explicit_fresh_on_coordinator_is_not_overridden(tmp_path):
    # Arrange
    spec = _write_spec(tmp_path, _EXPLICIT_FRESH_WITH_COORDINATOR_ROLE)
    # Act
    cfg = load_config(str(spec))
    # Assert
    assert cfg.claude.session == "fresh"


def test_loader_fleet_role_via_env_defaults_to_continue(tmp_path):
    # Arrange
    spec = _write_spec(tmp_path, _FLEET_ROLE_VIA_ENV)
    # Act
    cfg = load_config(str(spec))
    # Assert
    assert cfg.claude.session == "continue"
