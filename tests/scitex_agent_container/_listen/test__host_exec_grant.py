"""``_listen._host_exec_grant`` — the spec.grants half of the host_exec gate.

Real spec files in ``tmp_path``, read through the injection seam the
group-authority module already exposes; no mocks.
"""

from __future__ import annotations

from pathlib import Path

from scitex_agent_container._listen._host_exec_grant import host_exec_grant


def _spec(tmp_path: Path, body: str) -> str:
    path = tmp_path / "spec.yaml"
    path.write_text(body, encoding="utf-8")
    return str(path)


def test_missing_fleet_spec_defers_to_group_gate(tmp_path: Path) -> None:
    # Arrange
    resolver = lambda _name: None  # noqa: E731
    # Act
    grant = host_exec_grant("ghost", spec_path_resolver=resolver)
    # Assert
    assert grant.allowed is True


def test_legacy_spec_without_grants_is_allowed(tmp_path: Path) -> None:
    # Arrange
    path = _spec(tmp_path, "spec:\n  workdir: /w\n")
    # Act
    grant = host_exec_grant("a", spec_path_resolver=lambda _n: path)
    # Assert
    assert grant.allowed is True


def test_explicit_host_exec_grant_is_allowed(tmp_path: Path) -> None:
    # Arrange
    path = _spec(
        tmp_path, "spec:\n  grants:\n    sac_control: true\n    host_exec: true\n"
    )
    # Act
    grant = host_exec_grant("a", spec_path_resolver=lambda _n: path)
    # Assert
    assert grant.allowed is True


def test_grants_block_without_host_exec_is_refused(tmp_path: Path) -> None:
    # Arrange
    path = _spec(tmp_path, "spec:\n  grants:\n    sac_control: true\n")
    # Act
    grant = host_exec_grant("a", spec_path_resolver=lambda _n: path)
    # Assert
    assert grant.allowed is False


def test_unreadable_spec_is_refused_not_allowed(tmp_path: Path) -> None:
    # Arrange
    path = _spec(tmp_path, "spec: [unclosed\n")
    # Act
    grant = host_exec_grant("a", spec_path_resolver=lambda _n: path)
    # Assert
    assert grant.allowed is False
