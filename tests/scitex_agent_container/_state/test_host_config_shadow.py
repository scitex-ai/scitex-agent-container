"""A PRESENT, well-formed ``config.yaml`` can still be the WRONG one.

INCIDENT 2026-08-05 (scitex-dev): they ran ``sac host add`` twice inside a
container, then ``sac host validate``, which answered ``ok, valid (2 peers)``.
The rows had landed in a 127-byte ``/home/agent/.scitex/agent-container/
config.yaml`` created that same day, while the operator's real four-peer config
under ``/home/ywatanabe`` — bind-mounted and readable from inside the container
— was never touched. Both commands reported success about the wrong file, and
the card was reported CLOSED on the strength of it, then retracted.

This is NOT the 2026-07-30 ABSENT case the module was written for; that one is
already caught. Here every state check legitimately passes, because the
resolved file really is a valid config — just not the fleet's. ``POPULATED``
emitted no diagnostic at all, so the one shape that silently diverges from
fleet state was the one shape we said nothing about.

NO MOCKS — real files in ``tmp_path``, with the homes root injected so the
guard is reachable from a test at all.
"""

from __future__ import annotations

from pathlib import Path

from scitex_agent_container._state.host_config_diagnose import (
    config_state_problems,
    find_shadowing_configs,
)


def _write_config(path: Path, peers: int) -> Path:
    """Write a well-formed config with ``peers`` named peer entries."""
    path.parent.mkdir(parents=True, exist_ok=True)
    body = "peers:\n" + "".join(
        f"  peer{i}:\n    ssh: user@peer{i}\n" for i in range(peers)
    )
    path.write_text(body if peers else "peers: {}\n", encoding="utf-8")
    return path


def test_config_under_another_home_is_reported(tmp_path: Path) -> None:
    """The operator's config in a different home must be surfaced."""
    # Arrange — a container-style home and an operator home, both valid.
    homes = tmp_path / "home"
    mine = _write_config(homes / "agent" / ".scitex/agent-container/config.yaml", 2)
    _write_config(homes / "ywatanabe" / ".scitex/agent-container/config.yaml", 4)
    # Act
    found = find_shadowing_configs(mine, homes_root=homes)
    # Assert
    assert [p.parent.parent.parent.name for p, _ in found] == ["ywatanabe"]


def test_peer_count_of_the_other_config_is_reported(tmp_path: Path) -> None:
    """The COUNT is the discriminator — 2 vs 4 is what names the split."""
    # Arrange
    homes = tmp_path / "home"
    mine = _write_config(homes / "agent" / ".scitex/agent-container/config.yaml", 2)
    _write_config(homes / "ywatanabe" / ".scitex/agent-container/config.yaml", 4)
    # Act
    found = find_shadowing_configs(mine, homes_root=homes)
    # Assert
    assert found[0][1] == 4


def test_resolved_config_is_not_its_own_shadow(tmp_path: Path) -> None:
    """The file we resolved must never be reported as shadowing itself."""
    # Arrange
    homes = tmp_path / "home"
    mine = _write_config(homes / "agent" / ".scitex/agent-container/config.yaml", 2)
    # Act
    found = find_shadowing_configs(mine, homes_root=homes)
    # Assert
    assert found == []


def test_same_file_reached_by_symlink_is_not_a_shadow(tmp_path: Path) -> None:
    """A second NAME for one file is not a second file.

    Measured 2026-08-05: the live spec path and the dotfiles path share one
    inode (4076503). Reporting an alias as a shadow would cry wolf on a
    perfectly normal layout, and a warning that cries wolf gets ignored on the
    day it is right.
    """
    # Arrange — real config under one home, the other home a symlink to it.
    homes = tmp_path / "home"
    real = _write_config(homes / "ywatanabe" / ".scitex/agent-container/config.yaml", 4)
    link_home = homes / "agent" / ".scitex" / "agent-container"
    link_home.mkdir(parents=True)
    (link_home / "config.yaml").symlink_to(real)
    # Act
    found = find_shadowing_configs(link_home / "config.yaml", homes_root=homes)
    # Assert
    assert found == []


def test_unparseable_other_config_is_ignored(tmp_path: Path) -> None:
    """Garbage elsewhere is not evidence of a fleet config."""
    # Arrange
    homes = tmp_path / "home"
    mine = _write_config(homes / "agent" / ".scitex/agent-container/config.yaml", 2)
    junk = homes / "someone" / ".scitex" / "agent-container"
    junk.mkdir(parents=True)
    (junk / "config.yaml").write_text("::: not yaml :::\n", encoding="utf-8")
    # Act
    found = find_shadowing_configs(mine, homes_root=homes)
    # Assert
    assert found == []


def test_populated_config_now_warns_when_shadowed(tmp_path: Path) -> None:
    """THE REGRESSION GUARD: POPULATED used to emit nothing at all.

    scitex-dev's config was present, well-formed and populated, so every
    existing branch passed in silence. If a future refactor drops the shadow
    check, this test goes red instead of an agent reporting a false success.
    """
    # Arrange
    homes = tmp_path / "home"
    mine = _write_config(homes / "agent" / ".scitex/agent-container/config.yaml", 2)
    _write_config(homes / "ywatanabe" / ".scitex/agent-container/config.yaml", 4)
    # Act
    import scitex_agent_container._state.host_config_diagnose as diag

    saved, diag.HOMES_ROOT = diag.HOMES_ROOT, homes
    try:
        _errors, warnings, _detail = config_state_problems(mine)
    finally:
        diag.HOMES_ROOT = saved
    # Assert
    assert any("ANOTHER config.yaml" in w for w in warnings)


def test_shadow_does_not_fail_the_gate(tmp_path: Path) -> None:
    """A shadow is a WARNING, not an error — validate must still exit 0.

    Two homes holding configs is legitimate on a bare host; only the container
    case is suspicious, and this module cannot tell them apart with certainty.
    Failing here would teach operators to ignore the check, which is how the
    2026-07-30 absent-config defect survived.
    """
    # Arrange
    homes = tmp_path / "home"
    mine = _write_config(homes / "agent" / ".scitex/agent-container/config.yaml", 2)
    _write_config(homes / "ywatanabe" / ".scitex/agent-container/config.yaml", 4)
    # Act
    import scitex_agent_container._state.host_config_diagnose as diag

    saved, diag.HOMES_ROOT = diag.HOMES_ROOT, homes
    try:
        errors, _warnings, _detail = config_state_problems(mine)
    finally:
        diag.HOMES_ROOT = saved
    # Assert
    assert errors == []
