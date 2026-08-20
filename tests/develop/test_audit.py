"""Audit conformance — runs `scitex-dev ecosystem audit-all` against
THIS CHECKOUT as a normal pytest test.

WHY THIS FILE PASSES AN EXPLICIT `path=` — DO NOT "SIMPLIFY" IT AWAY
====================================================================
`audit_all_for_package(distribution)` takes a distribution NAME, not a
path. Called with the name alone, `scitex-dev` has no argument telling
it WHICH tree you meant, so it resolves one by guessing — and its last
resort (`_resolve_repo_root` step 4, still present in 0.31.1) is a
``~/proj/<distribution>`` development guess.

On the self-hosted Spartan runner ``~`` is the OPERATOR'S REAL HOME and
that guess lands on a persistent, human/agent-writable dev checkout —
NOT the workspace CI checked out. The gate then grades a tree that has
no relationship to the commit under test, and reports a confident
pass/fail about source it never read. It was false in BOTH directions:

  * false RED  — any agent who `git checkout`s a branch in Spartan's sac
    clone reddens every other branch's CI. It took down the v0.21.20
    release for ~40 min, and a `develop` run once failed naming a file
    at its OLD name, on a branch that had already merged the rename.
  * false GREEN — the dangerous one. If the runner's checkout happens to
    be self-consistent the test PASSES while telling you nothing about
    the branch you are shipping. #685's orphan test reached develop that
    way: the audit was grading a five-release-stale tree that did not
    yet contain #685's new file.

A gate that grades the wrong tree is worse than no gate: it is
NON-DETERMINISTIC (identical code passes at 07:15 and fails at 07:23),
and its green is LOAD-BEARING — people merge on it.

The cure is to name the tree explicitly. `path=` is `_resolve_repo_root`
step 1 and is AUTHORITATIVE — it short-circuits every guess. We anchor it
on ``__file__``, which is by construction inside the checkout pytest is
running against, so the gate reads THE CODE UNDER TEST or fails loudly.
This mirrors what our sibling gates already do (`test_git_hooks.py`'s
``_REPO``, `test_skills_quality.py`'s ``package_root``).

scitex-dev >= 0.31.1 is REQUIRED (see [dev] in pyproject.toml): `path=`
first exists there. Older versions also lack the CWD-git-root safety net
(step 2), so on them the home-disk guess is the ONLY fallback. The
fixture asserts that support rather than silently dropping `path=` —
dropping it would restore the exact bug this file exists to prevent.

Bypass (exceptions / temporal remedy):
    SCITEX_DEV_SKIP_AUDIT=1 python -m pytest .

Use when remediating pre-existing violations or developing without the
audit corpus available locally. CI for release branches MUST NOT set
this — drift goes silent.
"""

from __future__ import annotations

import importlib.util
import inspect
import sys
from pathlib import Path

import pytest
import tomllib

_DISTRIBUTION = "scitex-agent-container"

# tests/develop/test_audit.py -> parents[2] is the repo root. Anchored on
# __file__, never on $HOME/CWD/an installed location: those answer "where
# is *a* checkout of this distribution on this disk?", which is NOT the
# question a gate asks. It asks "the tree I am running against".
_REPO_ROOT = Path(__file__).resolve().parents[2]


def _verified_repo_root() -> Path:
    """Return the checkout THIS TEST FILE lives in, or fail loudly.

    A validator, not a formality. If the anchor ever stops pointing at
    sac's repo root (a moved test file, a changed layout), we must NOT
    fall back to a guess and audit some other tree — that is the exact
    defect this file exists to prevent. So: fail, loudly, naming what we
    found. Never substitute silently.
    """
    pyproject = _REPO_ROOT / "pyproject.toml"
    if not pyproject.is_file():
        pytest.fail(
            f"cannot locate the checkout under test: no pyproject.toml at "
            f"{pyproject}. This gate refuses to fall back to a ~/proj/"
            f"{_DISTRIBUTION} guess — that would audit a tree unrelated to "
            f"the commit under test. Fix the _REPO_ROOT anchor in {__file__}."
        )
    declared = (tomllib.loads(pyproject.read_text()).get("project") or {}).get("name")
    if declared != _DISTRIBUTION:
        pytest.fail(
            f"the tree at {_REPO_ROOT} declares [project] name={declared!r}, "
            f"not {_DISTRIBUTION!r} — so it is NOT this package's checkout. "
            f"Refusing to audit it: a gate that grades the wrong tree reports "
            f"a confident pass/fail about source it never read."
        )
    return _REPO_ROOT


