"""The bake's scratch must live on ONE filesystem — node-local, not GPFS.

MEASURED (2026-07-19). The build ``srun`` in ``containers/spartan-sif-bake.sh``
exported ``APPTAINER_TMPDIR`` onto node-local ext4 and ``APPTAINER_CACHEDIR``
onto ``$WORKDIR/apptainer-cache`` on GPFS, so a single apptainer build ran
across two filesystems with different consistency semantics. Two live bakes
died at DIFFERENT points — one after writing a complete 1.4GB ``.partial``,
one mid-build during apt — with no error, no signal and no ``SAC_BAKE_RESULT``
line. A deterministic control-flow bug dies in the same place every run; the
divergence is what a filesystem consistency race looks like. Spartan's GPFS
has a documented read-after-write race (the CI-runner ``_temp`` incident:
ESTALE / "Unknown system error -116", re-confirmed live 2026-07-18) and the
remedy there was the same one: move the scratch off GPFS.

This is a leading hypothesis, not a proven cure — an intermittent fault is not
disproved by one green bake. What these tests DO pin is the invariant the fix
asserts, so nobody silently re-splits the scratch across two filesystems while
chasing a cache hit. The control tests mutate the shipped script back to its
pre-fix shape: a checker that cannot go red proves nothing about the green.
"""

from __future__ import annotations

import posixpath
import re
from pathlib import Path

import scitex_agent_container

BAKE_SCRIPT = (
    Path(scitex_agent_container.__file__).resolve().parent
    / "containers"
    / "spartan-sif-bake.sh"
)

# The pre-fix export line, verbatim, for the mutation controls.
_FIXED_EXPORT = (
    '--export=ALL,APPTAINER_TMPDIR="$BUILD_SCRATCH/tmp"'
    ',APPTAINER_CACHEDIR="$BUILD_SCRATCH/cache"'
)
_SPLIT_EXPORT = (
    '--export=ALL,APPTAINER_TMPDIR="/tmp/sac-sif-bake-$USER"'
    ',APPTAINER_CACHEDIR="$WORKDIR/apptainer-cache"'
)

_ASSIGNMENT = re.compile(r'(APPTAINER_(?:TMPDIR|CACHEDIR))="([^"]*)"')
_TOP_LEVEL_ASSIGNMENT = re.compile(r'^([A-Z_][A-Z0-9_]*)="([^"]*)"', re.MULTILINE)
_VAR_REF = re.compile(r"\$([A-Z_][A-Z0-9_]*)")


def _script_source() -> str:
    return BAKE_SCRIPT.read_text(encoding="utf-8")


def _export_line(source: str) -> str:
    """The build srun's ``--export=`` line. Exactly one, or the parse is a lie."""
    lines = [ln for ln in source.splitlines() if "--export=ALL," in ln]
    if len(lines) != 1:
        raise AssertionError(f"expected 1 build --export= line, found {len(lines)}")
    return lines[0]


def _scratch_dirs(source: str) -> dict[str, str]:
    """Map ``APPTAINER_TMPDIR``/``APPTAINER_CACHEDIR`` to their exported paths."""
    found = dict(_ASSIGNMENT.findall(_export_line(source)))
    missing = {"APPTAINER_TMPDIR", "APPTAINER_CACHEDIR"} - set(found)
    if missing:
        raise AssertionError(f"build srun exports neither of: {sorted(missing)}")
    return found


def _mkdir_command(source: str) -> str:
    """The ``mkdir -p`` the build srun runs ON THE NODE, and only that.

    Scoped to the ``bash -c`` payload on purpose. Searching the whole file
    would match the ``--export=`` line itself, so the check would pass on a
    script that creates nothing — a test the mutation cannot move.
    """
    lines = [ln for ln in source.splitlines() if "bash -c" in ln and "mkdir -p" in ln]
    if len(lines) != 1:
        raise AssertionError(f"expected 1 build `bash -c` mkdir, found {len(lines)}")
    return lines[0]


def _expand(path: str, source: str) -> str:
    """Substitute the script's own top-level ``NAME="literal"`` assignments.

    Comparing the raw text would call two spellings of one directory different
    and one variable used twice the same — neither is what "same filesystem"
    means. ``$USER`` has no assignment and stays literal; it is identical on
    both sides, so it cannot hide a split.
    """
    literals = dict(_TOP_LEVEL_ASSIGNMENT.findall(source))
    return _VAR_REF.sub(
        lambda m: literals.get(m.group(1), m.group(0)),
        path,
    )


def _scratch_parents(source: str) -> tuple[str, str]:
    """The parent dir of each scratch path — equal iff they share a root."""
    dirs = _scratch_dirs(source)
    return (
        posixpath.dirname(_expand(dirs["APPTAINER_TMPDIR"], source)),
        posixpath.dirname(_expand(dirs["APPTAINER_CACHEDIR"], source)),
    )


def test_build_scratch_tmpdir_and_cachedir_share_a_parent() -> None:
    # Arrange — the whole point: one apptainer build, one filesystem.
    tmp_parent, cache_parent = _scratch_parents(_script_source())
    # Act
    same_root = tmp_parent == cache_parent
    # Assert
    assert same_root, f"scratch split across {tmp_parent!r} and {cache_parent!r}"


def test_build_scratch_parent_is_node_local_tmp() -> None:
    # Arrange — sharing a root is not enough; the shared root must be the
    # node-local one. Both paths under $WORKDIR would pass the check above
    # and put the entire build back on GPFS.
    tmp_parent, _ = _scratch_parents(_script_source())
    # Act
    node_local = tmp_parent.startswith("/tmp/")
    # Assert
    assert node_local, f"build scratch parent {tmp_parent!r} is not under /tmp/"


def test_build_srun_creates_both_scratch_directories() -> None:
    # Arrange — apptainer does not create a missing CACHEDIR tree for us, and
    # the node-local scratch is empty on every fresh node by construction.
    source = _script_source()
    dirs = _scratch_dirs(source)
    mkdir = _mkdir_command(source)
    # Act
    created = [path for path in dirs.values() if path in mkdir]
    # Assert
    assert sorted(created) == sorted(dirs.values()), f"not mkdir'd: {dirs}"


def test_control_the_split_scratch_fails_the_shared_parent_check() -> None:
    # Arrange — MUTATION PROOF. Put the pre-2026-07-19 export line back on the
    # real shipped script. If the check still reports one filesystem, the
    # green above is measuring nothing and this file is decoration.
    mutated = _script_source().replace(_FIXED_EXPORT, _SPLIT_EXPORT)
    # Act
    tmp_parent, cache_parent = _scratch_parents(mutated)
    # Assert
    assert tmp_parent != cache_parent


def test_control_the_mutation_actually_edits_the_script() -> None:
    # Arrange — a replace() that matched nothing would make the control above
    # pass against unmutated text, which is the same failure it exists to
    # catch. Pin that the fixed export line is really the one we ship.
    source = _script_source()
    # Act
    mutated = source.replace(_FIXED_EXPORT, _SPLIT_EXPORT)
    # Assert
    assert mutated != source
