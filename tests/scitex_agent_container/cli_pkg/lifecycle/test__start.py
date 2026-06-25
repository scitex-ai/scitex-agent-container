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

import json
import time
from pathlib import Path

from click.testing import CliRunner

from scitex_agent_container.cli_pkg.lifecycle._start import start


def _install_fresh_creds(home: Path) -> Path:
    """Write a non-expired OAuth credentials file under ``$home/.claude/``.

    The start command's preflight (``_state._preflight_creds.check_oauth_token_expiry``)
    reads ``$HOME/.claude/.credentials.json`` whenever an actual dispatch
    is about to fire — CI runners don't have one, so the preflight
    short-circuits with ``FileNotFoundError`` before the test's
    singleton-skip / multiplex / dispatch branch ever executes. Tests
    that pin ``$HOME`` to ``tmp_path`` and call this helper get a
    deterministic fresh-token preflight on any host.
    """
    claude_dir = home / ".claude"
    claude_dir.mkdir(parents=True, exist_ok=True)
    creds = claude_dir / ".credentials.json"
    expires_at_ms = int((time.time() + 3600) * 1000)
    creds.write_text(
        json.dumps(
            {
                "claudeAiOauth": {
                    "accessToken": "sk-ant-oat-fake",
                    "refreshToken": "sk-ant-ort-fake",
                    "expiresAt": expires_at_ms,
                    "scopes": ["user:inference"],
                    "subscriptionType": "max",
                }
            }
        ),
        encoding="utf-8",
    )
    return creds


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

    def test_continue_and_fresh_together_is_rejected(self):
        # Arrange
        runner = CliRunner()
        # Act
        result = runner.invoke(start, ["alpha", "--continue", "--fresh"])
        # Assert
        assert result.exit_code == 2

    def test_continue_and_fresh_together_explains_why(self):
        # Arrange
        runner = CliRunner()
        # Act
        result = runner.invoke(start, ["alpha", "--continue", "--fresh"])
        # Assert
        assert "mutually exclusive" in result.output

    def test_fresh_shorthand_contradicting_session_continue_is_rejected(self):
        # Arrange
        runner = CliRunner()
        # Act
        result = runner.invoke(start, ["alpha", "--fresh", "--session", "continue"])
        # Assert
        assert result.exit_code == 2

    def test_continue_shorthand_contradicting_session_fresh_explains_why(self):
        # Arrange
        runner = CliRunner()
        # Act
        result = runner.invoke(start, ["alpha", "--continue", "--session", "fresh"])
        # Assert
        assert "contradicts --session" in result.output

    def test_continue_shorthand_agreeing_with_session_continue_is_accepted(self):
        # Arrange — agreement is not a conflict; the command proceeds past
        # arg validation (it later fails on the missing agent, not exit 2).
        runner = CliRunner()
        # Act
        result = runner.invoke(start, ["alpha", "--continue", "--session", "continue"])
        # Assert — the contradiction guard did NOT fire (no exit-2 conflict).
        assert "contradicts --session" not in result.output

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
        "  workdir: /home/agent/work\n"
        "  apptainer:\n"
        "    image: ~/.scitex/agent-container/containers/sac-base.sif\n"
        "    binds: []\n"
        "  health:\n    enabled: true\n    interval: 60\n"
        "  restart:\n    policy: on-failure\n    max_retries: 3\n"
        "  claude:\n"
        "    model: haiku\n"
    )
    return y


def _record_live_singleton(
    state_db_path: Path, env_save_restore, name: str, host: str
) -> None:
    """Redirect state.db + record an active ``instances`` row for ``name``
    on ``host`` so :func:`_resolve_singleton_skip`'s liveness gate sees
    "live elsewhere" and preserves the legitimate skip path.

    Required because the gate (added in the follow-up to PR #252's
    real-liveness pattern) treats a singleton-on-wrong-host check as a
    stale dead-end binding when the registry has NO live row on the
    bound host — the lead's bm025 stale-binding repro. Tests that want
    the skip to fire MUST first prove there's a live agent on the bound
    host (otherwise the gate's release semantics rightly trigger).
    """
    import importlib

    env_save_restore.set("SCITEX_AGENT_CONTAINER_STATE_DB", str(state_db_path))
    import scitex_agent_container._state.state_db as _state_db_mod

    importlib.reload(_state_db_mod)
    _state_db_mod.record_instance_start(
        name=name,
        host=host,
        a2a_port=19200,
        bound_port=19200,
        remote=True,
        spawned_by="cli",
    )


