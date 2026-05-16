"""Tests for cli_pkg.lifecycle._start.

PA-306 no-mocks slim-down. The previous suite drove the start
command by ``monkeypatch.setattr``-ing ``agent_start``,
``load_config``, ``resolve_hostname``, ``resolve_with_prefix``,
``_singleton_skip_reason`` and ``_multiplex_foreground_tails`` at
the module namespace — i.e., it tested whatever the test author
substituted in, not production. Those tests are deleted (mock-only
by construction; there is no honest seam — the entire
``agent_start`` path materialises a real apptainer workspace and
the ``start`` click command takes no python-level injection
kwarg). What remains drives the real CLI surface end-to-end up to
(but not into) ``agent_start``: argument validation, real CSV /
template expansion against the production ``expand_params_file``,
real directory walking via ``_iter_agent_yamls``.

The behaviour the deleted tests claimed to cover (skip-reason
formatting, session-override plumbing, multiplexer dispatch,
bulk-loop FAILED/SKIP rendering, force/dry-run hooks) is exercised
by direct unit tests against ``_singleton_skip_reason`` /
``_multiplex_foreground_tails`` in ``test__common.py`` (already
converted to no-mocks) and by the end-to-end ``examples/agents/*``
smoke tests.
"""

from __future__ import annotations

from pathlib import Path

from click.testing import CliRunner

from scitex_agent_container.cli_pkg.lifecycle._start import start

# ---------------------------------------------------------------------------
# Argument-level validation — no collaborator is invoked; click parses,
# the command runs the validation branch, exits. Pure CLI surface.
# ---------------------------------------------------------------------------


class TestArgumentValidation:
    def test_resume_with_session_new_session_is_rejected(self):
        # Arrange
        runner = CliRunner()
        # Act
        result = runner.invoke(
            start, ["alpha", "--resume", "abc", "--session", "new-session"]
        )
        # Assert
        assert result.exit_code == 2

    def test_resume_with_session_new_session_explains_why(self):
        # Arrange
        runner = CliRunner()
        # Act
        result = runner.invoke(
            start, ["alpha", "--resume", "abc", "--session", "new-session"]
        )
        # Assert
        assert "requires --session resume" in result.output

    def test_resume_combined_with_directory_target_is_rejected(self, tmp_path):
        # Arrange
        agents_dir = tmp_path / "agents"
        (agents_dir / "x").mkdir(parents=True)
        (agents_dir / "x" / "x.yaml").write_text("x")
        runner = CliRunner()
        # Act
        result = runner.invoke(start, [str(agents_dir), "--resume", "abc", "-y"])
        # Assert
        assert result.exit_code == 2

    def test_resume_combined_with_directory_explains_why(self, tmp_path):
        # Arrange
        agents_dir = tmp_path / "agents"
        (agents_dir / "x").mkdir(parents=True)
        (agents_dir / "x" / "x.yaml").write_text("x")
        runner = CliRunner()
        # Act
        result = runner.invoke(start, [str(agents_dir), "--resume", "abc", "-y"])
        # Assert
        assert "cannot be combined with directory" in result.output

    def test_bulk_directory_without_yes_exits_two(self, tmp_path):
        # Arrange
        agents_dir = tmp_path / "agents"
        (agents_dir / "a").mkdir(parents=True)
        (agents_dir / "a" / "a.yaml").write_text("x")
        (agents_dir / "b").mkdir()
        (agents_dir / "b" / "b.yaml").write_text("x")
        runner = CliRunner()
        # Act
        result = runner.invoke(start, [str(agents_dir)])
        # Assert
        assert result.exit_code == 2

    def test_bulk_directory_without_yes_explains_why(self, tmp_path):
        # Arrange
        agents_dir = tmp_path / "agents"
        (agents_dir / "a").mkdir(parents=True)
        (agents_dir / "a" / "a.yaml").write_text("x")
        (agents_dir / "b").mkdir()
        (agents_dir / "b" / "b.yaml").write_text("x")
        runner = CliRunner()
        # Act
        result = runner.invoke(start, [str(agents_dir)])
        # Assert
        assert "Refusing to start" in result.output

    def test_bulk_directory_empty_returns_clean(self, tmp_path):
        # Arrange — directory exists but no agent subdirs; bulk loop
        # has nothing to do and single_targets is also empty.
        agents_dir = tmp_path / "agents"
        agents_dir.mkdir()
        runner = CliRunner()
        # Act
        result = runner.invoke(start, [str(agents_dir), "-y"])
        # Assert
        assert result.exit_code == 0


