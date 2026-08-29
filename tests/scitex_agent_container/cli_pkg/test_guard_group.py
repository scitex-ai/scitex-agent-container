"""``sac guard deletions`` — the surface any hook, shell or agent calls.

The exit code is the contract here, because a hook reads the code and not
the prose. So every verdict is asserted through the CLI, and the
``could-not-determine`` case is asserted BOTH to be non-zero and to not
say ``clean`` — a guard that exits non-zero while printing a pass is still
a guard that lied.
"""

from __future__ import annotations

import json
import subprocess
import traceback
from pathlib import Path

import pytest
from click.testing import CliRunner

from scitex_agent_container.cli_pkg._main import main

TRANSFORMS_BEFORE = '''\
"""Transform helpers imported by pipeline.py."""


class Scaler:
    def apply(self, values):
        return values


class Normalizer:
    def apply(self, values):
        return values


def identity(values):
    return list(values)
'''

TRANSFORMS_INCIDENT = '''\
"""Transform helpers imported by pipeline.py."""


def identity(values):
    return list(values)


def clip(values, lo, hi):
    return [min(max(v, lo), hi) for v in values]
'''


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True
    )


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A committed repo whose HEAD is the baseline."""
    root = tmp_path / "repo"
    root.mkdir()
    subprocess.run(
        ["git", "init", "-q", str(root)], check=True, capture_output=True
    )
    _git(root, "config", "user.email", "guard-test@example.com")
    _git(root, "config", "user.name", "guard test")
    (root / "transforms.py").write_text(TRANSFORMS_BEFORE)
    (root / "pipeline.py").write_text(
        "from transforms import Normalizer, Scaler  # noqa: F401\n"
    )
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "base")
    return root


@pytest.fixture
def incident_repo(repo: Path) -> Path:
    """The historical failure: a function added, two classes gone."""
    (repo / "transforms.py").write_text(TRANSFORMS_INCIDENT)
    return repo


def _run(*args: str):
    return CliRunner().invoke(main, ["guard", "deletions", *args])


def _run_json(*args: str) -> dict:
    """Parse the command's JSON stdout, or fail with everything the runner saw.

    TWO defects, both measured on click 8.4.2 on 2026-08-20.

    ONE — the wrong stream. ``result.output`` INTERLEAVES stdout and stderr.
    Measured with a positive control (write to ``sys.stderr`` at call time, so
    it reaches the stream CliRunner actually swapped in)::

        .output -> '{"ok": true}\nSTDERR-MARKER\n'   json.loads FAILS
        .stdout -> '{"ok": true}\n'                  json.loads OK

    A control matters here because the obvious experiment does NOT work: a
    logging handler created at import time holds the PRE-SWAP stderr, so its
    output never enters the capture and ``.output`` looks stdout-clean. That
    run cannot fail for any input.

    The CLI's contract is JSON *on stdout*; any log line from any module on
    stderr is not part of it. So parse ``.stdout``.

    TWO — the silent report. When parsing did fail on develop, the entire
    diagnostic was::

        json.decoder.JSONDecodeError: Expecting value: line 1 column 1 (char 0)

    `char 0` does NOT distinguish "stdout was empty" from "something non-JSON
    came first" — both land there. So the message below prints all three
    streams, the exit code, and the swallowed traceback, and lets the reader
    tell those apart instead of guessing.

    This does NOT tolerate the failure: an unparseable payload still fails the
    test. It only makes the failure say something.
    """
    result = _run(*args)
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        try:
            stderr = result.stderr
        except (ValueError, AttributeError):  # stx-allow: fallback (reason: click does not always capture stderr separately; a missing stderr must not replace the real failure with an error about reading it. SINK: the assertion message below, which pytest prints on failure)
            stderr = "<not captured separately>"
        detail = [
            "guard deletions --json produced no parseable JSON on stdout.",
            f"  json error : {exc}",
            f"  exit_code  : {result.exit_code}",
            f"  stdout     : {result.output!r}",
            f"  stderr     : {stderr!r}",
            f"  exception  : {result.exception!r}",
        ]
        if result.exc_info is not None:
            detail.append(
                "  traceback  :\n"
                + "".join(traceback.format_exception(*result.exc_info))
            )
        raise AssertionError("\n".join(detail)) from exc


def test_incident_exits_with_the_violations_code(incident_repo: Path) -> None:
    """3 — the declared domain code, never 1."""
    # Arrange
    args = ("--repo", str(incident_repo), "--base", "HEAD")
    # Act
    result = _run(*args)
    # Assert
    assert result.exit_code == 3


def test_incident_output_names_the_deleted_class(incident_repo: Path) -> None:
    """Naming the symbol is the difference between a report and a shrug."""
    # Arrange
    args = ("--repo", str(incident_repo), "--base", "HEAD")
    # Act
    result = _run(*args)
    # Assert
    assert "class:Scaler" in result.output


def test_incident_output_names_the_file_and_lines(incident_repo: Path) -> None:
    """'transforms.py:4-6' points at where the code used to be."""
    # Arrange
    args = ("--repo", str(incident_repo), "--base", "HEAD")
    # Act
    result = _run(*args)
    # Assert
    assert "transforms.py:4-6" in result.output


def test_human_view_folds_methods_into_their_class(
    incident_repo: Path,
) -> None:
    """A deleted class implies its methods; twenty lines bury the next find."""
    # Arrange
    args = ("--repo", str(incident_repo), "--base", "HEAD")
    # Act
    result = _run(*args)
    # Assert
    assert "(+1 method)" in result.output


def test_json_still_lists_every_deleted_method(incident_repo: Path) -> None:
    """The human view tidies; the machine view keeps full fidelity."""
    # Arrange
    args = ("--repo", str(incident_repo), "--base", "HEAD", "--json")
    # Act
    payload = _run_json(*args)
    # Assert
    assert "transforms.py::class:Scaler.apply" in {
        d["key"] for d in payload["deletions"]
    }


def test_incident_output_says_what_to_do_next(incident_repo: Path) -> None:
    """An error that only states what broke is half-written."""
    # Arrange
    args = ("--repo", str(incident_repo), "--base", "HEAD")
    # Act
    result = _run(*args)
    # Assert
    assert "what to do next:" in result.output


def test_clean_change_exits_zero(repo: Path) -> None:
    """A multi-file feature that deletes nothing passes."""
    # Arrange
    (repo / "transforms.py").write_text(
        TRANSFORMS_BEFORE + "\n\ndef clip(values):\n    return values\n"
    )
    (repo / "helpers.py").write_text("def widen(values):\n    return values\n")
    # Act
    result = _run("--repo", str(repo), "--base", "HEAD")
    # Assert
    assert result.exit_code == 0


def test_clean_change_says_ok(repo: Path) -> None:
    """The human line a reviewer skims."""
    # Arrange
    (repo / "helpers.py").write_text("def widen(values):\n    return values\n")
    # Act
    result = _run("--repo", str(repo), "--base", "HEAD")
    # Assert
    assert "OK" in result.output


def test_allowed_deletion_exits_zero(incident_repo: Path) -> None:
    """A deletion the task required, declared with --allow, is not a fail."""
    # Arrange
    keys = ("transforms.py::class:Scaler", "transforms.py::class:Normalizer")
    args = ["--repo", str(incident_repo), "--base", "HEAD"]
    for key in keys:
        args += ["--allow", key]
    # Act
    result = _run(*args)
    # Assert
    assert result.exit_code == 0


def test_missing_baseline_exits_undetermined(repo: Path) -> None:
    """4 — distinct from both 0 and 3, so a hook can tell them apart."""
    # Arrange
    args = ("--repo", str(repo))
    # Act
    result = _run(*args)
    # Assert
    assert result.exit_code == 4


def test_missing_baseline_is_not_printed_as_clean(repo: Path) -> None:
    """THE point of the guard: 'cannot tell' must not read as a pass."""
    # Arrange
    args = ("--repo", str(repo))
    # Act
    result = _run(*args)
    # Assert
    assert "CANNOT TELL" in result.output


def test_missing_baseline_never_says_ok(repo: Path) -> None:
    """The word reserved for a proven-clean tree stays reserved."""
    # Arrange
    args = ("--repo", str(repo))
    # Act
    result = _run(*args)
    # Assert
    assert "OK —" not in result.output


def test_missing_baseline_json_verdict_is_undetermined(repo: Path) -> None:
    """Machine readers get the same three-valued answer."""
    # Arrange
    args = ("--repo", str(repo), "--json")
    # Act
    payload = _run_json(*args)
    # Assert
    assert payload["verdict"] == "could-not-determine"


def test_unknown_ref_exits_undetermined(repo: Path) -> None:
    """A bad ref is an unreadable baseline, not an empty one."""
    # Arrange
    args = ("--repo", str(repo), "--base", "no-such-ref")
    # Act
    result = _run(*args)
    # Assert
    assert result.exit_code == 4


def test_json_verdict_is_violations_on_the_incident(
    incident_repo: Path,
) -> None:
    """The JSON verdict string is part of the contract."""
    # Arrange
    args = ("--repo", str(incident_repo), "--base", "HEAD", "--json")
    # Act
    payload = _run_json(*args)
    # Assert
    assert payload["verdict"] == "violations"


def test_json_carries_the_declared_exit_code(incident_repo: Path) -> None:
    """A caller reading JSON should not have to re-derive the code."""
    # Arrange
    args = ("--repo", str(incident_repo), "--base", "HEAD", "--json")
    # Act
    payload = _run_json(*args)
    # Assert
    assert payload["exit_code"] == 3


def test_json_top_level_keys_are_stable(incident_repo: Path) -> None:
    """Keys never disappear between verdicts — only their values empty."""
    # Arrange
    args = ("--repo", str(incident_repo), "--base", "HEAD", "--json")
    # Act
    payload = _run_json(*args)
    # Assert
    assert set(payload) == {
        "verdict", "exit_code", "baseline", "target", "files_compared",
        "deletions", "deleted_files", "broken_files", "allowed_deletions",
        "undetermined_reason", "next_steps",
    }


def test_json_deletion_entry_carries_its_allow_key(
    incident_repo: Path,
) -> None:
    """The printed key is exactly what --allow takes back."""
    # Arrange
    args = ("--repo", str(incident_repo), "--base", "HEAD", "--json")
    # Act
    payload = _run_json(*args)
    # Assert
    assert "transforms.py::class:Scaler" in {
        d["key"] for d in payload["deletions"]
    }


def test_snapshot_pair_mode_detects_the_incident(tmp_path: Path) -> None:
    """--before/--after works without git at all."""
    # Arrange
    before, after = tmp_path / "b", tmp_path / "a"
    before.mkdir()
    after.mkdir()
    (before / "transforms.py").write_text(TRANSFORMS_BEFORE)
    (after / "transforms.py").write_text(TRANSFORMS_INCIDENT)
    # Act
    result = _run("--before", str(before), "--after", str(after))
    # Assert
    assert result.exit_code == 3


def test_help_lists_the_three_verdicts() -> None:
    """The three-valued contract is documented where callers read it."""
    # Arrange
    args = ("--help",)
    # Act
    result = _run(*args)
    # Assert
    assert "could-not-determine" in result.output


def test_guard_group_is_registered_on_the_main_cli() -> None:
    """A gate nobody can reach is not a gate."""
    # Arrange
    runner = CliRunner()
    # Act
    result = runner.invoke(main, ["guard", "--help"])
    # Assert
    assert result.exit_code == 0