class TestSingletonHostSkip:
    def test_single_target_singleton_skip_exits_clean(self, tmp_path, env_save_restore):
        # Arrange — live row on the bound host preserves the legitimate
        # skip path (without it, the liveness gate would release the
        # binding and fall through to a local start).
        env_save_restore.set("SCITEX_AGENT_CONTAINER_HOSTNAME", "this-host")
        env_save_restore.set("HOME", str(tmp_path))
        _install_fresh_creds(tmp_path)
        _record_live_singleton(
            tmp_path / "state.db", env_save_restore, "mini", "nowhere-host"
        )
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
        env_save_restore.set("HOME", str(tmp_path))
        _install_fresh_creds(tmp_path)
        _record_live_singleton(
            tmp_path / "state.db", env_save_restore, "mini", "nowhere-host"
        )
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
        env_save_restore.set("HOME", str(tmp_path))
        _install_fresh_creds(tmp_path)
        _record_live_singleton(
            tmp_path / "state.db", env_save_restore, "mini", "nowhere-host"
        )
        yaml_path = _write_singleton_yaml(tmp_path, "mini", "nowhere-host")
        runner = CliRunner()
        # Act
        result = runner.invoke(start, [str(yaml_path), "--json"])
        # Assert
        assert '"status": "skipped"' in result.output

    def test_single_target_singleton_dead_binding_releases_and_starts_local(
        self, tmp_path, env_save_restore
    ):
        # Arrange — singleton pinned to nowhere-host, NO live row recorded.
        # The new liveness gate must release the stale binding and fall
        # through to a real start path. We can't run apptainer in tests,
        # so we just verify the skip JSON was NOT emitted (i.e. the gate
        # did NOT short-circuit). Real start fails downstream, that's fine
        # — what we're pinning is that the skip path didn't fire.
        env_save_restore.set("SCITEX_AGENT_CONTAINER_HOSTNAME", "this-host")
        env_save_restore.set("HOME", str(tmp_path))
        _install_fresh_creds(tmp_path)
        # Redirect state.db to ensure the registry is empty for this name.
        import importlib

        env_save_restore.set(
            "SCITEX_AGENT_CONTAINER_STATE_DB", str(tmp_path / "state.db")
        )
        import scitex_agent_container._state.state_db as _state_db_mod

        importlib.reload(_state_db_mod)
        yaml_path = _write_singleton_yaml(tmp_path, "mini", "nowhere-host")
        runner = CliRunner()
        # Act
        result = runner.invoke(start, [str(yaml_path), "--json"])
        # Assert — no "skipped" status; the start path proceeded past
        # the skip gate (whether it then errored is independent).
        assert '"status": "skipped"' not in result.output

    def test_bulk_directory_singleton_skip_renders_skip_line(
        self, tmp_path, env_save_restore
    ):
        # Arrange — single-agent bulk dir stays on the in-process bulk
        # path (the SKIP-rendering loop). A MULTI-agent bulk dir now
        # routes to the serialized parallel launcher instead, covered by
        # test__start_parallel.py.
        env_save_restore.set("SCITEX_AGENT_CONTAINER_HOSTNAME", "this-host")
        env_save_restore.set("HOME", str(tmp_path))
        _install_fresh_creds(tmp_path)
        agents_dir = tmp_path / "agents"
        _write_singleton_yaml(agents_dir, "aa", "nowhere-host")
        runner = CliRunner()
        # Act
        result = runner.invoke(start, [str(agents_dir), "-y"])
        # Assert
        assert "SKIP aa" in result.output

    def test_bulk_directory_singleton_skip_exits_zero(self, tmp_path, env_save_restore):
        # Arrange — single-agent bulk dir stays on the in-process bulk
        # path; multi-agent bulk now routes to the parallel launcher.
        env_save_restore.set("SCITEX_AGENT_CONTAINER_HOSTNAME", "this-host")
        env_save_restore.set("HOME", str(tmp_path))
        _install_fresh_creds(tmp_path)
        agents_dir = tmp_path / "agents"
        _write_singleton_yaml(agents_dir, "aa", "nowhere-host")
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
        env_save_restore.set("HOME", str(tmp_path))
        _install_fresh_creds(tmp_path)
        _record_live_singleton(
            tmp_path / "state.db", env_save_restore, "mini", "nowhere-host"
        )
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
        env_save_restore.set("HOME", str(tmp_path))
        _install_fresh_creds(tmp_path)
        _record_live_singleton(
            tmp_path / "state.db", env_save_restore, "mini", "nowhere-host"
        )
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
        env_save_restore.set("HOME", str(tmp_path))
        _install_fresh_creds(tmp_path)
        # Two live rows so both singleton skips preserve the original
        # short-circuit (one helper call seeds state.db redirect, the
        # second adds the second row via the now-redirected default).
        _record_live_singleton(
            tmp_path / "state.db", env_save_restore, "mini1", "nowhere-host"
        )
        from scitex_agent_container._state.state_db import record_instance_start

        record_instance_start(
            name="mini2",
            host="nowhere-host",
            a2a_port=19201,
            bound_port=19201,
            remote=True,
            spawned_by="cli",
        )
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
        env_save_restore.set("HOME", str(tmp_path))
        _install_fresh_creds(tmp_path)
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
# real config.yaml on disk and the real ``_resolve_dispatch_peer``. Step 3b
# replaced the unconditional ``NotImplementedError`` stub with a drift-check
# / rsync body that fails early on a missing local spec dir (``FileNotFoundError``)
# when ``~/.scitex/agent-container/agents/<name>/`` does not exist. The
# per-target loop in ``_start.py`` has a catch-all that turns the exception
# into a printed traceback + ``sys.exit(1)``, so the observable signal lives
# in ``result.output``: ``FileNotFoundError`` + the peer name prove the
# routing branch reached the dispatcher; absence means the branch fell
# through. See feedback_no_silent_stubs.
#
# Flag-propagation (``--dry-run`` / ``--force``) is proven directly by the
# unit tests in ``test__dispatch.py``, which call ``_dispatch_remote_start``
# with ``dry_run=True`` / ``force=True`` and observe distinct branches.
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
        # reach the dispatcher, which raises FileNotFoundError because no
        # local spec dir exists under ``~/.scitex/agent-container/agents/``.
        env_save_restore.set("SCITEX_AGENT_CONTAINER_HOSTNAME", "this-host")
        env_save_restore.set("HOME", str(tmp_path))
        _install_fresh_creds(tmp_path)
        cfg = _write_peer_config(tmp_path, "remote-host")
        env_save_restore.set("SCITEX_AGENT_CONTAINER_CONFIG", str(cfg))
        yaml_path = _write_singleton_yaml(tmp_path, "mini", "remote-host")
        runner = CliRunner()
        # Act
        result = runner.invoke(start, [str(yaml_path)])
        # Assert
        assert "FileNotFoundError" in result.output

    def test_dispatch_branch_exception_message_names_target_peer(
        self, tmp_path, env_save_restore
    ):
        # Arrange — dispatcher's traceback must echo the peer name so we
        # know the routing branch handed off the resolved peer.
        env_save_restore.set("SCITEX_AGENT_CONTAINER_HOSTNAME", "this-host")
        env_save_restore.set("HOME", str(tmp_path))
        _install_fresh_creds(tmp_path)
        cfg = _write_peer_config(tmp_path, "remote-host")
        env_save_restore.set("SCITEX_AGENT_CONTAINER_CONFIG", str(cfg))
        yaml_path = _write_singleton_yaml(tmp_path, "mini", "remote-host")
        runner = CliRunner()
        # Act
        result = runner.invoke(start, [str(yaml_path)])
        # Assert
        assert "remote-host" in result.output

    def test_dispatch_branch_propagates_agent_name_to_dispatcher(
        self, tmp_path, env_save_restore
    ):
        # Arrange — the dispatcher's exception must name the spec it tried
        # to dispatch, proving the agent name flowed through the routing
        # branch.
        env_save_restore.set("SCITEX_AGENT_CONTAINER_HOSTNAME", "this-host")
        env_save_restore.set("HOME", str(tmp_path))
        _install_fresh_creds(tmp_path)
        cfg = _write_peer_config(tmp_path, "remote-host")
        env_save_restore.set("SCITEX_AGENT_CONTAINER_CONFIG", str(cfg))
        yaml_path = _write_singleton_yaml(tmp_path, "mini", "remote-host")
        runner = CliRunner()
        # Act
        result = runner.invoke(start, [str(yaml_path)])
        # Assert
        assert "Spec dir for 'mini'" in result.output

    def test_dispatch_branch_exits_nonzero_when_dispatcher_raises(
        self, tmp_path, env_save_restore
    ):
        # Arrange — the routing branch must fire and the dispatcher's
        # FileNotFoundError must propagate to a nonzero exit, not be
        # silently swallowed.
        env_save_restore.set("SCITEX_AGENT_CONTAINER_HOSTNAME", "this-host")
        env_save_restore.set("HOME", str(tmp_path))
        cfg = _write_peer_config(tmp_path, "remote-host")
        env_save_restore.set("SCITEX_AGENT_CONTAINER_CONFIG", str(cfg))
        yaml_path = _write_singleton_yaml(tmp_path, "mini", "remote-host")
        runner = CliRunner()
        # Act
        result = runner.invoke(start, [str(yaml_path), "--dry-run", "--force"])
        # Assert
        assert result.exit_code != 0

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
        # ``Skipping 'mini'`` line. A live row on the bound host
        # preserves the original short-circuit (liveness gate sees the
        # agent IS running over there, so deferring is meaningful).
        env_save_restore.set("SCITEX_AGENT_CONTAINER_HOSTNAME", "this-host")
        env_save_restore.set("HOME", str(tmp_path))
        _install_fresh_creds(tmp_path)
        cfg = _write_peer_config(tmp_path, "remote-host")
        env_save_restore.set("SCITEX_AGENT_CONTAINER_CONFIG", str(cfg))
        _record_live_singleton(
            tmp_path / "state.db", env_save_restore, "mini", "unknown-host"
        )
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

    def test_no_redispatch_flag_skips_dispatch_branch(self, tmp_path, env_save_restore):
        # Arrange — spec.host names a known remote peer; --no-redispatch
        # must skip the dispatch branch (peer-side invocation contract
        # — prevents ssh recursion). Singleton skip still fires
        # because spec.host != current host.
        env_save_restore.set("SCITEX_AGENT_CONTAINER_HOSTNAME", "this-host")
        env_save_restore.set("HOME", str(tmp_path))
        cfg = _write_peer_config(tmp_path, "remote-host")
        env_save_restore.set("SCITEX_AGENT_CONTAINER_CONFIG", str(cfg))
        yaml_path = _write_singleton_yaml(tmp_path, "mini", "remote-host")
        runner = CliRunner()
        # Act
        result = runner.invoke(start, [str(yaml_path), "--no-redispatch"])
        # Assert — dispatcher's FileNotFoundError (its first action when
        # spec dir is absent) MUST NOT appear; the branch never fired.
        assert "FileNotFoundError" not in result.output