# ---------------------------------------------------------------------------
# --params-file expansion — drives the REAL ``expand_params_file``
# helper (no mocks). Validation branches and CSV error surface.
# ---------------------------------------------------------------------------


class TestParamsFile:
    def test_params_file_requires_exactly_one_target_exit_two(self, tmp_path):
        # Arrange
        csv = tmp_path / "p.csv"
        csv.write_text("name\n")
        runner = CliRunner()
        # Act
        result = runner.invoke(start, ["a", "b", "--params-file", str(csv)])
        # Assert
        assert result.exit_code == 2

    def test_params_file_requires_exactly_one_target_explains_why(self, tmp_path):
        # Arrange
        csv = tmp_path / "p.csv"
        csv.write_text("name\n")
        runner = CliRunner()
        # Act
        result = runner.invoke(start, ["a", "b", "--params-file", str(csv)])
        # Assert
        assert "exactly one TARGET" in result.output

    def test_params_file_template_must_exist_exit_two(self, tmp_path):
        # Arrange
        csv = tmp_path / "p.csv"
        csv.write_text("name\n")
        runner = CliRunner()
        # Act
        result = runner.invoke(start, ["no-such-template", "--params-file", str(csv)])
        # Assert
        assert result.exit_code == 2

    def test_params_file_template_must_exist_explains_why(self, tmp_path):
        # Arrange
        csv = tmp_path / "p.csv"
        csv.write_text("name\n")
        runner = CliRunner()
        # Act
        result = runner.invoke(start, ["no-such-template", "--params-file", str(csv)])
        # Assert
        assert "template not found" in result.output

    def test_params_file_missing_name_column_exit_two(self, tmp_path):
        # Arrange — real CSV without ``name`` column drives the real
        # ``expand_params_file`` ValueError path.
        template = tmp_path / "tpl.yaml"
        template.write_text("name: x\n")
        csv = tmp_path / "p.csv"
        csv.write_text("not_name\nfoo\n")
        runner = CliRunner()
        # Act
        result = runner.invoke(start, [str(template), "--params-file", str(csv)])
        # Assert
        assert result.exit_code == 2

    def test_params_file_missing_name_column_surfaces_real_message(self, tmp_path):
        # Arrange
        template = tmp_path / "tpl.yaml"
        template.write_text("name: x\n")
        csv = tmp_path / "p.csv"
        csv.write_text("not_name\nfoo\n")
        runner = CliRunner()
        # Act
        result = runner.invoke(start, [str(template), "--params-file", str(csv)])
        # Assert
        assert "name" in result.output


# ---------------------------------------------------------------------------
# Singleton host-skip — drives the real ``_singleton_skip_reason`` against
# a real YAML on disk; hostname is pinned via the production env var so the
# command short-circuits before reaching ``agent_start``. No mocks.
# ---------------------------------------------------------------------------


def _write_singleton_yaml(parent: Path, name: str, host: str) -> Path:
    """Write an agent YAML pinned to ``host`` under ``<parent>/<name>/<name>.yaml``."""
    sub = parent / name
    sub.mkdir(parents=True, exist_ok=True)
    y = sub / f"{name}.yaml"
    y.write_text(
        "apiVersion: scitex-agent-container/v3\n"
        "kind: Agent\n"
        "spec:\n"
        "  runtime: apptainer\n"
        f"  host: {host}\n"
        "  apptainer:\n"
        "    image: ~/.scitex/agent-container/containers/sac-base.sif\n"
        "  claude:\n"
        "    model: haiku\n"
    )
    return y


