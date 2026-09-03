"""The scratch-root RESOLUTION TABLE — where ``/uvwork`` lives on this host.

ADR-0024. Four rows, and the fourth is the whole point:

    config path      ``scratch_root: /abs`` and it exists   -> source=config
    config none      ``scratch_root: none`` + a reason      -> source=none
    default present  nothing declared, ``/scratch`` there   -> source=default
    default absent   nothing declared, no ``/scratch``      -> REFUSE

A resolver that answered the fourth row with "well, the overlay then" would
put every agent's uv cache and venv back on the host ROOT volume — the volume
that filled to 0 four times on scitex-compute-04 on 2026-09-02 — and would
say nothing while doing it. So the refusal is a tested property, not an
incidental exception, and its message is tested for naming the missing path,
the config key and both fixes.

The DEFAULT rows cannot be exercised against the real ``/scratch``: it exists
on every compute host and inside every agent container, so "default absent"
would be unreachable there and "default present" would pass for the wrong
reason on a laptop. Hence the ``probe`` seam — the resolver's documented
test seam for the default candidate, and the ONE dish: no env var selects a
root, only ``config.yaml``. The production value is pinned separately below
so the seam cannot drift away from what ships.

No mocks (PA-306): real YAML at ``tmp_path`` through the real loader, real
directories on disk. STX-TQ002 AAA markers; one fact per test (PA-307).
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from scitex_agent_container._state.host_config import load
from scitex_agent_container._state.host_scratch import (
    CONFIG_KEY,
    DEFAULT_SCRATCH_ROOT,
    SCRATCH_SOURCES,
    ScratchRoot,
    ScratchRootError,
    resolve_scratch_root,
)


@pytest.fixture
def cfg_path(tmp_path: Path, env_save_restore) -> Path:
    """Real config.yaml under tmp_path, surfaced via the env override."""
    p = tmp_path / "config.yaml"
    env_save_restore.set("SCITEX_AGENT_CONTAINER_CONFIG", str(p))
    return p


@pytest.fixture
def absent_probe(tmp_path: Path) -> Path:
    """A path that is NOT a directory — the 'no /scratch on this host' row."""
    return tmp_path / "no-such-scratch"


@pytest.fixture
def present_probe(tmp_path: Path) -> Path:
    """A real directory standing in for a host's ``/scratch``."""
    p = tmp_path / "scratch"
    p.mkdir()
    return p


# ---------------------------------------------------------------------------
# Row 1 — config.yaml declares a path
# ---------------------------------------------------------------------------


def test_a_declared_existing_root_is_the_resolved_root(
    cfg_path: Path, present_probe: Path, absent_probe: Path
) -> None:
    # Arrange — declared, and the default probe deliberately absent so a
    # pass cannot come from the default leg.
    cfg_path.write_text(f"{CONFIG_KEY}: {present_probe}\n")
    # Act
    resolved = resolve_scratch_root(load(), probe=absent_probe)
    # Assert
    assert resolved.root == present_probe


def test_a_declared_root_reports_the_config_source(
    cfg_path: Path, present_probe: Path, absent_probe: Path
) -> None:
    # Arrange
    cfg_path.write_text(f"{CONFIG_KEY}: {present_probe}\n")
    # Act
    resolved = resolve_scratch_root(load(), probe=absent_probe)
    # Assert
    assert resolved.source == "config"


def test_a_declared_root_beats_a_present_default(
    cfg_path: Path, present_probe: Path, tmp_path: Path
) -> None:
    # Arrange — both legs available; the declaration must win.
    declared = tmp_path / "declared"
    declared.mkdir()
    cfg_path.write_text(f"{CONFIG_KEY}: {declared}\n")
    # Act
    resolved = resolve_scratch_root(load(), probe=present_probe)
    # Assert
    assert resolved.root == declared


def test_a_declared_root_that_is_not_a_directory_refuses(
    cfg_path: Path, absent_probe: Path, present_probe: Path
) -> None:
    # Arrange — a declaration that cannot be honoured must FAIL, never
    # quietly fall through to the default.
    cfg_path.write_text(f"{CONFIG_KEY}: {absent_probe}\n")
    # Act
    raised = pytest.raises(ScratchRootError, match=re.escape(str(absent_probe)))
    # Assert
    with raised:
        resolve_scratch_root(load(), probe=present_probe)


def test_the_unhonourable_declaration_refusal_names_the_config_file(
    cfg_path: Path, absent_probe: Path, present_probe: Path
) -> None:
    # Arrange
    cfg_path.write_text(f"{CONFIG_KEY}: {absent_probe}\n")
    # Act
    raised = pytest.raises(ScratchRootError, match=re.escape(str(cfg_path)))
    # Assert
    with raised:
        resolve_scratch_root(load(), probe=present_probe)


# ---------------------------------------------------------------------------
# Row 2 — the written decision to keep /uvwork in the overlay
# ---------------------------------------------------------------------------


def test_the_none_decision_resolves_to_no_root(
    cfg_path: Path, present_probe: Path
) -> None:
    # Arrange — present_probe would otherwise supply a default.
    cfg_path.write_text(f"{CONFIG_KEY}: none\n{CONFIG_KEY}_reason: root LV is 8T\n")
    # Act
    resolved = resolve_scratch_root(load(), probe=present_probe)
    # Assert
    assert resolved.root is None


