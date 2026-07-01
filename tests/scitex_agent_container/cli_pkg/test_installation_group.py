"""Tests for installation_group: sac install-post-merge-cron + bash script.

No-mocks pattern (PA-306):
- A fake ``crontab`` binary is installed on PATH; production code calls
  the real ``subprocess.run(["crontab", ...])`` and finds the shim,
  which persists state on disk + logs every invocation as JSONL.
- The bash post-merge-pull.sh script runs against real git repos in
  tmp_path.

TestBoot was deleted: it patched ``_find_python311`` /
``_find_sac_src`` / ``subprocess.run`` chains. The boot flow is
exercised end-to-end by the container-build CI; the unit-level tests
were only verifying the mocks.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest
from click.testing import CliRunner

from scitex_agent_container.cli_pkg.installation_group import (
    _cron_line,
    _find_python311,
    _find_sac_src,
    boot,
    install_post_merge_cron,
)

FAKE_CRON_LINE = _cron_line()


# ---------------------------------------------------------------------------
# Fake `crontab` binary on PATH — real subprocess, real filesystem state.
# ---------------------------------------------------------------------------


_CRONTAB_SCRIPT = """#!/usr/bin/env python3
import json
import os
import sys

log_path = os.environ["CRONTAB_LOG_FILE"]
state_path = os.environ["CRONTAB_STATE_FILE"]
argv = sys.argv[1:]
entry = {"argv": argv}

if argv == ["-l"]:
    rc = int(os.environ.get("CRONTAB_LIST_RC", "0"))
    if os.path.exists(state_path):
        with open(state_path) as fh:
            sys.stdout.write(fh.read())
    if rc != 0:
        with open(log_path, "a") as fh:
            fh.write(json.dumps(entry) + "\\n")
        sys.exit(rc)
elif argv == ["-"]:
    stdin = sys.stdin.read()
    entry["stdin"] = stdin
    with open(state_path, "w") as fh:
        fh.write(stdin)

with open(log_path, "a") as fh:
    fh.write(json.dumps(entry) + "\\n")