class TestSingletonHostSkip:
    def test_single_target_singleton_skip_exits_clean(self, tmp_path, env_save_restore):
        # Arrange
        env_save_restore.set("SCITEX_AGENT_CONTAINER_HOSTNAME", "this-host")
        yaml_path = _write_singleton_yaml(tmp_path, "mini", "nowhere-host")
        runner = CliRunner()
        # Act
        result = runner.invoke(start, [str(yaml_path)])
        # Assert
        assert result.exit_code == 0

    def test_single_target_singleton_skip_explains_host_mismatch(
        self, tmp_path, env_save_restore
    ):
        # Arrange
        env_save_restore.set("SCITEX_AGENT_CONTAINER_HOSTNAME", "this-host")
        yaml_path = _write_singleton_yaml(tmp_path, "mini", "nowhere-host")
        runner = CliRunner()
        # Act
        result = runner.invoke(start, [str(yaml_path)])
        # Assert
        assert "Skipping 'mini'" in result.output

    def test_single_target_singleton_skip_emits_json_status(
        self, tmp_path, env_save_restore
    ):
        # Arrange
        env_save_restore.set("SCITEX_AGENT_CONTAINER_HOSTNAME", "this-host")
        yaml_path = _write_singleton_yaml(tmp_path, "mini", "nowhere-host")
        runner = CliRunner()
        # Act
        result = runner.invoke(start, [str(yaml_path), "--json"])
        # Assert
        assert '"status": "skipped"' in result.output

    def test_bulk_directory_singleton_skip_renders_skip_line(
        self, tmp_path, env_save_restore
    ):
        # Arrange
        env_save_restore.set("SCITEX_AGENT_CONTAINER_HOSTNAME", "this-host")
        agents_dir = tmp_path / "agents"
        _write_singleton_yaml(agents_dir, "aa", "nowhere-host")
        _write_singleton_yaml(agents_dir, "bb", "nowhere-host")
        runner = CliRunner()
        # Act
        result = runner.invoke(start, [str(agents_dir), "-y"])
        # Assert
        assert "SKIP aa" in result.output

    def test_bulk_directory_singleton_skip_exits_zero(self, tmp_path, env_save_restore):
        # Arrange
        env_save_restore.set("SCITEX_AGENT_CONTAINER_HOSTNAME", "this-host")
        agents_dir = tmp_path / "agents"
        _write_singleton_yaml(agents_dir, "aa", "nowhere-host")
        _write_singleton_yaml(agents_dir, "bb", "nowhere-host")
        runner = CliRunner()
        # Act
        result = runner.invoke(start, [str(agents_dir), "-y"])
        # Assert
        assert result.exit_code == 0


# ---------------------------------------------------------------------------
# Resume + foreground branches — non-validation flag plumbing that exits via
# the singleton-skip short-circuit so no real ``agent_start`` is invoked.
# ---------------------------------------------------------------------------


