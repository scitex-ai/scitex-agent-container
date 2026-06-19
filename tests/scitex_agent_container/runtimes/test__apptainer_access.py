"""``spec.access`` host-access posture — bind + ``--pwd`` resolution.

Covers ``runtimes._apptainer_access`` (operator directive 2026-06-19,
``feedback_sac_dev_agent_bind_policy``): dev agents see the whole host by
default (``access: full`` → whole-home bind + canonical ``--pwd``);
``access: capsule`` keeps only the explicit spec binds + the ``/work``
alias (legacy leak-prevention behaviour). Back-compat: an absent
``access`` field defaults to ``full``.

Real ``AgentConfig`` via ``load_config`` on a tmp spec — no mocks.
STX-TQ002 AAA markers + STX-TQ007 one-assert + PA-306 no-mock-fixtures.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Iterator

import pytest

from scitex_agent_container.config import load_config
from scitex_agent_container.runtimes._apptainer_access import (
    full_home_bind_flags,
    is_full_access,
    resolve_pwd,
    workdir_bind_targets,
)


@pytest.fixture
def _home(tmp_path: Path) -> Iterator[Path]:
    """Redirect ``HOME`` so the whole-home bind resolves to a tmp dir.

    ``full_home_bind_flags`` binds ``Path.home()``; sliding ``HOME`` keeps
    the asserted bind target deterministic + independent of the developer's
    real home.
    """
    saved = os.environ.get("HOME")
    os.environ["HOME"] = str(tmp_path)
    try:
        yield tmp_path
    finally:
        if saved is None:
            os.environ.pop("HOME", None)
        else:
            os.environ["HOME"] = saved


_SPEC = """\
apiVersion: scitex-agent-container/v3
kind: Agent
metadata:
  labels:
    project: t
    sac-builtin: "off"
spec:
  runtime: tui
  workdir: {workdir}
  claude:
    model: sonnet
{extra}
"""


def _cfg(tmp_path: Path, *, workdir: str, extra: str = ""):
    spec_dir = tmp_path / "agents" / "agt"
    spec_dir.mkdir(parents=True, exist_ok=True)
    spec = spec_dir / "spec.yaml"
    spec.write_text(_SPEC.format(workdir=workdir, extra=extra), encoding="utf-8")
    return load_config(str(spec))


# ---------------------------------------------------------------------------
# is_full_access — default + explicit
# ---------------------------------------------------------------------------


def test_is_full_access_true_when_access_absent(tmp_path: Path) -> None:
    # Arrange — no spec.access → loader defaults to "full".
    cfg = _cfg(tmp_path, workdir="/home/u/proj/x")
    # Act
    out = is_full_access(cfg)
    # Assert
    assert out is True


def test_is_full_access_false_for_capsule(tmp_path: Path) -> None:
    # Arrange
    cfg = _cfg(tmp_path, workdir="/home/u/proj/x", extra="  access: capsule")
    # Act
    out = is_full_access(cfg)
    # Assert
    assert out is False


def test_is_full_access_true_for_explicit_full(tmp_path: Path) -> None:
    # Arrange
    cfg = _cfg(tmp_path, workdir="/home/u/proj/x", extra="  access: full")
    # Act
    out = is_full_access(cfg)
    # Assert
    assert out is True


# ---------------------------------------------------------------------------
# full_home_bind_flags — whole-home bind for full, nothing for capsule
# ---------------------------------------------------------------------------


def test_full_home_bind_binds_whole_operator_home(tmp_path: Path, _home: Path) -> None:
    # Arrange — full (default) agent.
    cfg = _cfg(tmp_path, workdir=str(_home / "proj" / "x"))
    # Act
    flags = full_home_bind_flags(cfg)
    # Assert — the operator's whole home rw at its canonical path.
    assert flags == ["--bind", f"{_home}:{_home}:rw"]


def test_full_home_bind_empty_for_capsule(tmp_path: Path, _home: Path) -> None:
    # Arrange
    cfg = _cfg(tmp_path, workdir=str(_home / "proj" / "x"), extra="  access: capsule")
    # Act
    flags = full_home_bind_flags(cfg)
    # Assert — capsule agents get ONLY their explicit binds (none here).
    assert flags == []


# ---------------------------------------------------------------------------
# workdir_bind_targets — capsule = [/work]; full = [canonical, /work]
# ---------------------------------------------------------------------------


def test_workdir_targets_capsule_is_work_alias_only(tmp_path: Path) -> None:
    # Arrange
    cfg = _cfg(tmp_path, workdir="/home/u/proj/x", extra="  access: capsule")
    # Act
    targets = workdir_bind_targets(cfg)
    # Assert — single legacy mount, byte-identical to pre-2026-06-19.
    assert targets == ["/work"]


def test_workdir_targets_full_canonical_first_then_work_alias(tmp_path: Path) -> None:
    # Arrange — full agent whose workdir is under the operator home.
    cfg = _cfg(tmp_path, workdir="/home/u/proj/x")
    # Act
    targets = workdir_bind_targets(cfg)
    # Assert — canonical path first (so --pwd lands there), /work for back-compat.
    assert targets == ["/home/u/proj/x", "/work"]


def test_workdir_targets_full_out_of_home_workdir_still_keeps_work(
    tmp_path: Path,
) -> None:
    # Arrange — full agent whose workdir is OUTSIDE the home (e.g. /tmp).
    cfg = _cfg(tmp_path, workdir="/tmp/agt-work")
    # Act
    targets = workdir_bind_targets(cfg)
    # Assert — canonical /tmp path is bound so --pwd resolves, plus /work.
    assert targets == ["/tmp/agt-work", "/work"]


def test_workdir_targets_full_collapses_when_alias_equals_canonical(
    tmp_path: Path,
) -> None:
    # Arrange — operator set container_workdir to the canonical path itself;
    # the duplicate must collapse to a single bind target.
    cfg = _cfg(
        tmp_path,
        workdir="/home/u/proj/x",
        extra="  apptainer:\n    container_workdir: /home/u/proj/x",
    )
    # Act
    targets = workdir_bind_targets(cfg)
    # Assert
    assert targets == ["/home/u/proj/x"]


# ---------------------------------------------------------------------------
# resolve_pwd — canonical for full, /work for capsule
# ---------------------------------------------------------------------------


def test_resolve_pwd_full_is_canonical_workdir(tmp_path: Path) -> None:
    # Arrange
    cfg = _cfg(tmp_path, workdir="/home/u/proj/x")
    # Act
    pwd = resolve_pwd(cfg)
    # Assert — the agent opens in its project at the operator's canonical path.
    assert pwd == "/home/u/proj/x"


def test_resolve_pwd_capsule_is_work_alias(tmp_path: Path) -> None:
    # Arrange
    cfg = _cfg(tmp_path, workdir="/home/u/proj/x", extra="  access: capsule")
    # Act
    pwd = resolve_pwd(cfg)
    # Assert
    assert pwd == "/work"
