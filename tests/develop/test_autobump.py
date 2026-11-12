"""`.github/ci/autobump.py` is the write-path of the merge->release sweep.

A hand repair leaves no detector; the sweep's whole safety rests on this helper
being SURGICAL (touch only the [project] version line) and FAIL-LOUD (never emit
an inconsistent pyproject/CHANGELOG that would become a ghost tag). So the
detector ships with it, and the negative cases feed the exact bad input the
sweep must refuse — a mutation there goes RED here.

Loaded by file path (autobump.py is a CI helper, not an importable package),
mirroring tests/develop/test_git_hooks.py's loader for scripts/.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

import pytest

_REPO = Path(__file__).resolve().parents[2]

_UNRELEASED = "## [Unreleased]"

_PYPROJECT_FIXTURE = """\
[build-system]
requires = ["hatchling<1.28"]
build-backend = "hatchling.build"

[project]
name = "scitex-agent-container"
version = "0.24.1"
requires-python = ">=3.10"
dependencies = [
    "click>=8.0",
    "pyyaml>=6.0",
]

[tool.ruff]
target-version = "py310"
"""

_CHANGELOG_FIXTURE = """\
# Changelog

## [Unreleased]

### Fixed

- something pending

## [0.24.1] - 2026-07-20

### Fixed

