"""Tests for cli_pkg.build_cmds (check, validate)."""

from __future__ import annotations

from types import SimpleNamespace

from click.testing import CliRunner

import scitex_agent_container.cli_pkg.build_cmds as build_cmds
from scitex_agent_container.cli_pkg.build_cmds import check, validate

# ---------------------------------------------------------------------------
# validate
# ---------------------------------------------------------------------------


def test_validate_resolve_error_exits_1(monkeypatch):
    def boom(_):
        raise FileNotFoundError("no such")

    monkeypatch.setattr(build_cmds, "resolve_config", boom)
    runner = CliRunner()
    result = runner.invoke(validate, ["ghost"])
    assert result.exit_code == 1
    assert "no such" in result.output


def test_validate_ok(monkeypatch, tmp_path):
    p = tmp_path / "spec.yaml"
    p.write_text("")
    monkeypatch.setattr(build_cmds, "resolve_config", lambda _: p)
    monkeypatch.setattr(build_cmds, "validate_config", lambda _: [])
    runner = CliRunner()
    result = runner.invoke(validate, ["whatever"])
    assert result.exit_code == 0
    assert "valid" in result.output


def test_validate_errors_exits_1(monkeypatch, tmp_path):
    p = tmp_path / "spec.yaml"
    p.write_text("")
    monkeypatch.setattr(build_cmds, "resolve_config", lambda _: p)
    monkeypatch.setattr(build_cmds, "validate_config", lambda _: ["err1", "err2"])
    runner = CliRunner()
    result = runner.invoke(validate, ["x"])
    assert result.exit_code == 1
    assert "err1" in result.output
    assert "err2" in result.output


# ---------------------------------------------------------------------------
# check
# ---------------------------------------------------------------------------


def _cfg(name="x", runtime="apptainer"):
    return SimpleNamespace(name=name, runtime=runtime)


def test_check_resolve_error_exits_1(monkeypatch):
    monkeypatch.setattr(
        build_cmds,
        "resolve_config",
        lambda _: (_ for _ in ()).throw(RuntimeError("nope")),
    )
    runner = CliRunner()
    result = runner.invoke(check, ["x"])
    assert result.exit_code == 1
    assert "nope" in result.output


def test_check_validation_error_exits_1(monkeypatch, tmp_path):
    p = tmp_path / "spec.yaml"
    p.write_text("")
    monkeypatch.setattr(build_cmds, "resolve_config", lambda _: p)
    monkeypatch.setattr(build_cmds, "validate_config", lambda _: ["bad"])
    runner = CliRunner()
    result = runner.invoke(check, ["x"])
    assert result.exit_code == 1
    assert "bad" in result.output


def test_check_load_config_error_exits_1(monkeypatch, tmp_path):
    p = tmp_path / "spec.yaml"
    p.write_text("")
    monkeypatch.setattr(build_cmds, "resolve_config", lambda _: p)
    monkeypatch.setattr(build_cmds, "validate_config", lambda _: [])

    def boom(_):
        raise ValueError("can't load")

    monkeypatch.setattr(build_cmds, "load_config", boom)
    runner = CliRunner()
    result = runner.invoke(check, ["x"])
    assert result.exit_code == 1
    assert "can't load" in result.output


def test_check_all_ok(monkeypatch, tmp_path):
    p = tmp_path / "spec.yaml"
    p.write_text("")
    monkeypatch.setattr(build_cmds, "resolve_config", lambda _: p)
    monkeypatch.setattr(build_cmds, "validate_config", lambda _: [])
    monkeypatch.setattr(build_cmds, "load_config", lambda _: _cfg())
    monkeypatch.setattr(build_cmds.shutil, "which", lambda b: f"/usr/bin/{b}")

    class _Proc:
        returncode = 0
        stdout = "Python 3.11.0"

    monkeypatch.setattr(build_cmds.subprocess, "run", lambda *a, **kw: _Proc())

    runner = CliRunner()
    result = runner.invoke(check, ["x"])
    assert result.exit_code == 0
    assert "Ready to deploy" in result.output


def test_check_apptainer_missing_fails(monkeypatch, tmp_path):
    p = tmp_path / "spec.yaml"
    p.write_text("")
    monkeypatch.setattr(build_cmds, "resolve_config", lambda _: p)
    monkeypatch.setattr(build_cmds, "validate_config", lambda _: [])
    monkeypatch.setattr(build_cmds, "load_config", lambda _: _cfg())
    monkeypatch.setattr(build_cmds.shutil, "which", lambda b: None)

    class _Proc:
        returncode = 0
        stdout = "Python 3.11"

    monkeypatch.setattr(build_cmds.subprocess, "run", lambda *a, **kw: _Proc())

    runner = CliRunner()
    result = runner.invoke(check, ["x"])
    assert result.exit_code == 1
    assert "FAIL" in result.output


def test_check_python_failure_marks_fail(monkeypatch, tmp_path):
    p = tmp_path / "spec.yaml"
    p.write_text("")
    monkeypatch.setattr(build_cmds, "resolve_config", lambda _: p)
    monkeypatch.setattr(build_cmds, "validate_config", lambda _: [])
    monkeypatch.setattr(build_cmds, "load_config", lambda _: _cfg())
    monkeypatch.setattr(build_cmds.shutil, "which", lambda b: f"/bin/{b}")

    class _Proc:
        returncode = 1
        stdout = ""

    monkeypatch.setattr(build_cmds.subprocess, "run", lambda *a, **kw: _Proc())

    runner = CliRunner()
    result = runner.invoke(check, ["x"])
    assert result.exit_code == 1


def test_check_python_not_found_marks_fail(monkeypatch, tmp_path):
    p = tmp_path / "spec.yaml"
    p.write_text("")
    monkeypatch.setattr(build_cmds, "resolve_config", lambda _: p)
    monkeypatch.setattr(build_cmds, "validate_config", lambda _: [])
    monkeypatch.setattr(build_cmds, "load_config", lambda _: _cfg())
    monkeypatch.setattr(build_cmds.shutil, "which", lambda b: f"/bin/{b}")

    def raise_fnf(*a, **kw):
        raise FileNotFoundError("python3 missing")

    monkeypatch.setattr(build_cmds.subprocess, "run", raise_fnf)

    runner = CliRunner()
    result = runner.invoke(check, ["x"])
    assert result.exit_code == 1
    assert "python3 not found" in result.output


def test_check_runtime_defaults_to_apptainer_when_empty(monkeypatch, tmp_path):
    """An empty `spec.runtime` is treated as apptainer."""
    p = tmp_path / "spec.yaml"
    p.write_text("")
    monkeypatch.setattr(build_cmds, "resolve_config", lambda _: p)
    monkeypatch.setattr(build_cmds, "validate_config", lambda _: [])
    monkeypatch.setattr(build_cmds, "load_config", lambda _: _cfg(runtime=""))

    seen_bins = []

    def fake_which(b):
        seen_bins.append(b)
        return f"/bin/{b}"

    monkeypatch.setattr(build_cmds.shutil, "which", fake_which)

    class _Proc:
        returncode = 0
        stdout = "Python 3.11"

    monkeypatch.setattr(build_cmds.subprocess, "run", lambda *a, **kw: _Proc())

    runner = CliRunner()
    result = runner.invoke(check, ["x"])
    assert result.exit_code == 0
    assert "apptainer" in seen_bins