@pytest.fixture
def scitex_dev_audit():
    # PROBE WHAT THE AUDITOR ACTUALLY RUNS, NOT A CONSOLE SCRIPT IT IGNORES.
    #
    # This used to be `shutil.which("scitex-dev") is None -> skip`. The
    # auditor never invokes that binary. `_audit_conformance.py` builds its
    # argv as `[sys.executable, "-m", "scitex_dev", ...]`, with the comment
    # "`sys.executable -m` binds the auditor to the interpreter running the
    # tests" — so what matters is whether THIS interpreter can import
    # scitex_dev, and the console script's presence on $PATH is unrelated.
    #
    # The two disagree in the common case and the gate lost. Measured
    # 2026-08-20 on scitex-compute-04: scitex-dev is installed in
    # ~/.venv/bin, which is not on the $PATH the test process inherits, so
    # `which` returned None and this gate SKIPPED — while the same
    # interpreter ran `python -m scitex_dev ecosystem audit-all` to
    # completion, exit 0. Re-running the suite with that directory on $PATH
    # turned "1 passed, 1 skipped" into "2 passed in 83s".
    #
    # THE COST WAS PAID THE SAME DAY. PR #1155 added a test file in a
    # location PS-204 rejects. Local runs of tests/develop/ reported
    # "1 passed, 1 skipped" three times and looked like coverage; the
    # skipped one WAS the gate. CI caught it, and the message told me
    # scitex-dev was "not installed" — false, and it points at a fix that
    # changes nothing.
    #
    # So: skip only when the MODULE is genuinely unavailable, which is the
    # condition the auditor cannot survive. importlib rather than a bare
    # try/import so that an ImportError raised from INSIDE scitex_dev — a
    # real breakage — still propagates instead of being read as absence.
    if importlib.util.find_spec("scitex_dev") is None:
        pytest.skip(
            "scitex_dev is not importable by this interpreter "
            f"({sys.executable}) — add `scitex-dev[cli-audit]` to "
            "[project.optional-dependencies.dev]. Note this is about the "
            "MODULE, not the `scitex-dev` console script: the auditor runs "
            "`sys.executable -m scitex_dev` and never looks at $PATH."
        )
    from scitex_dev.testing import audit_all_for_package

    if "path" not in inspect.signature(audit_all_for_package).parameters:
        pytest.fail(
            "installed scitex-dev's audit_all_for_package() has no `path` "
            "parameter, so this gate cannot name the tree under test and "
            "would silently audit a ~/proj guess instead. Upgrade to "
            "scitex-dev>=0.31.1 (the [dev] floor in pyproject.toml). "
            "Failing loudly rather than auditing the wrong checkout."
        )
    return audit_all_for_package


def test_audit_target_is_the_checkout_under_test():
    """The anchor resolves to the tree THIS FILE lives in — the regression
    guard against reintroducing a home-disk guess.

    Falsifiable, and that is the point: re-point `_REPO_ROOT` at
    ``Path.home()/"proj"/_DISTRIBUTION`` (the old behaviour) and this test
    goes RED, because this file does not live under that tree on a runner.
    """
    # Arrange
    this_file = Path(__file__).resolve()
    # Act
    repo_root = _verified_repo_root()
    # Assert
    assert this_file.is_relative_to(repo_root), (
        f"the gate would audit {repo_root}, which does NOT contain the test "
        f"file {this_file} — that is a wrong-tree audit, the exact defect "
        f"this module exists to prevent."
    )


def test_audit_all_clean(scitex_dev_audit):
    # Arrange — name the tree under test; never let scitex-dev guess one.
    repo_root = _verified_repo_root()
    # Act — RAISES AssertionError (carrying the offending rule lines) on any
    # error-severity violation. That raise is the real gate; the documented
    # return contract is `-> None`.
    result = scitex_dev_audit(_DISTRIBUTION, path=repo_root)
    # Assert — a falsifiable check of that contract. (The previous
    # `assert result is None or ... or result is not Ellipsis` was a
    # TAUTOLOGY: the trailing clause is true for every value, so the
    # assert could never fail whatever the audit found.)
    assert result is None
