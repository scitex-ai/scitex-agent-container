"""Tests for ``_apptainer_build`` fakeroot-on-non-root auto-detection.

Operator gotcha 2026-06-03 (lead msg ``dab9b3a5fc99438c9143309df271ce69``):
``sac image build`` ran ``apptainer build`` with NO ``--fakeroot`` on a
host where apptainer wasn't setuid → apptainer fell back to
``sudo apptainer build`` → headless/agent context can't supply a
password → silent build failure.

Fix: auto-detect when the current user has ``/etc/subuid`` + ``/etc/subgid``
mappings and inject ``--fakeroot`` into the build argv.

No mocks (PA-306). Test seams (``euid``, ``subuid_path``, ``subgid_path``)
on the probe + ``subprocess_shim`` for the build subprocess. AAA + ≥3-word
test names + one assert per test (PA-307 / STX-TQ002).
"""

from __future__ import annotations

import json
import os
import pwd
import sys
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from scitex_agent_container.runtimes import _apptainer_build as build_mod
from scitex_agent_container.runtimes._apptainer_build import (
    _build_argv_prefix,
    _build_sif_from_def,
    _build_sif_from_uri,
    _has_subid_entry,
    _should_use_fakeroot_for_build,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


@contextmanager
def _swap_module_attr(name: str, value) -> Iterator[None]:
    """Replace ``build_mod.<name>`` for the duration of the block.

    Same shape as the project's other ``_swap_*`` helpers in the test
    suite (e.g. ``test_image_group._use_apptainer``). No MagicMock.
    """
    saved = getattr(build_mod, name)
    setattr(build_mod, name, value)
    try:
        yield
    finally:
        setattr(build_mod, name, saved)


def _write_subid(path: Path, *entries: str) -> None:
    """Write a ``/etc/sub{u,g}id``-shaped file. Each entry is one line."""
    path.write_text("\n".join(entries) + ("\n" if entries else ""))


def _install_apptainer_shim(bin_dir: Path) -> Path:
    """Install a fake ``apptainer`` binary that records argv + creates
    the SIF output file so the production ``rc==0`` happy path runs.

    Mirrors the existing ``apptainer_on_path`` fixture pattern in
    ``test__apptainer_runtime.py``; reimplemented here to keep this
    new module self-contained.
    """
    log = bin_dir / "apptainer.argv.jsonl"
    script = bin_dir / "apptainer"
    body = (
        f"#!{sys.executable}\n"
        "import json, sys, pathlib\n"
        f"with open({json.dumps(str(log))}, 'a') as fh:\n"
        "    fh.write(json.dumps(sys.argv[1:]) + '\\n')\n"
        # Build: argv layout is ["build", maybe "--fakeroot", "<sif>", "<src>"]
        # Last arg is the source; second-to-last (or third-to-last under
        # --fakeroot) is the output SIF. Find it by scanning for the
        # first .sif positional after 'build'.
        "args = sys.argv[1:]\n"
        "for a in args[1:]:\n"
        "    if a.endswith('.sif'):\n"
        "        pathlib.Path(a).write_bytes(b'\\x00')\n"
        "        break\n"
        "sys.exit(0)\n"
    )
    script.write_text(body)
    script.chmod(0o755)
    return log


# ---------------------------------------------------------------------------
# _has_subid_entry — file-format parsing
# ---------------------------------------------------------------------------


def test_has_subid_entry_true_when_username_prefix_matches(tmp_path: Path) -> None:
    # Arrange — a real /etc/subuid-shaped file with our user's mapping.
    subid = tmp_path / "subuid"
    _write_subid(subid, "myuser:100000:65536", "otheruser:165536:65536")
    # Act
    present = _has_subid_entry(subid, "myuser")
    # Assert
    assert present is True


def test_has_subid_entry_false_when_username_absent(tmp_path: Path) -> None:
    # Arrange
    subid = tmp_path / "subuid"
    _write_subid(subid, "someoneelse:100000:65536")
    # Act
    present = _has_subid_entry(subid, "myuser")
    # Assert
    assert present is False


def test_has_subid_entry_false_when_file_missing(tmp_path: Path) -> None:
    # Arrange
    subid = tmp_path / "absent-subuid"
    # Act
    present = _has_subid_entry(subid, "myuser")
    # Assert
    assert present is False


def test_has_subid_entry_does_not_match_prefix_collision(tmp_path: Path) -> None:
    # Arrange — "myuser-suffix" must NOT match "myuser" (need exact
    # ``<name>:`` prefix). The check uses ``startswith(user + ":")``
    # which already enforces this; pin it so a future refactor that
    # accidentally drops the colon trips a red test.
    subid = tmp_path / "subuid"
    _write_subid(subid, "myuser-suffix:100000:65536")
    # Act
    present = _has_subid_entry(subid, "myuser")
    # Assert
    assert present is False


# ---------------------------------------------------------------------------
# _should_use_fakeroot_for_build — composite decision
# ---------------------------------------------------------------------------


def test_should_use_fakeroot_false_when_root(tmp_path: Path) -> None:
    # Arrange — root doesn't need fakeroot.
    subuid = tmp_path / "subuid"
    subgid = tmp_path / "subgid"
    _write_subid(subuid, "myuser:100000:65536")
    _write_subid(subgid, "myuser:100000:65536")
    # Act
    use = _should_use_fakeroot_for_build(euid=0, subuid_path=subuid, subgid_path=subgid)
    # Assert
    assert use is False


def test_should_use_fakeroot_false_when_subuid_missing(tmp_path: Path) -> None:
    # Arrange — non-root user, no subuid entry → can't use fakeroot.
    subuid = tmp_path / "subuid"
    subgid = tmp_path / "subgid"
    _write_subid(subuid)  # empty
    _write_subid(subgid, _current_username() + ":100000:65536")
    # Act
    use = _should_use_fakeroot_for_build(
        euid=os.geteuid(), subuid_path=subuid, subgid_path=subgid
    )
    # Assert
    assert use is False


def test_should_use_fakeroot_false_when_subgid_missing(tmp_path: Path) -> None:
    # Arrange
    subuid = tmp_path / "subuid"
    subgid = tmp_path / "subgid"
    _write_subid(subuid, _current_username() + ":100000:65536")
    _write_subid(subgid)
    # Act
    use = _should_use_fakeroot_for_build(
        euid=os.geteuid(), subuid_path=subuid, subgid_path=subgid
    )
    # Assert
    assert use is False


def test_should_use_fakeroot_true_when_both_subid_files_have_entries(
    tmp_path: Path,
) -> None:
    # Arrange — the happy path the operator was missing.
    subuid = tmp_path / "subuid"
    subgid = tmp_path / "subgid"
    _write_subid(subuid, _current_username() + ":100000:65536")
    _write_subid(subgid, _current_username() + ":100000:65536")
    # Act
    use = _should_use_fakeroot_for_build(
        euid=os.geteuid(), subuid_path=subuid, subgid_path=subgid
    )
    # Assert
    assert use is True


def _current_username() -> str:
    """Return the real username for the running process's effective UID.

    Tests need to write a subuid entry that matches the user the
    production probe sees — otherwise the probe says "no match" and
    the test compares against the wrong condition. Real pwd lookup,
    no fakes.
    """
    return pwd.getpwuid(os.geteuid()).pw_name


# ---------------------------------------------------------------------------
# _build_argv_prefix — composed argv head
# ---------------------------------------------------------------------------


def test_build_argv_prefix_starts_with_apptainer_build() -> None:
    # Arrange — fakeroot probe forced off so the head is the minimum.
    # Act
    with _swap_module_attr("_should_use_fakeroot_for_build", lambda: False):
        argv = _build_argv_prefix()
    # Assert
    assert argv[:2] == ["apptainer", "build"]


def test_build_argv_prefix_includes_fakeroot_when_probe_true() -> None:
    # Arrange
    # Act
    with _swap_module_attr("_should_use_fakeroot_for_build", lambda: True):
        argv = _build_argv_prefix()
    # Assert
    assert "--fakeroot" in argv


def test_build_argv_prefix_omits_fakeroot_when_probe_false() -> None:
    # Arrange
    # Act
    with _swap_module_attr("_should_use_fakeroot_for_build", lambda: False):
        argv = _build_argv_prefix()
    # Assert
    assert "--fakeroot" not in argv


# ---------------------------------------------------------------------------
# _build_sif_from_def — end-to-end argv shape
# ---------------------------------------------------------------------------


def test_build_sif_from_def_passes_fakeroot_when_probe_true(
    tmp_path: Path, env_save_restore
) -> None:
    # Arrange — install a fake apptainer that records argv, force the
    # probe to True.
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log = _install_apptainer_shim(bin_dir)
    env_save_restore.set("PATH", f"{bin_dir}:{os.environ.get('PATH', '')}")
    def_file = tmp_path / "x.def"
    def_file.write_text("Bootstrap: docker\n")
    sif_path = tmp_path / "out.sif"
    # Act
    with _swap_module_attr("_should_use_fakeroot_for_build", lambda: True):
        _build_sif_from_def(sif_path, def_file)
    # Assert
    last_argv = json.loads(log.read_text().splitlines()[-1])
    assert "--fakeroot" in last_argv


def test_build_sif_from_def_omits_fakeroot_when_probe_false(
    tmp_path: Path, env_save_restore
) -> None:
    # Arrange
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log = _install_apptainer_shim(bin_dir)
    env_save_restore.set("PATH", f"{bin_dir}:{os.environ.get('PATH', '')}")
    def_file = tmp_path / "x.def"
    def_file.write_text("Bootstrap: docker\n")
    sif_path = tmp_path / "out.sif"
    # Act
    with _swap_module_attr("_should_use_fakeroot_for_build", lambda: False):
        _build_sif_from_def(sif_path, def_file)
    # Assert
    last_argv = json.loads(log.read_text().splitlines()[-1])
    assert "--fakeroot" not in last_argv


def test_build_sif_from_def_returns_true_on_success(
    tmp_path: Path, env_save_restore
) -> None:
    # Arrange
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _install_apptainer_shim(bin_dir)
    env_save_restore.set("PATH", f"{bin_dir}:{os.environ.get('PATH', '')}")
    def_file = tmp_path / "x.def"
    def_file.write_text("Bootstrap: docker\n")
    sif_path = tmp_path / "out.sif"
    # Act
    with _swap_module_attr("_should_use_fakeroot_for_build", lambda: True):
        ok = _build_sif_from_def(sif_path, def_file)
    # Assert
    assert ok is True


# ---------------------------------------------------------------------------
# _build_sif_from_uri — symmetric argv shape
# ---------------------------------------------------------------------------


def test_build_sif_from_uri_passes_fakeroot_when_probe_true(
    tmp_path: Path, env_save_restore
) -> None:
    # Arrange
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log = _install_apptainer_shim(bin_dir)
    env_save_restore.set("PATH", f"{bin_dir}:{os.environ.get('PATH', '')}")
    sif_path = tmp_path / "out.sif"
    # Act
    with _swap_module_attr("_should_use_fakeroot_for_build", lambda: True):
        _build_sif_from_uri(sif_path, "docker://python:3.11-slim")
    # Assert
    last_argv = json.loads(log.read_text().splitlines()[-1])
    assert "--fakeroot" in last_argv


def test_build_sif_from_uri_omits_fakeroot_when_probe_false(
    tmp_path: Path, env_save_restore
) -> None:
    # Arrange
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log = _install_apptainer_shim(bin_dir)
    env_save_restore.set("PATH", f"{bin_dir}:{os.environ.get('PATH', '')}")
    sif_path = tmp_path / "out.sif"
    # Act
    with _swap_module_attr("_should_use_fakeroot_for_build", lambda: False):
        _build_sif_from_uri(sif_path, "docker://python:3.11-slim")
    # Assert
    last_argv = json.loads(log.read_text().splitlines()[-1])
    assert "--fakeroot" not in last_argv


def test_build_sif_from_uri_targets_correct_source_uri(
    tmp_path: Path, env_save_restore
) -> None:
    # Arrange — pin that --fakeroot insertion does NOT displace the
    # source URI from its trailing position (regression guard: argv
    # order must remain `... <sif> <src>`).
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log = _install_apptainer_shim(bin_dir)
    env_save_restore.set("PATH", f"{bin_dir}:{os.environ.get('PATH', '')}")
    sif_path = tmp_path / "out.sif"
    uri = "docker://alpine:3.20"
    # Act
    with _swap_module_attr("_should_use_fakeroot_for_build", lambda: True):
        _build_sif_from_uri(sif_path, uri)
    # Assert
    last_argv = json.loads(log.read_text().splitlines()[-1])
    assert last_argv[-1] == uri
