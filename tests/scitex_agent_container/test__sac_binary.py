"""Tests for the shared sac-executable resolver (``_sac_binary.py``).

Regression coverage for the systemd ``--user`` PATH bug: ``sac listen``
runs as a systemd ``--user`` service whose inherited PATH does not include
the venv's ``bin/`` directory (systemd ``--user`` units never source
``~/.bashrc``/``~/.bash_profile``). Code inside the daemon that shells out
to spawn/restart other agents used to resolve the child ``sac`` via
``shutil.which("sac") or "sac"``, silently falling back to an unresolvable
bare ``"sac"`` argv that only died later, deep inside ``subprocess.run``,
as an opaque ``FileNotFoundError`` — surfacing to HTTP callers as a bare
500 with zero diagnostic detail.

``sac_binary()`` fixes this by raising loudly, AT RESOLUTION TIME, when
neither PATH nor a ``sys.executable`` sibling resolves — see
``scitex_agent_container/_sac_binary.py`` for the full resolution-order
rationale (PATH first so PATH-shimmed tests / non-venv installs keep
working, then the ``sys.executable`` sibling, which is what actually
fixes the systemd bug since PATH lookup already fails there).

PA-306: no mocks / no ``monkeypatch`` fixture. Every fixture here is a
plain save/restore of real process state (``os.environ["PATH"]``,
``sys.executable``) — the same pattern ``tests/conftest.py``'s
``env_save_restore`` fixture uses for env vars.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

from scitex_agent_container._sac_binary import SacBinaryNotFoundError, sac_binary


@pytest.fixture
def real_executable_save_restore():
    """Save/restore ``sys.executable`` — no monkeypatch (PA-306).

    ``sys.executable`` is process-global interpreter state; the resolver
    reads it directly, so exercising the "no sibling sac" branch requires
    pointing it at a controlled fake interpreter path for the test.
    """
    saved = sys.executable
    try:
        yield
    finally:
        sys.executable = saved


@pytest.fixture
def unresolvable_sac(tmp_path: Path, env_save_restore, real_executable_save_restore):
    """Real PATH scrubbed to an empty dir + ``sys.executable`` pointed at
    an interpreter with no sibling ``sac`` — the exact production failure
    shape (systemd ``--user`` PATH lacks the venv's ``bin/``, so neither
    resolution branch finds anything)."""
    empty_bin = tmp_path / "empty_bin"
    empty_bin.mkdir()
    no_sac_dir = tmp_path / "no_sac_here"
    no_sac_dir.mkdir()
    fake_python = no_sac_dir / "python3"
    fake_python.write_text("#!/bin/sh\n")
    fake_python.chmod(0o755)
    env_save_restore.set("PATH", str(empty_bin))
    sys.executable = str(fake_python)


class TestSacBinaryRaisesLoudly:
    """The bug-fix contract: an unresolvable ``sac`` must raise, never
    silently return a bare ``"sac"`` argv token."""

    def test_raises_sac_binary_not_found(self, unresolvable_sac):
        # Arrange — fixture scrubs PATH and points sys.executable at an
        # interpreter dir with no sibling `sac` (the systemd --user
        # daemon's actual failure shape).
        # Act
        # Assert
        with pytest.raises(SacBinaryNotFoundError, match=r"(?i)sac"):
            sac_binary()

    def test_error_message_names_sys_executable(self, unresolvable_sac):
        # Arrange — the message must name sys.executable so an operator
        # can actually diagnose which interpreter/venv is missing `sac`.
        expected = re.escape(sys.executable)
        # Act
        # Assert
        with pytest.raises(SacBinaryNotFoundError, match=expected):
            sac_binary()

    def test_does_not_return_bare_sac_string(self, unresolvable_sac):
        # Arrange
        result: str | None
        # Act — the historical bug: a silent fallback to the literal "sac"
        # string, which then dies unresolvable deep inside subprocess.run.
        try:
            result = sac_binary()
        except SacBinaryNotFoundError:
            result = None
        # Assert
        assert result != "sac"


class TestSacBinaryResolutionOrder:
    """Both successful resolution branches, pinned independently."""

    def test_resolves_via_path_when_sys_executable_has_no_sibling(
        self, tmp_path: Path, env_save_restore, real_executable_save_restore
    ):
        # Arrange — real `sac` shim on PATH; sys.executable points at an
        # interpreter dir with NO sibling `sac`, so PATH is the only hit.
        fake_sac = tmp_path / "bin" / "sac"
        fake_sac.parent.mkdir()
        fake_sac.write_text("#!/bin/sh\nexit 0\n")
        fake_sac.chmod(0o755)
        env_save_restore.set("PATH", str(fake_sac.parent))
        no_sac_dir = tmp_path / "no_sac_here"
        no_sac_dir.mkdir()
        sys.executable = str(no_sac_dir / "python3")
        # Act
        resolved = sac_binary()
        # Assert
        assert resolved == str(fake_sac)

    def test_falls_back_to_sys_executable_sibling_when_path_misses(
        self, tmp_path: Path, env_save_restore, real_executable_save_restore
    ):
        # Arrange — empty PATH (the systemd --user bug shape) + a `sac`
        # console script colocated with sys.executable (the venv shape the
        # daemon's own ExecStart= relies on).
        empty_bin = tmp_path / "empty_bin"
        empty_bin.mkdir()
        env_save_restore.set("PATH", str(empty_bin))
        venv_bin = tmp_path / "venv" / "bin"
        venv_bin.mkdir(parents=True)
        fake_python = venv_bin / "python3"
        fake_python.write_text("#!/bin/sh\n")
        fake_python.chmod(0o755)
        fake_sac = venv_bin / "sac"
        fake_sac.write_text("#!/bin/sh\nexit 0\n")
        fake_sac.chmod(0o755)
        sys.executable = str(fake_python)
        # Act
        resolved = sac_binary()
        # Assert
        assert resolved == str(fake_sac)