# ---------------------------------------------------------------------------
# Peer-side ``sac agents start --no-redispatch --json`` output contract.
#
# Background: before the cross-host a2a_port fix the peer-side JSON read
# ``config.a2a.port`` directly. When the spec said ``port: auto`` the
# literal string ``"auto"`` flunked the ``isinstance(_, int)`` check and
# the JSON emitted ``null`` — even though the runner had ALREADY claimed
# an int via ``resolve_a2a_port``. The lead then wrote NULL into its
# instances row, breaking ``sac agents send <name>`` ("state.db records
# no a2a_port for it"). These tests pin the JSON-emission seam: with an
# allocator row in place (simulating ``resolve_a2a_port``'s side effect),
# the ``--no-redispatch --json`` payload MUST report the resolved int.
#
# No-mocks / no-monkeypatch: hand-rolled context-manager attribute swap
# for fake ``agent_start`` (same pattern as ``test__a2a_port.py``).
# ---------------------------------------------------------------------------


from contextlib import contextmanager
from typing import Any, Iterator


@contextmanager
def _swap_attr(module: Any, name: str, replacement: Any) -> Iterator[None]:
    saved = getattr(module, name)
    setattr(module, name, replacement)
    try:
        yield
    finally:
        setattr(module, name, saved)


def _write_local_spec_with_a2a(home: Path, name: str, *, a2a_port: Any) -> Path:
    """Materialise a minimal spec yaml at ``~/.scitex/agent-container/agents/<name>``.

    The spec uses runtime ``apptainer`` (the default sac runtime). We
    never actually invoke the runner — ``agent_start`` is swapped for a
    no-op — so the spec only needs to load cleanly.
    """
    agents_dir = home / ".scitex" / "agent-container" / "agents" / name
    agents_dir.mkdir(parents=True)
    yaml_path = agents_dir / f"{name}.yaml"
    port_line = "null" if a2a_port is None else json.dumps(a2a_port)
    yaml_path.write_text(
        "apiVersion: scitex-agent-container/v3\n"
        "kind: Agent\n"
        "metadata: {}\n"
        "spec:\n"
        "  runtime: apptainer\n"
        "  host: local\n"
        "  workdir: /home/agent/work\n"
        "  apptainer:\n    image: /x.sif\n    binds: []\n"
        "  claude:\n    model: sonnet\n"
        "  health:\n    enabled: true\n    interval: 60\n"
        "  restart:\n    policy: on-failure\n    max_retries: 3\n"
        f"  a2a:\n    port: {port_line}\n"
    )
    return yaml_path


