from __future__ import annotations

from scitex_agent_container.config._workdir_hook import mapped_workdir_mkdir_hook


def test_container_workdir_maps_to_writable_host_bind() -> None:
    # Arrange
    binds = ["/scratch/canary/workdir:/work:rw"]
    # Act
    hook = mapped_workdir_mkdir_hook("/work", binds)
    # Assert
    assert hook == "mkdir -p /scratch/canary/workdir/.claude"


def test_nested_workdir_uses_most_specific_bind() -> None:
    # Arrange
    binds = [
        "/scratch/root:/work:rw",
        "/scratch/project:/work/project:rw",
    ]
    # Act
    hook = mapped_workdir_mkdir_hook("/work/project/src", binds)
    # Assert
    assert hook == "mkdir -p /scratch/project/src/.claude"


def test_unmapped_container_workdir_does_not_become_a_host_hook() -> None:
    # Arrange
    binds = ["/scratch/data:/data:ro"]
    # Act
    hook = mapped_workdir_mkdir_hook("/work", binds)
    # Assert
    assert hook is None


def test_read_only_workdir_bind_is_not_mutated_on_the_host() -> None:
    # Arrange
    binds = ["/scratch/project:/work:ro"]
    # Act
    hook = mapped_workdir_mkdir_hook("/work", binds)
    # Assert
    assert hook is None


def test_host_hook_shell_quotes_the_mapped_path() -> None:
    # Arrange
    binds = ["/scratch/canary workdir:/work:rw"]
    # Act
    hook = mapped_workdir_mkdir_hook("/work", binds)
    # Assert
    assert hook == "mkdir -p '/scratch/canary workdir/.claude'"


def test_parent_segments_cannot_escape_the_declared_bind() -> None:
    # Arrange
    binds = ["/scratch/canary/workdir:/work:rw"]
    # Act
    hook = mapped_workdir_mkdir_hook("/work/../outside", binds)
    # Assert
    assert hook is None


def test_parent_segments_in_a_bind_destination_are_refused() -> None:
    # Arrange
    binds = ["/scratch/canary/workdir:/work/../outside:rw"]
    # Act
    hook = mapped_workdir_mkdir_hook("/work/../outside", binds)
    # Assert
    assert hook is None
