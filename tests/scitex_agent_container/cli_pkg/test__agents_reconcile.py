"""``sac agents reconcile`` — the CLI surface of the fleet enforcer.

Real ``CliRunner`` against the real click command; the engine underneath is
covered by ``tests/scitex_agent_container/_reconcile/``. These tests pin the
things only the CLI owns: that dry-run is the DEFAULT (the single most
important safety property of this verb — a wrong default here restarts the
fleet by accident), the flag contract, and the exit codes a cron reads.

The pass is redirected at a REAL empty tmp fleet registry via
``SCITEX_AGENT_CONTAINER_AGENTS_DIR`` so the command can never touch the
operator's live fleet from a test run. No mocks.

Each test: AAA markers (TQ002), one assertion (TQ007), 3+-word name (TQ003).
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from click.testing import CliRunner

from scitex_agent_container.cli_pkg._agents_reconcile import reconcile


@pytest.fixture(autouse=True)
def empty_fleet(tmp_path: Path):
    # Arrange — point the verb at an EMPTY tmp registry (env override, the
    # same seam `sac agents refresh-acl` honours) so no test can ever read
    # or restart the operator's real agents. Explicit save/restore.
    reg = tmp_path / "agents"
    reg.mkdir()
    keys = {
        "SCITEX_AGENT_CONTAINER_AGENTS_DIR": str(reg),
        "SCITEX_AGENT_CONTAINER_RUNTIME_DIR": str(tmp_path / "runtime"),
        "SCITEX_TODO_TASKS_YAML_SHARED": str(tmp_path / "tasks.yaml"),
    }
    saved = {k: os.environ.get(k) for k in keys}
    os.environ.update(keys)
    try:
        yield reg
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def _runner() -> CliRunner:
    """A runner for asserting on ``result.stdout``, never ``result.output``.

    MEASURED on click 8.4.2 (the version both this checkout and CI resolve):
    ``Result.output`` is stdout and stderr COMBINED; only ``Result.stdout``
    is stdout alone. That distinction is the contract under test, not a
    harness detail — the scheduled form is `sac agents reconcile --json` and
    its consumer parses a PIPE, where the two streams were never merged.

    This is what made the first version of these tests lie. scitex-todo is
    an OPTIONAL board, not a hard dependency, so where it is absent or its
    store is unwritable the heartbeat rail fails and prints a deliberately
    LOUD line on every pass. Locally the board was installed and writable,
    so nothing hit stderr and `result.output` happened to equal the JSON;
    in CI the board write failed, two stderr lines landed after the envelope
    in the combined stream, and `json.loads` died with "Extra data: line 8".
    The command was right both times — the test was asserting on the wrong
    stream and only passed because of a condition it never stated.

    ``mix_stderr=False`` requested defensively for click < 8.2, where it was
    the way to split the streams; it was REMOVED in 8.2+.
    """
    # stx-allow: fallback (reason: `mix_stderr` was removed in click 8.2; ask for it only on the older click that still accepts it, where it is what splits the streams)
    try:
        return CliRunner(mix_stderr=False)
    except TypeError:
        return CliRunner()


@pytest.fixture
def default_run() -> object:
    # Arrange + Act — the bare verb, no flags at all.
    return _runner().invoke(reconcile, [])


def test_bare_invocation_is_a_dry_run(default_run):
    # Arrange — THE safety property: no flags must never mutate the fleet.
    # Act
    # Assert
    assert "dry-run" in default_run.output


def test_bare_invocation_on_clean_fleet_exits_zero(default_run):
    # Arrange — an empty registry has nothing down.
    # Act
    # Assert
    assert default_run.exit_code == 0


def test_apply_and_dry_run_together_are_refused():
    # Arrange — contradictory flags must fail loud, never pick one silently.
    # Act
    result = _runner().invoke(reconcile, ["--apply", "--dry-run"])
    # Assert
    assert result.exit_code == 2


def test_apply_and_dry_run_conflict_explains_the_default():
    # Arrange
    # Act
    result = _runner().invoke(reconcile, ["--apply", "--dry-run"])
    # Assert
    assert "Dry-run is the DEFAULT" in result.stderr


def test_json_mode_emits_parseable_output():
    # Arrange — the scheduled form and any tooling read this.
    # Act
    result = _runner().invoke(reconcile, ["--json"])
    # Assert
    assert json.loads(result.stdout)["mode"] == "dry-run"


def test_json_mode_reports_the_exit_code():
    # Arrange — the envelope carries the verdict for a non-tty consumer.
    # Act
    result = _runner().invoke(reconcile, ["--json"])
    # Assert
    assert json.loads(result.stdout)["exit_code"] == 0


def test_json_mode_lists_agents():
    # Arrange — an empty fleet reports an empty list, not a missing key.
    # Act
    result = _runner().invoke(reconcile, ["--json"])
    # Assert
    assert json.loads(result.stdout)["agents"] == []


def test_json_stdout_carries_only_the_envelope():
    # Arrange — the diagnostics rail must never pollute the machine-readable
    # one. This is what a `sac agents reconcile --json | jq` consumer relies
    # on.
    # Act
    result = _runner().invoke(reconcile, ["--json"])
    # Assert — parses whole, with nothing appended.
    assert json.loads(result.stdout)


def test_json_survives_an_unwritable_board(tmp_path: Path, env_save_restore):
    # Arrange — THE CI CONDITION, pinned. scitex-todo is an OPTIONAL board,
    # so on a host without it (or with an unwritable store) the heartbeat
    # rail fails and prints a deliberately LOUD line on every pass. That
    # line must go to stderr and must NEVER end up appended to the JSON
    # envelope — which is precisely how these tests passed locally and broke
    # in CI. Made organic (a read-only dir), not injected.
    readonly = tmp_path / "readonly"
    readonly.mkdir()
    readonly.chmod(0o555)
    env_save_restore.set("SCITEX_TODO_TASKS_YAML_SHARED", str(readonly / "tasks.yaml"))
    try:
        # Act
        result = _runner().invoke(reconcile, ["--json"])
        # Assert — stdout is still exactly one parseable envelope.
        assert json.loads(result.stdout)["mode"] == "dry-run"
    finally:
        readonly.chmod(0o755)


def test_help_documents_the_dry_run_default():
    # Arrange — an operator must learn the default from --help, not by
    # restarting the fleet.
    # Act
    result = _runner().invoke(reconcile, ["--help"])
    # Assert
    assert "Dry-run by default" in result.output


def test_help_documents_the_apply_flag():
    # Arrange
    # Act
    result = _runner().invoke(reconcile, ["--help"])
    # Assert
    assert "--apply" in result.output


def test_limit_flag_is_accepted():
    # Arrange — the global per-pass cap must be operator-tunable.
    # Act
    result = _runner().invoke(reconcile, ["--limit", "3", "--json"])
    # Assert
    assert result.exit_code == 0


def test_verb_is_registered_on_the_agents_group():
    # Arrange — a verb nobody can invoke is a verb that does not exist.
    from scitex_agent_container.cli_pkg.agent_group import agent_group

    # Act
    names = sorted(agent_group.commands)
    # Assert
    assert "reconcile" in names