def _run_start_no_redispatch_json(
    name: str, yaml_path: Path, *, preclaim_port: int | None
) -> dict:
    """Drive ``sac agents start <yaml> --no-redispatch --json`` with a
    no-op fake ``agent_start`` and (optionally) a pre-claimed allocator
    row. Returns the parsed JSON object emitted on stdout.

    Pre-claiming substitutes for what the real ``agent_start`` would do
    via ``resolve_a2a_port`` — keeping the test focused on the JSON
    emission seam without spinning real apptainer.
    """
    from scitex_agent_container._state import port_allocator

    if preclaim_port is not None:
        port_allocator.claim_port(name, explicit=preclaim_port)

    from scitex_agent_container.cli_pkg.lifecycle import _start as start_mod

    # ``agent_start`` is invoked inside the per-target loop, which lives
    # in the ``_start_single`` sibling (split out of ``_start`` to keep
    # the click entry under the line cap). Swap it there.
    from scitex_agent_container.cli_pkg.lifecycle import _start_single as single_mod

    def _fake_agent_start(*args: Any, **kwargs: Any) -> bool:
        return True

    runner = CliRunner()
    with _swap_attr(single_mod, "agent_start", _fake_agent_start):
        result = runner.invoke(
            start_mod.start,
            [str(yaml_path), "--no-redispatch", "--json"],
            catch_exceptions=False,
        )
    stdout_lines = [
        ln for ln in result.output.splitlines() if ln.strip().startswith("{")
    ]
    assert stdout_lines, (
        f"no JSON line in stdout. exit={result.exit_code}, output={result.output!r}"
    )
    return json.loads(stdout_lines[-1])