def test_the_none_decision_reports_the_none_source(
    cfg_path: Path, present_probe: Path
) -> None:
    # Arrange
    cfg_path.write_text(f"{CONFIG_KEY}: none\n{CONFIG_KEY}_reason: root LV is 8T\n")
    # Act
    resolved = resolve_scratch_root(load(), probe=present_probe)
    # Assert
    assert resolved.source == "none"


def test_the_none_decision_carries_the_written_reason(
    cfg_path: Path, present_probe: Path
) -> None:
    # Arrange — the reason is what a later reader has instead of a guess.
    cfg_path.write_text(f"{CONFIG_KEY}: none\n{CONFIG_KEY}_reason: root LV is 8T\n")
    # Act
    resolved = resolve_scratch_root(load(), probe=present_probe)
    # Assert
    assert resolved.reason == "root LV is 8T"


# ---------------------------------------------------------------------------
# Row 3 — no declaration, the default probe is there
# ---------------------------------------------------------------------------


def test_an_undeclared_host_with_a_present_probe_uses_it(
    cfg_path: Path, present_probe: Path
) -> None:
    # Arrange — compute-01 and compute-03 have no config.yaml at all.
    cfg_path.write_text("")
    # Act
    resolved = resolve_scratch_root(load(), probe=present_probe)
    # Assert
    assert resolved.root == present_probe


def test_an_undeclared_host_with_a_present_probe_reports_the_default_source(
    cfg_path: Path, present_probe: Path
) -> None:
    # Arrange
    cfg_path.write_text("")
    # Act
    resolved = resolve_scratch_root(load(), probe=present_probe)
    # Assert
    assert resolved.source == "default"


def test_the_default_reason_names_the_probed_path(
    cfg_path: Path, present_probe: Path
) -> None:
    # Arrange
    cfg_path.write_text("")
    # Act
    resolved = resolve_scratch_root(load(), probe=present_probe)
    # Assert
    assert str(present_probe) in resolved.reason


# ---------------------------------------------------------------------------
# Row 4 — no declaration and no probe: REFUSE
# ---------------------------------------------------------------------------


def test_an_undeclared_host_without_a_probe_refuses(
    cfg_path: Path, absent_probe: Path
) -> None:
    # Arrange — the row the whole module exists for.
    cfg_path.write_text("")
    # Act
    raised = pytest.raises(ScratchRootError)
    # Assert
    with raised:
        resolve_scratch_root(load(), probe=absent_probe)


def test_the_refusal_names_the_missing_path(
    cfg_path: Path, absent_probe: Path
) -> None:
    # Arrange
    cfg_path.write_text("")
    # Act
    raised = pytest.raises(ScratchRootError, match=re.escape(str(absent_probe)))
    # Assert
    with raised:
        resolve_scratch_root(load(), probe=absent_probe)


def test_the_refusal_names_the_config_key(cfg_path: Path, absent_probe: Path) -> None:
    # Arrange — fix #2 is a one-line edit; the message must spell the key.
    cfg_path.write_text("")
    # Act
    raised = pytest.raises(ScratchRootError, match=CONFIG_KEY)
    # Assert
    with raised:
        resolve_scratch_root(load(), probe=absent_probe)


def test_the_refusal_names_the_config_file_to_edit(
    cfg_path: Path, absent_probe: Path
) -> None:
    # Arrange
    cfg_path.write_text("")
    # Act
    raised = pytest.raises(ScratchRootError, match=re.escape(str(cfg_path)))
    # Assert
    with raised:
        resolve_scratch_root(load(), probe=absent_probe)


def test_the_refusal_offers_the_written_none_decision_as_a_fix(
    cfg_path: Path, absent_probe: Path
) -> None:
    # Arrange — fix #3: the escape hatch must be discoverable from the error.
    cfg_path.write_text("")
    # Act
    raised = pytest.raises(ScratchRootError, match=f"{CONFIG_KEY}_reason")
    # Assert
    with raised:
        resolve_scratch_root(load(), probe=absent_probe)


# ---------------------------------------------------------------------------
# The answer SHAPE, and the production default the seam stands in for
# ---------------------------------------------------------------------------


def test_the_production_default_probe_is_slash_scratch() -> None:
    # Arrange — the seam above must not drift from what actually ships.
    expected = Path("/scratch")
    # Act
    default = DEFAULT_SCRATCH_ROOT
    # Assert
    assert default == expected


def test_the_sources_are_a_closed_set_of_three() -> None:
    # Arrange
    expected = ("config", "default", "none")
    # Act
    sources = SCRATCH_SOURCES
    # Assert
    assert sources == expected


def test_an_unknown_source_is_refused_by_the_answer_shape() -> None:
    # Arrange
    root = Path("/scratch")
    # Act
    raised = pytest.raises(ValueError, match="source must be one of")
    # Assert
    with raised:
        ScratchRoot(root=root, source="guessed", reason="")


def test_a_rootless_answer_must_say_none(tmp_path: Path) -> None:
    # Arrange — "no root" and "source=none" are one fact, not two.
    # Act
    raised = pytest.raises(ValueError, match="inconsistent")
    # Assert
    with raised:
        ScratchRoot(root=None, source="default", reason="")


def test_a_none_answer_must_not_carry_a_root(tmp_path: Path) -> None:
    # Arrange — the positive control for the row above, from the other side.
    root = tmp_path
    # Act
    raised = pytest.raises(ValueError, match="inconsistent")
    # Assert
    with raised:
        ScratchRoot(root=root, source="none", reason="written")
