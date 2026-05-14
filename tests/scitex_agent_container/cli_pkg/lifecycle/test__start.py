"""Tests for cli_pkg.lifecycle._start.

Heavy subprocess / runtime mocked. Drive via CliRunner.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from scitex_agent_container.cli_pkg.lifecycle._start import start
from scitex_agent_container.config import AgentConfig


@pytest.fixture(autouse=True)
def _isolate_home(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))


def _cfg(name="alpha", workdir="/tmp/wd"):
    c = AgentConfig(name=name, workdir=workdir)
    return c


def _patch_chain(
    monkeypatch, *, cfg=None, agent_start_side=None, skip=None, host="local"
):
    cfg = cfg or _cfg()
    monkeypatch.setattr(
        "scitex_agent_container.cli_pkg.lifecycle._start.resolve_with_prefix",
        lambda t: f"/fake/{t}.yaml",
    )
    monkeypatch.setattr(
        "scitex_agent_container.cli_pkg.lifecycle._start.load_config",
        lambda p: cfg,
    )
    monkeypatch.setattr(
        "scitex_agent_container.cli_pkg.lifecycle._start.resolve_hostname",
        lambda: host,
    )
    monkeypatch.setattr(
        "scitex_agent_container.cli_pkg.lifecycle._start._singleton_skip_reason",
        lambda c, h: skip,
    )
    if agent_start_side is None:
        agent_start_side = lambda *a, **kw: True  # noqa: E731
    monkeypatch.setattr(
        "scitex_agent_container.cli_pkg.lifecycle._start.agent_start",
        agent_start_side,
    )


def test_start_single_happy(monkeypatch):
    calls = []
    _patch_chain(
        monkeypatch,
        agent_start_side=lambda *a, **kw: calls.append((a, kw)) or True,
    )
    runner = CliRunner()
    result = runner.invoke(start, ["alpha"])
    assert result.exit_code == 0, result.output
    assert calls and calls[0][1]["foreground"] is False


def test_start_single_json(monkeypatch):
    _patch_chain(monkeypatch)
    runner = CliRunner()
    result = runner.invoke(start, ["alpha", "--json"])
    assert result.exit_code == 0
    obj = json.loads(result.output.strip().splitlines()[-1])
    assert obj["name"] == "alpha"
    assert obj["status"] == "started"


def test_start_single_skip_reason_human(monkeypatch):
    _patch_chain(monkeypatch, skip="wrong host")
    runner = CliRunner()
    result = runner.invoke(start, ["alpha"])
    assert result.exit_code == 0
    assert "Skipping" in result.output


def test_start_single_skip_reason_json(monkeypatch):
    _patch_chain(monkeypatch, skip="wrong host")
    runner = CliRunner()
    result = runner.invoke(start, ["alpha", "--json"])
    assert result.exit_code == 0
    obj = json.loads(result.output.strip().splitlines()[-1])
    assert obj["status"] == "skipped"
    assert obj["reason"] == "wrong host"


def test_start_failure_human(monkeypatch):
    _patch_chain(
        monkeypatch,
        agent_start_side=lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("boom")),
    )
    runner = CliRunner()
    result = runner.invoke(start, ["alpha"])
    assert result.exit_code == 1
    assert "boom" in result.output


def test_start_failure_json(monkeypatch):
    _patch_chain(
        monkeypatch,
        agent_start_side=lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("boom")),
    )
    runner = CliRunner()
    result = runner.invoke(start, ["alpha", "--json"])
    assert result.exit_code == 1
    obj = json.loads(result.output.strip().splitlines()[-1])
    assert obj["status"] == "error" and "boom" in obj["error"]


def test_resume_implies_session_resume(monkeypatch):
    captured = []
    _patch_chain(
        monkeypatch,
        agent_start_side=lambda *a, **kw: captured.append(kw) or True,
    )
    runner = CliRunner()
    result = runner.invoke(start, ["alpha", "--resume", "abc"])
    assert result.exit_code == 0
    assert captured[0]["session_override"] == "resume"
    assert captured[0]["resume_id_override"] == "abc"


def test_resume_conflict_with_session(monkeypatch):
    _patch_chain(monkeypatch)
    runner = CliRunner()
    result = runner.invoke(
        start, ["alpha", "--resume", "abc", "--session", "new-session"]
    )
    assert result.exit_code == 2
    assert "requires --session resume" in result.output


def test_session_override_emitted(monkeypatch):
    _patch_chain(monkeypatch)
    runner = CliRunner()
    result = runner.invoke(start, ["alpha", "--session", "new-session"])
    assert result.exit_code == 0


def test_no_preflight_message(monkeypatch):
    _patch_chain(monkeypatch)
    runner = CliRunner()
    result = runner.invoke(start, ["alpha", "--no-preflight"])
    assert result.exit_code == 0


def test_force_message(monkeypatch):
    _patch_chain(monkeypatch)
    runner = CliRunner()
    result = runner.invoke(start, ["alpha", "--force"])
    assert result.exit_code == 0


def test_dry_run_prepared(monkeypatch):
    _patch_chain(monkeypatch)
    runner = CliRunner()
    result = runner.invoke(start, ["alpha", "--dry-run"])
    assert result.exit_code == 0


def test_session_resume_with_resume_id_message(monkeypatch):
    _patch_chain(monkeypatch)
    runner = CliRunner()
    result = runner.invoke(start, ["alpha", "--session", "resume", "--resume", "rid"])
    assert result.exit_code == 0


def test_dry_run_json(monkeypatch):
    _patch_chain(monkeypatch)
    runner = CliRunner()
    result = runner.invoke(start, ["alpha", "--dry-run", "--json"])
    assert result.exit_code == 0
    obj = json.loads(result.output.strip().splitlines()[-1])
    assert obj["status"] == "dry_run_ok"


# NB: a `test_resolve_hostname_runtime_error_uses_empty` test was
# attempted here by a coverage agent, but it pinned an implementation
# detail (which of the three `resolve_hostname()` call sites catches
# the raise) rather than user-visible behaviour. Removed during the
# autonomous coverage pass because it was brittle without adding
# meaningful regression value.


def test_bulk_dir_without_yes_refuses(tmp_path, monkeypatch):
    agents_dir = tmp_path / "agents"
    (agents_dir / "a").mkdir(parents=True)
    (agents_dir / "a" / "a.yaml").write_text("x")
    (agents_dir / "b").mkdir()
    (agents_dir / "b" / "b.yaml").write_text("x")

    runner = CliRunner()
    result = runner.invoke(start, [str(agents_dir)])
    assert result.exit_code == 2
    assert "Refusing to start" in result.output


def test_bulk_dir_with_yes_runs_each(tmp_path, monkeypatch):
    agents_dir = tmp_path / "agents"
    (agents_dir / "a").mkdir(parents=True)
    (agents_dir / "a" / "a.yaml").write_text("x")
    (agents_dir / "b").mkdir()
    (agents_dir / "b" / "b.yaml").write_text("x")

    starts = []
    monkeypatch.setattr(
        "scitex_agent_container.cli_pkg.lifecycle._start.load_config",
        lambda p: _cfg(name=Path(p).stem),
    )
    monkeypatch.setattr(
        "scitex_agent_container.cli_pkg.lifecycle._start.resolve_hostname",
        lambda: "local",
    )
    monkeypatch.setattr(
        "scitex_agent_container.cli_pkg.lifecycle._start._singleton_skip_reason",
        lambda c, h: None,
    )
    monkeypatch.setattr(
        "scitex_agent_container.cli_pkg.lifecycle._start.agent_start",
        lambda *a, **kw: starts.append(a[0]) or True,
    )
    runner = CliRunner()
    result = runner.invoke(start, [str(agents_dir), "-y"])
    assert result.exit_code == 0, result.output
    assert len(starts) == 2


def test_bulk_dir_skip_singleton(tmp_path, monkeypatch):
    agents_dir = tmp_path / "agents"
    (agents_dir / "a").mkdir(parents=True)
    (agents_dir / "a" / "a.yaml").write_text("x")

    monkeypatch.setattr(
        "scitex_agent_container.cli_pkg.lifecycle._start.load_config",
        lambda p: _cfg(name="a"),
    )
    monkeypatch.setattr(
        "scitex_agent_container.cli_pkg.lifecycle._start.resolve_hostname",
        lambda: "local",
    )
    monkeypatch.setattr(
        "scitex_agent_container.cli_pkg.lifecycle._start._singleton_skip_reason",
        lambda c, h: "wrong host",
    )
    started = []
    monkeypatch.setattr(
        "scitex_agent_container.cli_pkg.lifecycle._start.agent_start",
        lambda *a, **kw: started.append(1) or True,
    )
    runner = CliRunner()
    result = runner.invoke(start, [str(agents_dir), "-y"])
    assert result.exit_code == 0
    assert "SKIP" in result.output
    assert not started


def test_bulk_dir_load_failure_continues(tmp_path, monkeypatch):
    agents_dir = tmp_path / "agents"
    (agents_dir / "a").mkdir(parents=True)
    (agents_dir / "a" / "a.yaml").write_text("x")
    (agents_dir / "b").mkdir()
    (agents_dir / "b" / "b.yaml").write_text("x")

    def fake_load(p):
        if "a.yaml" in p:
            raise ValueError("parse fail")
        return _cfg(name="b")

    monkeypatch.setattr(
        "scitex_agent_container.cli_pkg.lifecycle._start.load_config", fake_load
    )
    monkeypatch.setattr(
        "scitex_agent_container.cli_pkg.lifecycle._start.resolve_hostname",
        lambda: "local",
    )
    monkeypatch.setattr(
        "scitex_agent_container.cli_pkg.lifecycle._start._singleton_skip_reason",
        lambda c, h: None,
    )
    monkeypatch.setattr(
        "scitex_agent_container.cli_pkg.lifecycle._start.agent_start",
        lambda *a, **kw: True,
    )
    runner = CliRunner()
    result = runner.invoke(start, [str(agents_dir), "-y"])
    assert result.exit_code == 0
    assert "FAILED" in result.output


def test_bulk_dir_resolve_hostname_runtime_error(tmp_path, monkeypatch):
    agents_dir = tmp_path / "agents"
    (agents_dir / "a").mkdir(parents=True)
    (agents_dir / "a" / "a.yaml").write_text("x")

    def hostfail():
        raise RuntimeError("no host")

    monkeypatch.setattr(
        "scitex_agent_container.cli_pkg.lifecycle._start.resolve_hostname", hostfail
    )
    monkeypatch.setattr(
        "scitex_agent_container.cli_pkg.lifecycle._start.load_config",
        lambda p: _cfg(name="a"),
    )
    monkeypatch.setattr(
        "scitex_agent_container.cli_pkg.lifecycle._start._singleton_skip_reason",
        lambda c, h: None,
    )
    monkeypatch.setattr(
        "scitex_agent_container.cli_pkg.lifecycle._start.agent_start",
        lambda *a, **kw: True,
    )
    runner = CliRunner()
    result = runner.invoke(start, [str(agents_dir), "-y"])
    assert result.exit_code == 0


def test_bulk_dir_empty_prints_no_agents(tmp_path, monkeypatch):
    agents_dir = tmp_path / "agents"
    agents_dir.mkdir()
    runner = CliRunner()
    result = runner.invoke(start, [str(agents_dir), "-y"])
    # is_bulk False since no yamls found; treated as single-target → "alpha" path
    # Actually directory exists but no yamls → bulk_yamls_from_dirs empty, single_targets also empty → no work.
    # Click would error on required args? "required=True, nargs=-1" allows.
    # Actually it iterates targets; the dir produced no yamls; single_targets empty → exit 0 with no output.
    assert result.exit_code == 0


def test_dir_plus_single_target(tmp_path, monkeypatch):
    agents_dir = tmp_path / "agents"
    (agents_dir / "x").mkdir(parents=True)
    (agents_dir / "x" / "x.yaml").write_text("x")
    starts = []
    monkeypatch.setattr(
        "scitex_agent_container.cli_pkg.lifecycle._start.load_config",
        lambda p: _cfg(name=Path(p).stem if "/" in p else "alpha"),
    )
    monkeypatch.setattr(
        "scitex_agent_container.cli_pkg.lifecycle._start.resolve_with_prefix",
        lambda t: f"/fake/{t}.yaml",
    )
    monkeypatch.setattr(
        "scitex_agent_container.cli_pkg.lifecycle._start.resolve_hostname",
        lambda: "local",
    )
    monkeypatch.setattr(
        "scitex_agent_container.cli_pkg.lifecycle._start._singleton_skip_reason",
        lambda c, h: None,
    )
    monkeypatch.setattr(
        "scitex_agent_container.cli_pkg.lifecycle._start.agent_start",
        lambda *a, **kw: starts.append(a[0]) or True,
    )
    runner = CliRunner()
    result = runner.invoke(start, [str(agents_dir), "alpha", "-y"])
    assert result.exit_code == 0
    # 1 from dir + 1 from single
    assert len(starts) == 2


def test_resume_with_bulk_dir_errors(tmp_path):
    agents_dir = tmp_path / "agents"
    (agents_dir / "x").mkdir(parents=True)
    (agents_dir / "x" / "x.yaml").write_text("x")
    runner = CliRunner()
    result = runner.invoke(start, [str(agents_dir), "--resume", "abc", "-y"])
    assert result.exit_code == 2
    assert "cannot be combined with directory" in result.output


def test_multi_foreground_runs_multiplexer(monkeypatch):
    """multiple targets + --foreground → multiplexer invoked, foreground disabled per-runtime."""
    cfg = _cfg()
    monkeypatch.setattr(
        "scitex_agent_container.cli_pkg.lifecycle._start.resolve_with_prefix",
        lambda t: f"/fake/{t}.yaml",
    )
    monkeypatch.setattr(
        "scitex_agent_container.cli_pkg.lifecycle._start.load_config", lambda p: cfg
    )
    monkeypatch.setattr(
        "scitex_agent_container.cli_pkg.lifecycle._start.resolve_hostname",
        lambda: "local",
    )
    monkeypatch.setattr(
        "scitex_agent_container.cli_pkg.lifecycle._start._singleton_skip_reason",
        lambda c, h: None,
    )
    fg_seen = []
    monkeypatch.setattr(
        "scitex_agent_container.cli_pkg.lifecycle._start.agent_start",
        lambda *a, **kw: fg_seen.append(kw["foreground"]) or True,
    )
    multiplex_calls = []
    monkeypatch.setattr(
        "scitex_agent_container.cli_pkg.lifecycle._start._multiplex_foreground_tails",
        lambda names: multiplex_calls.append(names),
    )
    runner = CliRunner()
    result = runner.invoke(start, ["alpha", "beta", "--foreground"])
    assert result.exit_code == 0, result.output
    assert fg_seen == [False, False]
    assert multiplex_calls == [["alpha", "beta"]]


# ---------------------------------------------------------------------------
# params-file expansion
# ---------------------------------------------------------------------------


def test_params_file_needs_single_target(tmp_path):
    csv = tmp_path / "p.csv"
    csv.write_text("name\n")
    runner = CliRunner()
    result = runner.invoke(start, ["a", "b", "--params-file", str(csv)])
    assert result.exit_code == 2
    assert "exactly one TARGET" in result.output


def test_params_file_template_missing(tmp_path):
    csv = tmp_path / "p.csv"
    csv.write_text("name\n")
    runner = CliRunner()
    result = runner.invoke(start, ["no-such-template", "--params-file", str(csv)])
    assert result.exit_code == 2
    assert "template not found" in result.output


def test_params_file_expands_and_starts(tmp_path, monkeypatch):
    template = tmp_path / "tpl.yaml"
    template.write_text("name: ${name}\n")
    csv = tmp_path / "p.csv"
    csv.write_text("name\nfoo\nbar\n")

    materialised = [tmp_path / "out" / "foo.yaml", tmp_path / "out" / "bar.yaml"]
    for m in materialised:
        m.parent.mkdir(exist_ok=True)
        m.write_text("x")
    monkeypatch.setattr(
        "scitex_agent_container._state.fleet_template.expand_params_file",
        lambda *a, **kw: materialised,
    )
    monkeypatch.setattr(
        "scitex_agent_container.cli_pkg.lifecycle._start.load_config",
        lambda p: _cfg(name=Path(p).stem),
    )
    monkeypatch.setattr(
        "scitex_agent_container.cli_pkg.lifecycle._start.resolve_with_prefix",
        lambda t: t,
    )
    monkeypatch.setattr(
        "scitex_agent_container.cli_pkg.lifecycle._start.resolve_hostname",
        lambda: "local",
    )
    monkeypatch.setattr(
        "scitex_agent_container.cli_pkg.lifecycle._start._singleton_skip_reason",
        lambda c, h: None,
    )
    starts = []
    monkeypatch.setattr(
        "scitex_agent_container.cli_pkg.lifecycle._start.agent_start",
        lambda *a, **kw: starts.append(a[0]) or True,
    )
    monkeypatch.setattr(
        "scitex_agent_container.cli_pkg.lifecycle._start._multiplex_foreground_tails",
        lambda names: None,
    )
    runner = CliRunner()
    result = runner.invoke(start, [str(template), "--params-file", str(csv)])
    assert result.exit_code == 0, result.output
    assert len(starts) == 2


def test_params_file_expand_error(tmp_path, monkeypatch):
    template = tmp_path / "tpl.yaml"
    template.write_text("name: ${name}\n")
    csv = tmp_path / "p.csv"
    csv.write_text("bad\n")
    monkeypatch.setattr(
        "scitex_agent_container._state.fleet_template.expand_params_file",
        lambda *a, **kw: (_ for _ in ()).throw(ValueError("missing name col")),
    )
    runner = CliRunner()
    result = runner.invoke(start, [str(template), "--params-file", str(csv)])
    assert result.exit_code == 2
    assert "missing name col" in result.output