class TestStartNoRedispatchJsonA2aPort:
    def test_start_no_redispatch_json_includes_resolved_a2a_port(
        self, tmp_path, env_save_restore
    ):
        """``port: auto`` spec → JSON ``a2a_port`` is an int (resolved by allocator)."""
        # Arrange — redirect HOME + state.db so the allocator + spec dir
        # operate in tmp. ``state_db`` and ``port_allocator`` cache the
        # default path at import time, so we reload them after env mutation.
        import importlib

        env_save_restore.set("HOME", str(tmp_path))
        env_save_restore.set(
            "SCITEX_AGENT_CONTAINER_STATE_DB", str(tmp_path / "state.db")
        )
        import scitex_agent_container._state.port_allocator as _pa
        import scitex_agent_container._state.state_db as _sdb

        importlib.reload(_sdb)
        importlib.reload(_pa)
        try:
            yaml_path = _write_local_spec_with_a2a(tmp_path, "alpha", a2a_port="auto")
            # Act — pre-claim port 19_200 to simulate resolve_a2a_port's effect.
            payload = _run_start_no_redispatch_json(
                "alpha", yaml_path, preclaim_port=19_200
            )
            # Assert
            assert payload["a2a_port"] == 19_200
        finally:
            importlib.reload(_sdb)
            importlib.reload(_pa)

    def test_start_no_redispatch_json_includes_a2a_port_when_explicit_int(
        self, tmp_path, env_save_restore
    ):
        """``port: 19_500`` spec → JSON ``a2a_port`` is exactly that int."""
        # Arrange
        import importlib

        env_save_restore.set("HOME", str(tmp_path))
        env_save_restore.set(
            "SCITEX_AGENT_CONTAINER_STATE_DB", str(tmp_path / "state.db")
        )
        import scitex_agent_container._state.port_allocator as _pa
        import scitex_agent_container._state.state_db as _sdb

        importlib.reload(_sdb)
        importlib.reload(_pa)
        try:
            yaml_path = _write_local_spec_with_a2a(tmp_path, "alpha", a2a_port=19_500)
            # Act
            payload = _run_start_no_redispatch_json(
                "alpha", yaml_path, preclaim_port=19_500
            )
            # Assert
            assert payload["a2a_port"] == 19_500
        finally:
            importlib.reload(_sdb)
            importlib.reload(_pa)


