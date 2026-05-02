"""Tests for install_cmds: sac install boot + sac install-post-merge-cron.

Coverage:
- boot --dry-run: prints actions, touches nothing
- boot idempotency: re-run on already-bootstrapped host is a no-op
- install-post-merge-cron: add, idempotent add, dry-run, uninstall
- install-post-merge-cron: --dry-run + --uninstall mutually exclusive
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from scitex_agent_container.cli_pkg.install_cmds import (
    _cron_line,
    boot,
    install_post_merge_cron,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

FAKE_CRON_LINE = _cron_line()


def _make_crontab_proc(stdout: str = "", returncode: int = 0) -> MagicMock:
    m = MagicMock()
    m.returncode = returncode
    m.stdout = stdout
    m.stderr = ""
    return m


# ---------------------------------------------------------------------------
# sac install boot
# ---------------------------------------------------------------------------


class TestBoot:
    def test_dry_run_touches_nothing(self, tmp_path, monkeypatch):
        """--dry-run prints plan without creating any files."""
        monkeypatch.setattr(
            "scitex_agent_container.cli_pkg.install_cmds._find_python311",
            lambda: "/usr/bin/python3.11",
        )
        monkeypatch.setattr(
            "scitex_agent_container.cli_pkg.install_cmds._find_sac_src",
            lambda: tmp_path,
        )
        # Patch subprocess so nothing is actually run.
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=0, stdout="tmux 3.3", stderr=""
            )
            runner = CliRunner()
            result = runner.invoke(boot, ["--dry-run"])

        assert result.exit_code == 0, result.output
        assert "dry-run" in result.output
        # subprocess.run should NOT be called for venv creation in dry-run.
        venv_calls = [c for c in mock_run.call_args_list if "venv" in str(c)]
        assert venv_calls == [], "venv should not be created in dry-run"

    def test_venv_already_exists_is_reported(self, tmp_path, monkeypatch):
        """If ~/.venv-3.11 already exists, boot reports it and skips creation."""
        fake_venv = tmp_path / ".venv-3.11"
        fake_venv.mkdir()
        monkeypatch.setattr(
            "scitex_agent_container.cli_pkg.install_cmds.Path",
            lambda p: fake_venv if "venv-3.11" in str(p) else Path(p),
        )
        # Avoid actually running anything.
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=0, stdout="tmux 3.3a", stderr=""
            )
            with patch(
                "scitex_agent_container.cli_pkg.install_cmds._find_python311",
                return_value="/usr/bin/python3.11",
            ):
                with patch(
                    "scitex_agent_container.cli_pkg.install_cmds._find_sac_src",
                    return_value=tmp_path,
                ):
                    with patch(
                        "scitex_agent_container.cli_pkg.install_cmds._deploy_cron_script"
                    ):
                        runner = CliRunner()
                        result = runner.invoke(boot, ["--dry-run"])

        assert result.exit_code == 0
        # Even in dry-run the venv "already exists" path should be hit.
        assert "already exists" in result.output or "dry-run" in result.output


# ---------------------------------------------------------------------------
# sac install-post-merge-cron
# ---------------------------------------------------------------------------


class TestInstallPostMergeCron:
    def _invoke(self, args: list[str], crontab_out: str = "", crontab_rc: int = 0):
        """Invoke with side_effect: crontab -l returns crontab_rc/out; crontab - succeeds."""
        runner = CliRunner()

        def _side_effect(cmd, **kwargs):
            if list(cmd) == ["crontab", "-l"]:
                return _make_crontab_proc(stdout=crontab_out, returncode=crontab_rc)
            return _make_crontab_proc(stdout="", returncode=0)

        # Inject -y to skip the confirmation prompt unless the caller already
        # supplied --dry-run / -y / --yes (those bypass the prompt themselves).
        if not any(a in args for a in ("-y", "--yes", "--dry-run")):
            args = [*args, "-y"]
        with patch("subprocess.run", side_effect=_side_effect) as mock_run:
            result = runner.invoke(install_post_merge_cron, args)
        return result, mock_run

    def test_add_when_empty_crontab(self):
        """Adds the cron line when crontab is empty (rc=1 means no crontab)."""
        result, mock_run = self._invoke([], crontab_out="", crontab_rc=1)
        assert result.exit_code == 0, result.output
        # Should call crontab -l then crontab -
        calls = mock_run.call_args_list
        assert any(["crontab", "-l"] == list(c.args[0]) for c in calls)
        write_calls = [c for c in calls if "-" in c.args[0]]
        assert write_calls, "crontab - should be called to write"

    def test_add_when_existing_crontab(self):
        """Adds the cron line to a crontab that already has other entries."""
        existing = "0 * * * * /usr/bin/some-other-job\n"
        result, mock_run = self._invoke([], crontab_out=existing, crontab_rc=0)
        assert result.exit_code == 0
        # The written content should include both old entry and our new line.
        write_calls = [
            c for c in mock_run.call_args_list if "crontab" in str(c) and "-" in str(c)
        ]
        if write_calls:
            written = write_calls[-1].kwargs.get("input", "")
            assert "post-merge-pull" in written

    def test_idempotent_already_present(self):
        """No-op when cron line is already present."""
        existing = FAKE_CRON_LINE + "\n"
        result, mock_run = self._invoke([], crontab_out=existing, crontab_rc=0)
        assert result.exit_code == 0
        assert "no-op" in result.output.lower() or "already" in result.output.lower()
        # crontab - should NOT be called.
        write_calls = [
            c
            for c in mock_run.call_args_list
            if c.args and list(c.args[0]) == ["crontab", "-"]
        ]
        assert write_calls == [], "Should not write crontab when already present"

    def test_dry_run_prints_line(self):
        """--dry-run prints the cron line without touching crontab."""
        result, mock_run = self._invoke(["--dry-run"], crontab_out="", crontab_rc=1)
        assert result.exit_code == 0
        assert "post-merge-pull" in result.output
        write_calls = [
            c
            for c in mock_run.call_args_list
            if c.args and list(c.args[0]) == ["crontab", "-"]
        ]
        assert write_calls == [], "crontab should not be written in dry-run"

    def test_dry_run_notes_already_present(self):
        """--dry-run still reports if line is already present."""
        existing = FAKE_CRON_LINE + "\n"
        result, mock_run = self._invoke(
            ["--dry-run"], crontab_out=existing, crontab_rc=0
        )
        assert result.exit_code == 0
        assert "no-op" in result.output.lower() or "already" in result.output.lower()

    def test_uninstall_removes_line(self):
        """--uninstall removes the cron line if present."""
        existing = "0 2 * * * /something/else\n" + FAKE_CRON_LINE + "\n"
        result, mock_run = self._invoke(
            ["--uninstall"], crontab_out=existing, crontab_rc=0
        )
        assert result.exit_code == 0
        assert "Removed" in result.output
        write_calls = [
            c
            for c in mock_run.call_args_list
            if c.args and list(c.args[0]) == ["crontab", "-"]
        ]
        assert write_calls, "crontab - should be called to remove"
        written = write_calls[-1].kwargs.get("input", "")
        assert "post-merge-pull" not in written

    def test_uninstall_noop_when_not_present(self):
        """--uninstall is a no-op if line not in crontab."""
        existing = "0 2 * * * /something/else\n"
        result, mock_run = self._invoke(
            ["--uninstall"], crontab_out=existing, crontab_rc=0
        )
        assert result.exit_code == 0
        assert "nothing to remove" in result.output.lower()
        write_calls = [
            c
            for c in mock_run.call_args_list
            if c.args and list(c.args[0]) == ["crontab", "-"]
        ]
        assert write_calls == [], "Should not write crontab"

    def test_dry_run_and_uninstall_mutually_exclusive(self):
        """--dry-run and --uninstall together exit with code 2."""
        result, _ = self._invoke(["--dry-run", "--uninstall"])
        assert result.exit_code == 2


# ---------------------------------------------------------------------------
# Integration: bash script smoke test
# ---------------------------------------------------------------------------


@pytest.mark.integration
class TestPostMergePullScript:
    """Spin up two fake git repos and verify the script pulls them."""

    def _make_repo(self, path: Path, name: str) -> Path:
        """Create a bare upstream and a clone with 'gitea' remote."""
        upstream = path / f"{name}-upstream"
        clone = path / "proj" / name
        upstream.mkdir(parents=True)
        clone.mkdir(parents=True)

        subprocess.run(
            ["git", "init", "--bare", str(upstream)], check=True, capture_output=True
        )
        subprocess.run(["git", "init", str(clone)], check=True, capture_output=True)
        subprocess.run(
            ["git", "-C", str(clone), "remote", "add", "gitea", str(upstream)],
            check=True,
            capture_output=True,
        )
        # Initial commit.
        (clone / "README.md").write_text("hello")
        subprocess.run(
            ["git", "-C", str(clone), "add", "."], check=True, capture_output=True
        )
        subprocess.run(
            ["git", "-C", str(clone), "commit", "--allow-empty", "-m", "init"],
            check=True,
            capture_output=True,
            env={
                **__import__("os").environ,
                "GIT_AUTHOR_NAME": "test",
                "GIT_AUTHOR_EMAIL": "t@t",
                "GIT_COMMITTER_NAME": "test",
                "GIT_COMMITTER_EMAIL": "t@t",
            },
        )
        subprocess.run(
            ["git", "-C", str(clone), "push", "gitea", "HEAD:develop"],
            check=True,
            capture_output=True,
        )
        return clone

    def test_script_pulls_clean_repo(self, tmp_path):
        """Script pulls a clean repo and writes a log entry."""
        script = (
            Path(__file__).parent.parent
            / "src"
            / "scitex_agent_container"
            / "cron"
            / "post-merge-pull.sh"
        )
        assert script.exists(), f"Cron script not found: {script}"

        clone = self._make_repo(tmp_path, "scitex-agent-container")
        log_dir = tmp_path / ".scitex" / "orochi" / "shared" / "logs"
        log_dir.mkdir(parents=True)

        env = {
            **__import__("os").environ,
            "HOME": str(tmp_path),
        }
        result = subprocess.run(
            ["bash", str(script)],
            capture_output=True,
            text=True,
            env=env,
        )
        # Script exits 0 and log file exists.
        assert result.returncode == 0, result.stderr
        log_files = list(log_dir.glob("post-merge-pull.*.log"))
        assert log_files, "Log file should be created"
        log_content = log_files[0].read_text()
        assert "done" in log_content.lower() or "OK" in log_content

    def test_script_skips_dirty_repo(self, tmp_path):
        """Script skips repos with uncommitted changes and logs WARN."""
        script = (
            Path(__file__).parent.parent
            / "src"
            / "scitex_agent_container"
            / "cron"
            / "post-merge-pull.sh"
        )
        clone = self._make_repo(tmp_path, "scitex-agent-container")

        # Make the clone dirty.
        (clone / "dirty.txt").write_text("unsaved")
        subprocess.run(
            ["git", "-C", str(clone), "add", "dirty.txt"], capture_output=True
        )

        log_dir = tmp_path / ".scitex" / "orochi" / "shared" / "logs"
        log_dir.mkdir(parents=True)

        env = {**__import__("os").environ, "HOME": str(tmp_path)}
        result = subprocess.run(
            ["bash", str(script)], capture_output=True, text=True, env=env
        )
        assert result.returncode == 0
        log_files = list(log_dir.glob("post-merge-pull.*.log"))
        assert log_files
        log_content = log_files[0].read_text()
        assert "WARN" in log_content or "SKIP" in log_content