- prior release
"""


@pytest.fixture(scope="module")
def autobump() -> ModuleType:
    path = _REPO / ".github" / "ci" / "autobump.py"
    assert path.exists(), f"autobump helper missing at {path}"
    spec = importlib.util.spec_from_file_location("autobump", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    (tmp_path / "pyproject.toml").write_text(_PYPROJECT_FIXTURE, encoding="utf-8")
    (tmp_path / "CHANGELOG.md").write_text(_CHANGELOG_FIXTURE, encoding="utf-8")
    return tmp_path


# --------------------------------------------------------------------------
# version arithmetic
# --------------------------------------------------------------------------
def test_read_current_version_returns_project_version(autobump, repo):
    # Arrange: fixture writes pyproject with version 0.24.1
    expected = "0.24.1"
    # Act
    got = autobump.read_current_version(repo)
    # Assert
    assert got == expected


def test_compute_next_patch_increments_patch_component(autobump):
    # Arrange
    current = "0.24.1"
    # Act
    got = autobump.compute_next_patch(current)
    # Assert
    assert got == "0.24.2"


def test_compute_next_patch_carries_into_double_digits(autobump):
    # Arrange
    current = "1.0.9"
    # Act
    got = autobump.compute_next_patch(current)
    # Assert
    assert got == "1.0.10"


def test_compute_next_patch_rejects_non_semver_string(autobump):
    # Arrange
    dirty = "0.24.1.dev3+gdead"
    # Act
    # Assert
    with pytest.raises(autobump.AutobumpError):
        autobump.compute_next_patch(dirty)


# --------------------------------------------------------------------------
# bump is SURGICAL: only the [project] version line moves
# --------------------------------------------------------------------------
def test_bump_returns_the_next_patch_version(autobump, repo):
    # Arrange: pyproject at 0.24.1
    # Act
    new = autobump.do_bump(repo, date="2026-07-21")
    # Assert
    assert new == "0.24.2"


def test_bump_rewrites_the_project_version_line(autobump, repo):
    # Arrange
    autobump.do_bump(repo, date="2026-07-21")
    # Act
    py = (repo / "pyproject.toml").read_text(encoding="utf-8")
    # Assert
    assert 'version = "0.24.2"' in py


def test_bump_leaves_no_stale_old_version(autobump, repo):
    # Arrange
    autobump.do_bump(repo, date="2026-07-21")
    # Act
    py = (repo / "pyproject.toml").read_text(encoding="utf-8")
    # Assert
    assert 'version = "0.24.1"' not in py


def test_bump_preserves_indented_dependency_constraints(autobump, repo):
    # Arrange: the col-0 anchor must never hit an indented dep line
    autobump.do_bump(repo, date="2026-07-21")
    # Act
    py = (repo / "pyproject.toml").read_text(encoding="utf-8")
    # Assert
    assert '"click>=8.0"' in py


def test_bump_preserves_ruff_target_version(autobump, repo):
    # Arrange: target-version must not be mistaken for the project version
    autobump.do_bump(repo, date="2026-07-21")
    # Act
    py = (repo / "pyproject.toml").read_text(encoding="utf-8")
    # Assert
    assert 'target-version = "py310"' in py


def test_bump_creates_dated_released_section(autobump, repo):
    # Arrange
    autobump.do_bump(repo, date="2026-07-21")
    # Act
    cl = (repo / "CHANGELOG.md").read_text(encoding="utf-8")
    # Assert
    assert "## [0.24.2] - 2026-07-21" in cl


def test_bump_keeps_empty_unreleased_on_top(autobump, repo):
    # Arrange
    autobump.do_bump(repo, date="2026-07-21")
    # Act
    cl = (repo / "CHANGELOG.md").read_text(encoding="utf-8")
    # Assert
    assert cl.index(_UNRELEASED) < cl.index("## [0.24.2]")


def test_bump_moves_unreleased_body_into_release(autobump, repo):
    # Arrange
    autobump.do_bump(repo, date="2026-07-21")
    # Act
    cl = (repo / "CHANGELOG.md").read_text(encoding="utf-8")
    # Assert
    assert cl.index("## [0.24.2]") < cl.index("- something pending")


def test_bump_orders_new_release_above_prior(autobump, repo):
    # Arrange
    autobump.do_bump(repo, date="2026-07-21")
    # Act
    cl = (repo / "CHANGELOG.md").read_text(encoding="utf-8")
    # Assert
    assert cl.index("## [0.24.2]") < cl.index("## [0.24.1]")


def test_promote_changelog_refuses_duplicate_version(autobump, repo):
    # Arrange: 0.24.2 already promoted once
    autobump.do_bump(repo, date="2026-07-21")
    # Act
    # Assert: re-promoting the same version must fail loud, not double-write
    with pytest.raises(autobump.AutobumpError):
        autobump.promote_changelog(repo, "0.24.2", date="2026-07-21")


def test_bump_fails_without_unreleased_heading(autobump, repo):
    # Arrange: a CHANGELOG with no [Unreleased] section
    (repo / "CHANGELOG.md").write_text("# Changelog\n\n## [0.24.1]\n", encoding="utf-8")
    # Act
    # Assert
    with pytest.raises(autobump.AutobumpError):
        autobump.do_bump(repo, date="2026-07-21")


# --------------------------------------------------------------------------
# verify: the pre-tag consistency gate — MUTATION-PROVE it goes red
# --------------------------------------------------------------------------
def test_verify_passes_on_consistent_tree(autobump, repo):
    # Arrange: fixture is consistent at 0.24.1
    # Act
    problems = autobump.verify_consistency(repo, "0.24.1")
    # Assert
    assert problems == []


def test_verify_flags_pyproject_version_disagreement(autobump, repo):
    # Arrange: assert tag 0.24.2 against a tree still at 0.24.1 (ghost-at-birth)
    # Act
    problems = autobump.verify_consistency(repo, "0.24.2")
    # Assert
    assert any("pyproject" in p for p in problems)


def test_verify_flags_missing_changelog_section(autobump, repo):
    # Arrange: pyproject bumped but CHANGELOG promotion never happened
    autobump.rewrite_pyproject_version(repo, "0.24.2")
    # Act
    problems = autobump.verify_consistency(repo, "0.24.2")
    # Assert
    assert any("CHANGELOG" in p and "0.24.2" in p for p in problems)


def test_verify_flags_missing_version_line(autobump, tmp_path):
    # Arrange: a pyproject with no [project] version line at all
    (tmp_path / "CHANGELOG.md").write_text(_CHANGELOG_FIXTURE, encoding="utf-8")
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "x"\n', encoding="utf-8"
    )
    # Act
    problems = autobump.verify_consistency(tmp_path, "0.24.1")
    # Assert
    assert problems


# --------------------------------------------------------------------------
# CLI surface the workflow actually calls
# --------------------------------------------------------------------------
def test_cli_verify_returns_zero_when_consistent(autobump, repo):
    # Arrange
    argv = ["--root", str(repo), "verify", "--version", "0.24.1"]
    # Act
    rc = autobump.main(argv)
    # Assert
    assert rc == 0


def test_cli_verify_tolerates_leading_v_prefix(autobump, repo):
    # Arrange: the sweep passes tags like v0.24.1
    argv = ["--root", str(repo), "verify", "--version", "v0.24.1"]
    # Act
    rc = autobump.main(argv)
    # Assert
    assert rc == 0


def test_cli_verify_returns_three_on_mismatch(autobump, repo):
    # Arrange
    argv = ["--root", str(repo), "verify", "--version", "0.24.2"]
    # Act
    rc = autobump.main(argv)
    # Assert
    assert rc == autobump.EXIT_INCONSISTENT


def test_cli_bump_prints_new_version_to_stdout(autobump, repo, capsys):
    # Arrange
    argv = ["--root", str(repo), "bump", "--date", "2026-07-21"]
    # Act
    autobump.main(argv)
    out = capsys.readouterr().out.strip()
    # Assert
    assert out == "0.24.2"


# Execute only bounded sections of the trusted workflow with inert external
# command fixtures. No GitHub, tag, release, store or operator git calls occur.
_GH_FIXTURE = """#!/usr/bin/env python3
import json, os, sys
with open(os.environ['CALLS'], 'a') as stream:
    stream.write(json.dumps(sys.argv[1:]) + '\\n')