def test_start_command_exposes_strict_drift_flag():
    # Arrange
    flag_names = {opt for p in start.params for opt in p.opts}
    # Act
    has_flag = "--strict-drift" in flag_names
    # Assert
    assert has_flag is True


# ---------------------------------------------------------------------------
# Cold-start forms (operator TODO 2026-06-17) — sac (agents) start <path|host>.
# All use --dry-run so nothing launches or is written to the real agents root.
# ---------------------------------------------------------------------------


class TestColdStart:
    def test_local_workdir_dry_run_prints_plan_and_exits_zero(self, tmp_path):
        # Arrange — a non-empty project workdir (not an agents root).
        work = tmp_path / "figdemo"
        work.mkdir()
        (work / "README.md").write_text("x")
        # Act
        result = CliRunner().invoke(start, [str(work), "--dry-run"])
        # Assert
        assert result.exit_code == 0

    def test_local_workdir_dry_run_names_the_derived_label(self, tmp_path):
        # Arrange
        work = tmp_path / "figdemo"
        work.mkdir()
        (work / "README.md").write_text("x")
        # Act
        result = CliRunner().invoke(start, [str(work), "--dry-run"])
        # Assert
        assert "figdemo" in result.output

    def test_local_workdir_dry_run_does_not_write_spec(self, tmp_path):
        # Arrange
        work = tmp_path / "figdemo-unique-xyz"
        work.mkdir()
        (work / "README.md").write_text("x")
        # Act
        CliRunner().invoke(start, [str(work), "--dry-run"])
        # Assert — dry-run must not materialize into the real agents root.
        from scitex_agent_container.cli_pkg._new import _default_base_dir

        assert not (_default_base_dir() / "figdemo-unique-xyz").exists()

    def test_json_dry_run_emits_cold_start_payload(self, tmp_path):
        # Arrange
        work = tmp_path / "figdemo"
        work.mkdir()
        (work / "README.md").write_text("x")
        # Act
        result = CliRunner().invoke(start, [str(work), "--dry-run", "--json"])
        # Assert
        assert "cold_start" in result.output

    def test_malformed_form_fails_loud_exit_two(self):
        # Arrange
        arg = "fig@nohost"
        # Act
        result = CliRunner().invoke(start, [arg, "--dry-run"])
        # Assert
        assert result.exit_code == 2