sys.exit(0)
"""


class _CrontabShim:
    def __init__(self, state_file: Path, log_file: Path):
        self._state = state_file
        self._log = log_file

    def set_initial_crontab(self, content: str) -> None:
        self._state.write_text(content)

    def set_list_exit_code(self, rc: int) -> None:
        os.environ["CRONTAB_LIST_RC"] = str(rc)

    def invocations(self) -> list[dict]:
        if not self._log.exists():
            return []
        return [json.loads(line) for line in self._log.read_text().splitlines()]

    def current_crontab(self) -> str:
        return self._state.read_text() if self._state.exists() else ""

    def write_calls(self) -> list[dict]:
        return [inv for inv in self.invocations() if inv["argv"] == ["-"]]


@pytest.fixture
def crontab_shim(tmp_path: Path, env_save_restore) -> _CrontabShim:
    bin_dir = tmp_path / "crontab_bin"
    bin_dir.mkdir()
    state_file = tmp_path / "crontab.state"
    log_file = tmp_path / "crontab.log.jsonl"

    script = bin_dir / "crontab"
    script.write_text(_CRONTAB_SCRIPT)
    script.chmod(0o755)

    env_save_restore.set("PATH", f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}")
    env_save_restore.set("CRONTAB_LOG_FILE", str(log_file))
    env_save_restore.set("CRONTAB_STATE_FILE", str(state_file))
    env_save_restore.set("CRONTAB_LIST_RC", "0")

    return _CrontabShim(state_file, log_file)


# ---------------------------------------------------------------------------
# sac install-post-merge-cron
# ---------------------------------------------------------------------------


def _run_cron_cli(args: list[str]) -> "CliRunner.invoke":
    runner = CliRunner()
    # Inject -y to skip the confirmation prompt unless caller already gave it.
    if not any(a in args for a in ("-y", "--yes", "--dry-run")):
        args = [*args, "-y"]
    return runner.invoke(install_post_merge_cron, args)


def test_add_when_empty_crontab_writes_cron_line(crontab_shim):
    # Arrange
    crontab_shim.set_list_exit_code(1)  # rc=1 → no crontab
    # Act
    result = _run_cron_cli([])
    # Assert
    assert result.exit_code == 0


def test_add_when_empty_crontab_calls_crontab_dash(crontab_shim):
    # Arrange
    crontab_shim.set_list_exit_code(1)
    # Act
    _run_cron_cli([])
    # Assert
    assert crontab_shim.write_calls()


def test_add_to_existing_crontab_preserves_existing_entry(crontab_shim):
    # Arrange
    existing = "0 * * * * /usr/bin/some-other-job\n"
    crontab_shim.set_initial_crontab(existing)
    # Act
    _run_cron_cli([])
    # Assert
    assert "some-other-job" in crontab_shim.current_crontab()


def test_add_to_existing_crontab_appends_post_merge_line(crontab_shim):
    # Arrange
    crontab_shim.set_initial_crontab("0 * * * * /usr/bin/some-other-job\n")
    # Act
    _run_cron_cli([])
    # Assert
    assert "post-merge-pull" in crontab_shim.current_crontab()


def test_idempotent_add_does_not_rewrite_crontab(crontab_shim):
    # Arrange — initial crontab already has the line
    crontab_shim.set_initial_crontab(FAKE_CRON_LINE + "\n")
    # Act
    _run_cron_cli([])
    # Assert
    assert crontab_shim.write_calls() == []


def test_idempotent_add_emits_already_present_message(crontab_shim):
    # Arrange
    crontab_shim.set_initial_crontab(FAKE_CRON_LINE + "\n")
    # Act
    result = _run_cron_cli([])
    # Assert
    out = result.output.lower()
    assert "no-op" in out or "already" in out


def test_dry_run_does_not_write_crontab(crontab_shim):
    # Arrange
    crontab_shim.set_list_exit_code(1)
    # Act
    _run_cron_cli(["--dry-run"])
    # Assert
    assert crontab_shim.write_calls() == []


def test_dry_run_prints_post_merge_line(crontab_shim):
    # Arrange
    crontab_shim.set_list_exit_code(1)
    # Act
    result = _run_cron_cli(["--dry-run"])
    # Assert
    assert "post-merge-pull" in result.output


def test_dry_run_notes_already_present(crontab_shim):
    # Arrange
    crontab_shim.set_initial_crontab(FAKE_CRON_LINE + "\n")
    # Act
    result = _run_cron_cli(["--dry-run"])
    # Assert
    out = result.output.lower()
    assert "no-op" in out or "already" in out


def test_uninstall_removes_post_merge_line(crontab_shim):
    # Arrange
    crontab_shim.set_initial_crontab(
        "0 2 * * * /something/else\n" + FAKE_CRON_LINE + "\n"
    )
    # Act
    _run_cron_cli(["--uninstall"])
    # Assert
    assert "post-merge-pull" not in crontab_shim.current_crontab()


def test_uninstall_preserves_other_entries(crontab_shim):
    # Arrange
    crontab_shim.set_initial_crontab(
        "0 2 * * * /something/else\n" + FAKE_CRON_LINE + "\n"
    )
    # Act
    _run_cron_cli(["--uninstall"])
    # Assert
    assert "/something/else" in crontab_shim.current_crontab()


def test_uninstall_noop_when_line_absent(crontab_shim):
    # Arrange
    crontab_shim.set_initial_crontab("0 2 * * * /something/else\n")
    # Act
    _run_cron_cli(["--uninstall"])
    # Assert
    assert crontab_shim.write_calls() == []


def test_uninstall_emits_nothing_to_remove(crontab_shim):
    # Arrange
    crontab_shim.set_initial_crontab("0 2 * * * /something/else\n")
    # Act
    result = _run_cron_cli(["--uninstall"])
    # Assert
    assert "nothing to remove" in result.output.lower()


def test_dry_run_and_uninstall_together_exit_2(crontab_shim):
    # Arrange
    # Act
    result = _run_cron_cli(["--dry-run", "--uninstall"])
    # Assert
    assert result.exit_code == 2


# ---------------------------------------------------------------------------
# Confirmation and crontab error branches
# ---------------------------------------------------------------------------


def test_missing_yes_install_exits_two(crontab_shim):
    # Arrange
    runner = CliRunner()
    # Act
    result = runner.invoke(install_post_merge_cron, [])
    # Assert
    assert result.exit_code == 2


def test_missing_yes_install_emits_refusal(crontab_shim):
    # Arrange
    runner = CliRunner()
    # Act
    result = runner.invoke(install_post_merge_cron, [])
    # Assert
    assert "refusing" in result.output.lower()


def test_missing_yes_uninstall_mentions_remove(crontab_shim):
    # Arrange
    runner = CliRunner()
    # Act
    result = runner.invoke(install_post_merge_cron, ["--uninstall"])
    # Assert
    assert "remove" in result.output.lower()


def test_crontab_list_unexpected_rc_exits_one(crontab_shim):
    # Arrange
    crontab_shim.set_list_exit_code(2)
    # Act
    result = _run_cron_cli([])
    # Assert
    assert result.exit_code == 1


def test_crontab_write_failure_exits_one(tmp_path, env_save_restore, subprocess_shim):
    # Arrange
    subprocess_shim.install("crontab", exit=1, stderr="write boom")
    # Act
    result = _run_cron_cli([])
    # Assert
    assert result.exit_code == 1


# ---------------------------------------------------------------------------
# Helper functions: _find_python311, _find_sac_src
# ---------------------------------------------------------------------------


def test_find_python311_returns_path_when_available():
    # Arrange
    # Act
    result = _find_python311()
    # Assert
    assert result is not None


def test_find_python311_none_when_no_python_on_path(tmp_path, env_save_restore):
    # Arrange — PATH contains only an empty dir, no python at all
    empty = tmp_path / "empty_path"
    empty.mkdir()
    env_save_restore.set("PATH", str(empty))
    # Act
    result = _find_python311()
    # Assert
    assert result is None


def test_find_sac_src_returns_directory_with_pyproject():
    # Arrange
    # Act
    src_root = _find_sac_src()
    # Assert
    assert (src_root / "pyproject.toml").exists()


# ---------------------------------------------------------------------------
# sac install boot --dry-run (does not mutate host)
# ---------------------------------------------------------------------------


def test_boot_dry_run_exits_zero(subprocess_shim):
    # Arrange
    subprocess_shim.install("tmux", stdout="tmux 3.3a\n")
    runner = CliRunner()
    # Act
    result = runner.invoke(boot, ["--dry-run"])
    # Assert
    assert result.exit_code == 0


def test_boot_dry_run_announces_completion(subprocess_shim):
    # Arrange
    subprocess_shim.install("tmux", stdout="tmux 3.3a\n")
    runner = CliRunner()
    # Act
    result = runner.invoke(boot, ["--dry-run"])
    # Assert
    assert "dry-run complete" in result.output.lower()


def test_boot_dry_run_mentions_pip_install_plan(
    subprocess_shim, tmp_path, env_save_restore
):
    # Arrange — force venv-missing branch by pointing HOME at empty dir
    subprocess_shim.install("tmux", stdout="tmux 3.3a\n")
    env_save_restore.set("HOME", str(tmp_path))
    runner = CliRunner()
    # Act
    result = runner.invoke(boot, ["--dry-run"])
    # Assert
    assert "pip" in result.output.lower() or "install" in result.output.lower()


# ---------------------------------------------------------------------------
# Integration: bash post-merge-pull.sh script smoke test against real git.
# ---------------------------------------------------------------------------


_GIT_ENV = {
    "GIT_AUTHOR_NAME": "test",
    "GIT_AUTHOR_EMAIL": "t@t",
    "GIT_COMMITTER_NAME": "test",
    "GIT_COMMITTER_EMAIL": "t@t",
}


def _git(clone: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(clone), *args],
        check=True,
        capture_output=True,
        text=True,
        env={**os.environ, **_GIT_ENV},
    )


def _make_repo(path: Path, name: str, *, branch: str = "develop") -> Path:
    """Create a bare upstream on `develop` plus a clone tracking it.

    The clone is checked out on ``branch`` with origin/develop as the
    tracked upstream — exactly the fleet layout the cron script targets
    (remote-agnostic: the remote is named ``origin``, not ``gitea``).
    """
    upstream = path / f"{name}-upstream"
    clone = path / "proj" / name
    upstream.mkdir(parents=True)

    subprocess.run(
        ["git", "init", "--bare", "-b", "develop", str(upstream)],
        check=True,
        capture_output=True,
    )
    # Seed the upstream develop branch via a throwaway clone.
    seed = path / f"{name}-seed"
    subprocess.run(
        ["git", "clone", str(upstream), str(seed)], check=True, capture_output=True
    )
    _git(seed, "checkout", "-b", "develop")
    (seed / "README.md").write_text("hello")
    _git(seed, "add", ".")
    _git(seed, "commit", "-m", "init")
    _git(seed, "push", "-u", "origin", "develop")

    # The fleet checkout: origin = upstream, on develop tracking it.
    subprocess.run(
        ["git", "clone", str(upstream), str(clone)], check=True, capture_output=True
    )
    _git(clone, "checkout", "develop")
    if branch != "develop":
        _git(clone, "checkout", "-b", branch)
    return clone


def _advance_upstream(path: Path, name: str) -> None:
    """Push a new develop commit upstream so the clone can fast-forward."""
    upstream = path / f"{name}-upstream"
    seed = path / f"{name}-seed"
    (seed / "more.txt").write_text("more")
    _git(seed, "add", ".")
    _git(seed, "commit", "-m", "second")
    _git(seed, "push", "origin", "develop")
    _ = upstream  # upstream already wired via origin


def _post_merge_pull_script() -> Path:
    return (
        Path(__file__).parents[3]
        / "src"
        / "scitex_agent_container"
        / "cron"
        / "post-merge-pull.sh"
    )


@pytest.fixture
def post_merge_pull_script() -> Path:
    script = _post_merge_pull_script()
    if not script.exists():
        pytest.skip(f"Cron script not found: {script}")
    return script


def _run_script(script: Path, home: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", str(script)],
        capture_output=True,
        text=True,
        env={**os.environ, "HOME": str(home)},
    )


def _log_text(home: Path) -> str:
    log_dir = home / ".scitex" / "agent-container" / "runtime" / "logs"
    return "\n".join(p.read_text() for p in log_dir.glob("post-merge-pull.*.log"))


_REPO = "scitex-agent-container"


@pytest.mark.integration
class TestPostMergePullScript:
    """Real bash + real git — no mocks anywhere."""

    # -- clean develop checkout: fast-forwards from tracked upstream --------

    @pytest.fixture
    def clean_develop_run(self, tmp_path, post_merge_pull_script):
        clone = _make_repo(tmp_path, _REPO)
        before = _git(clone, "rev-parse", "HEAD").stdout.strip()
        _advance_upstream(tmp_path, _REPO)
        result = _run_script(post_merge_pull_script, tmp_path)
        after = _git(clone, "rev-parse", "HEAD").stdout.strip()
        return result, before, after

    def test_clean_develop_returns_zero(self, clean_develop_run):
        # Arrange
        # Act
        result, _before, _after = clean_develop_run
        # Assert
        assert result.returncode == 0

    def test_clean_develop_fast_forwards_to_upstream(self, clean_develop_run):
        # Arrange
        # Act
        _result, before, after = clean_develop_run
        # Assert
        assert after != before

    # -- feature-branch checkout: never touched ----------------------------

    @pytest.fixture
    def feature_branch_run(self, tmp_path, post_merge_pull_script):
        clone = _make_repo(tmp_path, _REPO, branch="feat/wip")
        before = _git(clone, "rev-parse", "HEAD").stdout.strip()
        _advance_upstream(tmp_path, _REPO)
        result = _run_script(post_merge_pull_script, tmp_path)
        after = _git(clone, "rev-parse", "HEAD").stdout.strip()
        return result, before, after, _log_text(tmp_path)

    def test_feature_branch_head_unchanged(self, feature_branch_run):
        # Arrange
        # Act
        _result, before, after, _log = feature_branch_run
        # Assert
        assert after == before

    def test_feature_branch_logs_skip(self, feature_branch_run):
        # Arrange
        # Act
        _result, _before, _after, log = feature_branch_run
        # Assert
        assert "not 'develop'" in log

    # -- dirty develop checkout: skipped with a warning --------------------

    @pytest.fixture
    def dirty_develop_run(self, tmp_path, post_merge_pull_script):
        clone = _make_repo(tmp_path, _REPO)
        before = _git(clone, "rev-parse", "HEAD").stdout.strip()
        (clone / "dirty.txt").write_text("unsaved")
        _git(clone, "add", "dirty.txt")
        _advance_upstream(tmp_path, _REPO)
        result = _run_script(post_merge_pull_script, tmp_path)
        after = _git(clone, "rev-parse", "HEAD").stdout.strip()
        return result, before, after, _log_text(tmp_path)

    def test_dirty_develop_head_unchanged(self, dirty_develop_run):
        # Arrange
        # Act
        _result, before, after, _log = dirty_develop_run
        # Assert
        assert after == before

    def test_dirty_develop_logs_uncommitted_warning(self, dirty_develop_run):
        # Arrange
        # Act
        _result, _before, _after, log = dirty_develop_run
        # Assert
        assert "uncommitted changes" in log
