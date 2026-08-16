"""A `test_*.py` in the mirror tree must mirror a `src/` module — fast, locally.

WHY THIS EXISTS (PR #1026, run 31607851531, both matrix legs red)
=================================================================
A shared test-helper landed at ``tests/scitex_agent_container/_helpers/ports.py``
— correct, and the same shape as its siblings ``loopback_server.py`` and
``ssh_exec_shim.py``. Its test landed beside it as ``_helpers/test_ports.py``,
which is the obvious place and the wrong one::

    [E] [PS-204 §2 orphan-test-file]
        tests/scitex_agent_container/_helpers/test_ports.py:
        no matching src file (orphan test);
        mirror dir `src/scitex_agent_container/_helpers` does not exist

``tests/<pkg>/`` is the MIRROR TREE: a ``test_X.py`` there asserts that
``src/<pkg>/.../X.py`` exists. A `_helpers/` package is test-only by design and
has no `src/` counterpart — PS-207 is even documented as "src-aware so it never
flags fixture trees that legitimately have no source counterpart", which is
exactly why dropping a `test_*.py` into one is such a quiet trap: the directory
is blessed, so nothing warns you until CI. Tests with no src counterpart belong
OUTSIDE the mirror tree (``tests/develop/``, ``tests/integration/``,
``tests/e2e/``, ``tests/smoke/``), which is where this file and the relocated
``test_dead_port_helper.py`` now live.

WHY NOT JUST LEAN ON ``test_audit.py``
--------------------------------------
It does catch this — it is what went red — but only after a full
``scitex-dev ecosystem audit-all``, and it is skippable
(``SCITEX_DEV_SKIP_AUDIT=1``) and silent when the audit corpus is not
installed. So the trap can reach a push without a word. This is a
millisecond, dependency-free echo of one narrow half of PS-204 §2 — the
missing-mirror-DIRECTORY case, which is the half that bites — so the mistake
surfaces in a normal local run.

**scitex-dev remains the source of truth.** This deliberately does NOT
re-implement the rest of PS-204 (per-file basename matching, the enriched
relocate hint, the public/private prefix rules of PS-205). A directory-level
invariant cannot drift away from the auditor the way a re-implemented
file-level one would.

NO MOCKS — real directories on disk, including the negative control.

AAA markers (TQ002); 3+-word test names.
"""

from __future__ import annotations

from pathlib import Path
from typing import List, Tuple

_REPO_ROOT = Path(__file__).resolve().parents[2]
_TESTS_PKG = _REPO_ROOT / "tests" / "scitex_agent_container"
_SRC_PKG = _REPO_ROOT / "src" / "scitex_agent_container"

# Directories outside the mirror tree, where a test with no src counterpart
# belongs. Named here so the failure message can point at them.
_NON_MIRROR_HOMES = ("tests/develop/", "tests/integration/", "tests/e2e/", "tests/smoke/")


def _unmirrored_test_dirs(tests_pkg: Path, src_pkg: Path) -> List[Tuple[Path, Path]]:
    """Every dir holding a ``test_*.py`` whose ``src/`` mirror dir is missing.

    Returns ``(test_dir, expected_src_dir)`` pairs so the caller can print both
    halves — "what you wrote" and "what would have had to exist".
    """
    test_dirs = {p.parent for p in tests_pkg.rglob("test_*.py")}
    missing = []
    for test_dir in sorted(test_dirs):
        mirror = src_pkg / test_dir.relative_to(tests_pkg)
        if not mirror.is_dir():
            missing.append((test_dir, mirror))
    return missing


def _render(offenders: List[Tuple[Path, Path]]) -> str:
    lines = ["test files live in the mirror tree but have no src/ mirror dir:"]
    for test_dir, mirror in offenders:
        rel = test_dir.relative_to(_REPO_ROOT)
        names = sorted(p.name for p in test_dir.glob("test_*.py"))
        lines.append(f"  {rel}/  ({', '.join(names)})")
        lines.append(f"      needs {mirror.relative_to(_REPO_ROOT)}/ to exist")
    lines.append("")
    lines.append("This is PS-204 §2 (orphan-test-file). If the code under test is")
    lines.append("test-only support with no src/ counterpart, the TEST moves out of")
    lines.append("the mirror tree — the helper itself stays put. Homes: " )
    lines.append("  " + "  ".join(_NON_MIRROR_HOMES))
    return "\n".join(lines)


def test_every_test_dir_mirrors_a_src_dir():
    # Arrange — the real tree, as shipped.
    # Act
    offenders = _unmirrored_test_dirs(_TESTS_PKG, _SRC_PKG)
    # Assert
    assert offenders == [], _render(offenders)


def test_the_guard_detects_an_unmirrored_test_dir(tmp_path):
    # Arrange — the negative control, built from REAL directories: a test-only
    # `_helpers` package holding a test, exactly the 2026-08-12 mistake. A
    # guard that cannot fail is not a guard.
    tests_pkg = tmp_path / "tests" / "pkg"
    (tests_pkg / "_helpers").mkdir(parents=True)
    (tests_pkg / "_helpers" / "test_ports.py").write_text("def test_x(): pass\n")
    src_pkg = tmp_path / "src" / "pkg"
    src_pkg.mkdir(parents=True)
    # Act
    offenders = _unmirrored_test_dirs(tests_pkg, src_pkg)
    # Assert
    assert [d.name for d, _ in offenders] == ["_helpers"]


def test_the_guard_passes_a_mirrored_test_dir(tmp_path):
    # Arrange — the same shape, but with the src/ mirror present: this one is
    # legitimate and must NOT be flagged, or the guard would block real tests.
    tests_pkg = tmp_path / "tests" / "pkg"
    (tests_pkg / "cli").mkdir(parents=True)
    (tests_pkg / "cli" / "test_run.py").write_text("def test_x(): pass\n")
    src_pkg = tmp_path / "src" / "pkg"
    (src_pkg / "cli").mkdir(parents=True)
    # Act
    offenders = _unmirrored_test_dirs(tests_pkg, src_pkg)
    # Assert
    assert offenders == []
