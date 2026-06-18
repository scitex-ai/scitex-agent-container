"""``_format_boot_stderr_section`` — surface the captured inner-boot stderr.

Mirror for ``_lifecycle/_start.py`` boot-failure diagnostics: ``TuiSessionRuntime``
redirects the inner ``apptainer exec … claude`` STDERR to
``<state>/boot.stderr.log`` (B->A feedback), and a failed ``agent_start``
surfaces its tail LOUDLY instead of a silent "<empty>" pane. The formatter is
a pure function over a ``Path`` — real tmp files, no mocks (PA-306).
STX-TQ002 AAA-marker + STX-TQ007 one-assert.
"""

from __future__ import annotations

from pathlib import Path

from scitex_agent_container._lifecycle._start import _format_boot_stderr_section


def test_format_boot_stderr_section_surfaces_captured_fatal(tmp_path: Path) -> None:
    # Arrange — a boot.stderr.log holding the real apptainer FATAL.
    log = tmp_path / "boot.stderr.log"
    log.write_text("FATAL: container creation failed: mount error\n", encoding="utf-8")
    # Act
    section = _format_boot_stderr_section(log)
    # Assert
    assert "FATAL: container creation failed" in section


def test_format_boot_stderr_section_marks_absent_log(tmp_path: Path) -> None:
    # Arrange — no log file (the inner process never launched).
    log = tmp_path / "boot.stderr.log"
    # Act
    section = _format_boot_stderr_section(log)
    # Assert
    assert "<no stderr captured" in section