class TestResumeAndForeground:
    def test_resume_without_session_is_accepted(self, tmp_path, env_save_restore):
        # Arrange — --resume without --session must default session_mode to
        # "resume" rather than rejecting the invocation.
        env_save_restore.set("SCITEX_AGENT_CONTAINER_HOSTNAME", "this-host")
        yaml_path = _write_singleton_yaml(tmp_path, "mini", "nowhere-host")
        runner = CliRunner()
        # Act
        result = runner.invoke(start, [str(yaml_path), "--resume", "abc-uuid"])
        # Assert
        assert result.exit_code == 0

    def test_resume_with_matching_session_resume_is_accepted(
        self, tmp_path, env_save_restore
    ):
        # Arrange
        env_save_restore.set("SCITEX_AGENT_CONTAINER_HOSTNAME", "this-host")
        yaml_path = _write_singleton_yaml(tmp_path, "mini", "nowhere-host")
        runner = CliRunner()
        # Act
        result = runner.invoke(
            start, [str(yaml_path), "--resume", "abc", "--session", "resume"]
        )
        # Assert
        assert result.exit_code == 0

    def test_foreground_with_multiple_targets_takes_multiplex_branch(
        self, tmp_path, env_save_restore
    ):
        # Arrange — two singleton-skipped targets so multi_foreground is True
        # (disables per-runtime attach) but `not dry_run` blocks the actual
        # multiplex call; we just exercise the branch where foreground gets
        # demoted from True -> False.
        env_save_restore.set("SCITEX_AGENT_CONTAINER_HOSTNAME", "this-host")
        y1 = _write_singleton_yaml(tmp_path, "mini1", "nowhere-host")
        y2 = _write_singleton_yaml(tmp_path, "mini2", "nowhere-host")
        runner = CliRunner()
        # Act
        result = runner.invoke(start, [str(y1), str(y2), "--foreground", "--dry-run"])
        # Assert
        assert result.exit_code == 0

    def test_foreground_with_bulk_directory_takes_multiplex_branch(
        self, tmp_path, env_save_restore
    ):
        # Arrange
        env_save_restore.set("SCITEX_AGENT_CONTAINER_HOSTNAME", "this-host")
        agents_dir = tmp_path / "agents"
        _write_singleton_yaml(agents_dir, "aa", "nowhere-host")
        _write_singleton_yaml(agents_dir, "bb", "nowhere-host")
        runner = CliRunner()
        # Act
        result = runner.invoke(
            start, [str(agents_dir), "-y", "--foreground", "--dry-run"]
        )
        # Assert
        assert "SKIP bb" in result.output


# ---------------------------------------------------------------------------
# Cross-host dispatch routing branch — drives the real Click command with a
# real config.yaml on disk and the real ``_resolve_dispatch_peer``. The stub
# ``_dispatch_remote_start`` raises ``NotImplementedError`` (no silent stub).
# The per-target loop in ``_start.py`` has a catch-all that turns the
# exception into a printed traceback + ``sys.exit(1)``, so the observable
# signal lives in ``result.output``: the ``NotImplementedError`` class name +
# the dispatcher's signature line prove the routing branch reached the
# dispatcher; absence means the branch fell through. See feedback_no_silent_stubs.
# ---------------------------------------------------------------------------


def _write_peer_config(parent: Path, peer_name: str) -> Path:
    """Write a real ``config.yaml`` with a single peer entry; return its path."""
    cfg = parent / "config.yaml"
    cfg.write_text(
        "host:\n  fallback: hostname-short\npeers:\n"
        f"  {peer_name}:\n    ssh: {peer_name}\n"
    )
    return cfg


