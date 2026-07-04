"""Tests for ``sac agents start``'s ``-v``/``--verbose`` launch-plan gate.

feat/start-verbose-plan-gate: an interactive launch (real tty, no
``--yes``) always refuses to start, but the preview it prints before
refusing now has two variants:

  * default   → SHORT summary (``render_plan_summary``): identity,
                spec path, runtime/image, workdir + backed-by-bind
                check, model. No Mounts/Env/Skills/Hooks/... .
  * ``-v``    → FULL detail (``render_plan`` — the exact same content
                ``sac agents explain`` shows).

Drives the real ``run_single_targets`` (the per-target loop
``cli_pkg.lifecycle._start.start`` delegates to for a single target) with
a real minimal apptainer-runtime spec — ``build_run_argv`` is a pure
argv-renderer (no subprocess work) so this never needs a real apptainer
binary or SIF. The spec opts out of the default ``server:sac`` push
channel (``sac-builtin: off``) so it never needs a live ``sac listen``
bearer token either — unrelated to the plan-gate this file tests.

``Mounts (...)`` and ``Host deep-merge (...)`` are used as the
full-plan-only markers because ``render_plan`` prints both
UNCONDITIONALLY (unlike ``Settings sources``/``Hooks``, which are
skipped when the corresponding collection is empty) — so their presence
distinguishes full vs. summary regardless of the fixture's own content.

No mocks / no monkeypatch: ``sys.stdin.isatty()`` is the one condition an
automated harness can't otherwise produce (a real interactive operator
terminal), so we swap ``sys.stdin`` for a REAL pty slave file (``pty.
openpty()``) via a hand-rolled context manager — ``isatty()`` then
returns True because it genuinely IS a terminal device, not because a
return value was faked. The refusal message goes out through the
project's ``scitex_logging`` logger rather than stdout, so it's read
back via ``caplog`` (a real pytest log-capture fixture, not a mock).
"""

from __future__ import annotations

import logging
import os
import pty
import sys
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, NamedTuple

import pytest

from scitex_agent_container.cli_pkg.lifecycle._start import start
from scitex_agent_container.cli_pkg.lifecycle._start_single import run_single_targets

# Full-plan-only markers: printed UNCONDITIONALLY by render_plan, never by
# render_plan_summary.
_MOUNTS_MARKER = "Mounts (apptainer.binds"
_HOST_MERGE_MARKER = "Host deep-merge (~/.claude"


@contextmanager
def _real_tty_stdin() -> Iterator[None]:
    """Swap ``sys.stdin`` for a REAL pty slave — ``isatty()`` genuinely True."""
    master_fd, slave_fd = pty.openpty()
    saved_stdin = sys.stdin
    slave_file = os.fdopen(slave_fd, "r")
    sys.stdin = slave_file
    try:
        yield
    finally:
        sys.stdin = saved_stdin
        slave_file.close()
        os.close(master_fd)


def _write_local_spec(home: Path, name: str) -> Path:
    """Minimal project-scope apptainer spec — loads cleanly, never launches."""
    agents_dir = home / ".scitex" / "agent-container" / "agents" / name
    agents_dir.mkdir(parents=True)
    yaml_path = agents_dir / f"{name}.yaml"
    yaml_path.write_text(
        "apiVersion: scitex-agent-container/v3\n"
        "kind: Agent\n"
        # sac-builtin: off — opts out of the default server:sac push channel,
        # which otherwise requires a live `sac listen` bearer token file this
        # test has no need to materialise (unrelated to the plan-gate under
        # test).
        "metadata:\n  labels:\n    sac-builtin: \"off\"\n"
        "spec:\n"
        "  runtime: apptainer\n"
        "  host: local\n"
        "  workdir: /home/agent/work\n"
        "  apptainer:\n    image: /x.sif\n    binds: []\n"
        "  claude:\n    model: sonnet\n"
        "  health:\n    enabled: true\n    interval: 60\n"
        "  restart:\n    policy: on-failure\n    max_retries: 3\n"
        "  a2a:\n    port: null\n"
    )
    return yaml_path


class _Preview(NamedTuple):
    stdout: str
    log_text: str


def _run_interactive(yaml_path: Path, *, verbose: bool, capsys, caplog) -> _Preview:
    """Drive ``run_single_targets`` as an interactive operator (tty, no --yes).

    Always refuses (SystemExit(1)) — that refusal is unconditional and
    unaffected by ``verbose``; only the preview content differs.
    """
    caplog.set_level(logging.DEBUG)
    with _real_tty_stdin():
        with pytest.raises(SystemExit) as exc_info:
            run_single_targets(
                [str(yaml_path)],
                no_preflight=True,
                force=False,
                resume_id=None,
                session_mode=None,
                dry_run=False,
                as_json=False,
                foreground=False,
                one_shot=False,
                strict_drift=False,
                no_redispatch=True,
                multi_foreground=False,
                preflight_runner=lambda: None,
                yes=False,
                verbose=verbose,
            )
    assert exc_info.value.code == 1
    return _Preview(stdout=capsys.readouterr().out, log_text=caplog.text)