args = sys.argv[1:]
if args[:2] == ['pr', 'list']:
    print(os.environ['OPEN_PR'])
    sys.exit(int(os.environ['LOOKUP_EXIT']))
if args[:2] == ['pr', 'create']:
    print('https://example.invalid/fixture/pull/17')
elif args[:2] == ['pr', 'merge']:
    sys.exit(int(os.environ['MERGE_EXIT']))
elif args[:2] == ['pr', 'view']:
    print(os.environ['PR_STATE'])
elif args[:2] == ['workflow', 'run']:
    sys.exit(int(os.environ['DISPATCH_EXIT']))
"""


def _workflow_probe(tmp_path, section, **settings):
    import json
    import os
    import subprocess

    import yaml

    workflow = yaml.safe_load(
        (_REPO / ".github/workflows/autobump-release-sweep.yaml").read_text()
    )
    code = next(
        s["run"]
        for s in workflow["jobs"]["sweep"]["steps"]
        if 'BRANCH="autobump/release-v$NEXT"' in s.get("run", "")
    )
    starts = {
        "pending": 'BRANCH="autobump/release-v$NEXT"',
        "merge": "PR=$(gh pr create",
    }
    ends = {"pending": "# Mutate pyproject + CHANGELOG", "merge": "M=$(gh pr view"}
    body = code.split(starts[section], 1)[1].split(ends[section], 1)[0]
    body = starts[section] + body
    commands = tmp_path / "bin"
    commands.mkdir()
    for name, content in [
        ("gh", _GH_FIXTURE),
        (
            "git",
            '#!/usr/bin/env python3\nimport json, os, sys\nwith open(os.environ["CALLS"], "a") as stream:\n    stream.write(json.dumps(["git", *sys.argv[1:]]) + "\\n")\n',
        ),
    ]:
        path = commands / name
        path.write_text(content)
        path.chmod(0o755)
    calls = tmp_path / "calls.jsonl"
    env = dict(
        os.environ,
        PATH=f"{commands}:{os.environ['PATH']}",
        CALLS=str(calls),
        OPEN_PR="",
        LOOKUP_EXIT="0",
        MERGE_EXIT="0",
        PR_STATE="MERGED",
        DISPATCH_EXIT="0",
    )
    env.update(settings)
    prefix = (
        "set -uo pipefail\nREPO=owned/fixture\nNEXT=0.0.1\n"
        "BRANCH=autobump/release-v0.0.1\n"
        'log() { echo "$*"; }\nalarm() { echo "$*" >&2; }\n'
    )
    result = subprocess.run(
        ["bash", "-c", prefix + body + "\necho TAG_ALLOWED\n"],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=20,
    )
    invoked = [json.loads(line) for line in calls.read_text().splitlines()]
    invoked = [call for call in invoked if call[:2] != ["git", "config"]]
    return result.returncode, "TAG_ALLOWED" in result.stdout, invoked


@pytest.mark.parametrize("lookup_exit", ["0", "1"])
def test_pending_bump_is_preserved_without_deleting_or_releasing(tmp_path, lookup_exit):
    # Arrange
    expected = (0 if lookup_exit == "0" else 1, False, ["pr", "list"])
    # Act
    code, continued, calls = _workflow_probe(
        tmp_path, "pending", OPEN_PR="17", LOOKUP_EXIT=lookup_exit
    )
    # Assert
    assert (code, continued, calls[0][:2], len(calls)) == (*expected, 1)


@pytest.mark.parametrize("merge_exit", ["0", "1"])
@pytest.mark.parametrize(
    "state,allowed,code",
    [("MERGED", True, 0), ("OPEN", False, 0), ("CLOSED", False, 1), ("", False, 1)],
)
def test_tagging_requires_observed_merge_even_when_command_succeeds(
    tmp_path, merge_exit, state, allowed, code
):
    # Arrange
    expected = (code, allowed, False, True)
    # Act
    actual_code, continued, calls = _workflow_probe(
        tmp_path, "merge", MERGE_EXIT=merge_exit, PR_STATE=state
    )
    # Assert
    assert (
        actual_code,
        continued,
        any("--admin" in c for c in calls),
        any(c[:2] == ["workflow", "run"] for c in calls),
    ) == expected


def test_initial_ci_dispatch_failure_never_attempts_merge_or_tag(tmp_path):
    # Arrange
    expected = (1, False, False)
    # Act
    code, continued, calls = _workflow_probe(tmp_path, "merge", DISPATCH_EXIT="1")
    # Assert
    assert (code, continued, any(c[:2] == ["pr", "merge"] for c in calls)) == expected


def test_no_pending_pr_can_reach_orphan_branch_cleanup(tmp_path):
    # Arrange
    expected = (0, True, True)
    # Act
    code, continued, calls = _workflow_probe(tmp_path, "pending")
    # Assert
    assert (
        code,
        continued,
        any(c[:3] == ["git", "push", "origin"] and "--delete" in c for c in calls),
    ) == expected