class TestDispatchBranch:
    def test_dispatch_branch_fires_when_spec_host_names_known_peer(
        self, tmp_path, env_save_restore
    ):
        # Arrange — spec.host is a known remote peer; routing branch must
        # reach the dispatcher, which raises NotImplementedError.
        env_save_restore.set("SCITEX_AGENT_CONTAINER_HOSTNAME", "this-host")
        cfg = _write_peer_config(tmp_path, "remote-host")
        env_save_restore.set("SCITEX_AGENT_CONTAINER_CONFIG", str(cfg))
        yaml_path = _write_singleton_yaml(tmp_path, "mini", "remote-host")
        runner = CliRunner()
        # Act
        result = runner.invoke(start, [str(yaml_path)])
        # Assert
        assert "NotImplementedError" in result.output

    def test_dispatch_branch_exception_message_names_target_peer(
        self, tmp_path, env_save_restore
    ):
        # Arrange — dispatcher's traceback must echo the peer name so we
        # know the routing branch handed off the resolved peer.
        env_save_restore.set("SCITEX_AGENT_CONTAINER_HOSTNAME", "this-host")
        cfg = _write_peer_config(tmp_path, "remote-host")
        env_save_restore.set("SCITEX_AGENT_CONTAINER_CONFIG", str(cfg))
        yaml_path = _write_singleton_yaml(tmp_path, "mini", "remote-host")
        runner = CliRunner()
        # Act
        result = runner.invoke(start, [str(yaml_path)])
        # Assert
        assert "peer='remote-host'" in result.output

    def test_dispatch_branch_propagates_dry_run_flag_to_dispatcher(
        self, tmp_path, env_save_restore
    ):
        # Arrange — --dry-run must reach the dispatcher's kwargs; the
        # traceback echoes ``dry_run=True``.
        env_save_restore.set("SCITEX_AGENT_CONTAINER_HOSTNAME", "this-host")
        cfg = _write_peer_config(tmp_path, "remote-host")
        env_save_restore.set("SCITEX_AGENT_CONTAINER_CONFIG", str(cfg))
        yaml_path = _write_singleton_yaml(tmp_path, "mini", "remote-host")
        runner = CliRunner()
        # Act
        result = runner.invoke(start, [str(yaml_path), "--dry-run", "--force"])
        # Assert
        assert "dry_run=True" in result.output

    def test_dispatch_branch_propagates_force_flag_to_dispatcher(
        self, tmp_path, env_save_restore
    ):
        # Arrange — --force must reach the dispatcher's kwargs; the
        # traceback echoes ``force=True``.
        env_save_restore.set("SCITEX_AGENT_CONTAINER_HOSTNAME", "this-host")
        cfg = _write_peer_config(tmp_path, "remote-host")
        env_save_restore.set("SCITEX_AGENT_CONTAINER_CONFIG", str(cfg))
        yaml_path = _write_singleton_yaml(tmp_path, "mini", "remote-host")
        runner = CliRunner()
        # Act
        result = runner.invoke(start, [str(yaml_path), "--dry-run", "--force"])
        # Assert
        assert "force=True" in result.output

    def test_dispatch_branch_quiet_when_spec_host_equals_current_host(
        self, tmp_path, env_save_restore
    ):
        # Arrange — current host equals spec.host; resolver returns None,
        # routing branch must NOT fire. Downstream local-start may fail
        # (no real apptainer image), but the dispatcher's traceback MUST
        # NOT appear.
        env_save_restore.set("SCITEX_AGENT_CONTAINER_HOSTNAME", "this-host")
        cfg = _write_peer_config(tmp_path, "this-host")
        env_save_restore.set("SCITEX_AGENT_CONTAINER_CONFIG", str(cfg))
        yaml_path = _write_singleton_yaml(tmp_path, "mini", "this-host")
        runner = CliRunner()
        # Act
        result = runner.invoke(start, [str(yaml_path), "--dry-run"])
        # Assert
        assert "NotImplementedError" not in result.output

    def test_singleton_skip_still_fires_when_spec_host_is_unknown(
        self, tmp_path, env_save_restore
    ):
        # Arrange — spec.host is NOT in the peer registry AND NOT the
        # current host. Resolver returns None ("caller decides"), the
        # routing branch falls through, and singleton-skip emits the
        # ``Skipping 'mini'`` line.
        env_save_restore.set("SCITEX_AGENT_CONTAINER_HOSTNAME", "this-host")
        cfg = _write_peer_config(tmp_path, "remote-host")
        env_save_restore.set("SCITEX_AGENT_CONTAINER_CONFIG", str(cfg))
        yaml_path = _write_singleton_yaml(tmp_path, "mini", "unknown-host")
        runner = CliRunner()
        # Act
        result = runner.invoke(start, [str(yaml_path)])
        # Assert
        assert "Skipping 'mini'" in result.output

    def test_dispatch_branch_quiet_when_spec_host_is_unknown(
        self, tmp_path, env_save_restore
    ):
        # Arrange — same setup; complementary assertion that the
        # dispatcher did NOT raise (the branch fell through).
        env_save_restore.set("SCITEX_AGENT_CONTAINER_HOSTNAME", "this-host")
        cfg = _write_peer_config(tmp_path, "remote-host")
        env_save_restore.set("SCITEX_AGENT_CONTAINER_CONFIG", str(cfg))
        yaml_path = _write_singleton_yaml(tmp_path, "mini", "unknown-host")
        runner = CliRunner()
        # Act
        result = runner.invoke(start, [str(yaml_path)])
        # Assert
        assert "NotImplementedError" not in result.output