class TestVerbosePlanGateDefaultIsShort:
    def test_short_summary_omits_mounts_marker(
        self, tmp_path, env_save_restore, capsys, caplog
    ):
        # Arrange
        env_save_restore.set("HOME", str(tmp_path))
        yaml_path = _write_local_spec(tmp_path, "alpha")
        # Act
        out = _run_interactive(
            yaml_path, verbose=False, capsys=capsys, caplog=caplog
        ).stdout
        # Assert
        assert _MOUNTS_MARKER not in out

    def test_short_summary_omits_host_merge_marker(
        self, tmp_path, env_save_restore, capsys, caplog
    ):
        # Arrange
        env_save_restore.set("HOME", str(tmp_path))
        yaml_path = _write_local_spec(tmp_path, "alpha")
        # Act
        out = _run_interactive(
            yaml_path, verbose=False, capsys=capsys, caplog=caplog
        ).stdout
        # Assert
        assert _HOST_MERGE_MARKER not in out

    def test_short_summary_still_refuses_without_yes(
        self, tmp_path, env_save_restore, capsys, caplog
    ):
        # Arrange
        env_save_restore.set("HOME", str(tmp_path))
        yaml_path = _write_local_spec(tmp_path, "alpha")
        # Act
        log_text = _run_interactive(
            yaml_path, verbose=False, capsys=capsys, caplog=caplog
        ).log_text
        # Assert
        assert "without --yes/-y" in log_text

    def test_short_summary_shows_agent_identity(
        self, tmp_path, env_save_restore, capsys, caplog
    ):
        # Arrange
        env_save_restore.set("HOME", str(tmp_path))
        yaml_path = _write_local_spec(tmp_path, "alpha")
        # Act
        out = _run_interactive(
            yaml_path, verbose=False, capsys=capsys, caplog=caplog
        ).stdout
        # Assert
        assert "Agent: alpha" in out

    def test_short_summary_shows_workdir_line(
        self, tmp_path, env_save_restore, capsys, caplog
    ):
        # Arrange
        env_save_restore.set("HOME", str(tmp_path))
        yaml_path = _write_local_spec(tmp_path, "alpha")
        # Act
        out = _run_interactive(
            yaml_path, verbose=False, capsys=capsys, caplog=caplog
        ).stdout
        # Assert
        assert "Workdir (--pwd):" in out

    def test_short_summary_shows_model_line(
        self, tmp_path, env_save_restore, capsys, caplog
    ):
        # Arrange
        env_save_restore.set("HOME", str(tmp_path))
        yaml_path = _write_local_spec(tmp_path, "alpha")
        # Act
        out = _run_interactive(
            yaml_path, verbose=False, capsys=capsys, caplog=caplog
        ).stdout
        # Assert
        assert "Model:" in out


class TestVerbosePlanGateFlagShowsFullPlan:
    def test_verbose_shows_mounts_marker(
        self, tmp_path, env_save_restore, capsys, caplog
    ):
        # Arrange
        env_save_restore.set("HOME", str(tmp_path))
        yaml_path = _write_local_spec(tmp_path, "beta")
        # Act
        out = _run_interactive(
            yaml_path, verbose=True, capsys=capsys, caplog=caplog
        ).stdout
        # Assert
        assert _MOUNTS_MARKER in out

    def test_verbose_shows_host_merge_marker(
        self, tmp_path, env_save_restore, capsys, caplog
    ):
        # Arrange
        env_save_restore.set("HOME", str(tmp_path))
        yaml_path = _write_local_spec(tmp_path, "beta")
        # Act
        out = _run_interactive(
            yaml_path, verbose=True, capsys=capsys, caplog=caplog
        ).stdout
        # Assert
        assert _HOST_MERGE_MARKER in out

    def test_verbose_still_refuses_without_yes(
        self, tmp_path, env_save_restore, capsys, caplog
    ):
        # Arrange
        env_save_restore.set("HOME", str(tmp_path))
        yaml_path = _write_local_spec(tmp_path, "beta")
        # Act
        log_text = _run_interactive(
            yaml_path, verbose=True, capsys=capsys, caplog=caplog
        ).log_text
        # Assert
        assert "without --yes/-y" in log_text


def test_start_command_exposes_short_verbose_flag() -> None:
    # Arrange
    flag_names = {opt for p in start.params for opt in p.opts}
    # Act
    has_flag = "-v" in flag_names
    # Assert
    assert has_flag is True


def test_start_command_exposes_long_verbose_flag() -> None:
    # Arrange
    flag_names = {opt for p in start.params for opt in p.opts}
    # Act
    has_flag = "--verbose" in flag_names
    # Assert
    assert has_flag is True
