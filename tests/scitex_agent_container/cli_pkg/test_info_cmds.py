"""Tests for cli_pkg.info_cmds (find, tail, list-python-apis)."""

from __future__ import annotations

import json
from pathlib import Path

from click.testing import CliRunner

import scitex_agent_container.cli_pkg.info_cmds as info_cmds
from scitex_agent_container.cli_pkg.info_cmds import (
    _tail_one,
    find,
    list_python_apis,
    tail_session,
)


def _write_spec_dir(tmp_path: Path, name: str, caps: str = "HPC,GPU") -> Path:
    d = tmp_path / name
    d.mkdir()
    spec = d / "spec.yaml"
    spec.write_text(
        "apiVersion: scitex-agent-container/v3\n"
        "kind: Agent\n"
        "metadata:\n"
        f"  labels:\n    capabilities: '{caps}'\n    machine: m1\n"
        "spec:\n  runtime: apptainer\n"
    )
    return spec


# ---------------------------------------------------------------------------
# find
# ---------------------------------------------------------------------------


def test_find_no_matches_human(tmp_path):
    _write_spec_dir(tmp_path, "x", caps="other-thing")
    runner = CliRunner()
    result = runner.invoke(find, ["HPC", "--dir", str(tmp_path)])
    assert result.exit_code == 0
    assert "No agents" in result.output


def test_find_json_returns_matches(tmp_path):
    _write_spec_dir(tmp_path, "alpha", caps="HPC,GPU")
    _write_spec_dir(tmp_path, "beta", caps="GPU")
    runner = CliRunner()
    result = runner.invoke(find, ["HPC", "--dir", str(tmp_path), "--json"])
    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    names = [m["name"] for m in data]
    assert "alpha" in names
    assert "beta" not in names


def test_find_table_output(tmp_path):
    _write_spec_dir(tmp_path, "alpha", caps="HPC")
    runner = CliRunner()
    result = runner.invoke(find, ["HPC", "--dir", str(tmp_path)])
    assert result.exit_code == 0
    assert "alpha" in result.output
    assert "HPC" in result.output


def test_find_skips_invalid_yaml(tmp_path, monkeypatch):
    _write_spec_dir(tmp_path, "ok", caps="HPC")
    bad = tmp_path / "broken"
    bad.mkdir()
    (bad / "spec.yaml").write_text("not: valid: yaml: ---")

    runner = CliRunner()
    result = runner.invoke(find, ["HPC", "--dir", str(tmp_path), "--json"])
    assert result.exit_code == 0
    data = json.loads(result.output)
    # Bad config skipped; the good one still resolves.
    names = [m["name"] for m in data]
    assert "ok" in names


def test_find_default_dir_is_cwd(tmp_path, monkeypatch):
    _write_spec_dir(tmp_path, "ad", caps="X")
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    result = runner.invoke(find, ["X", "--json"])
    assert result.exit_code == 0
    data = json.loads(result.output)
    assert any(m["name"] == "ad" for m in data)


def test_find_search_path_not_a_dir(tmp_path):
    p = tmp_path / "not-a-dir"
    p.write_text("file")
    runner = CliRunner()
    result = runner.invoke(find, ["X", "--dir", str(p), "--json"])
    assert result.exit_code == 0
    assert json.loads(result.output) == []


# ---------------------------------------------------------------------------
# tail / _tail_one
# ---------------------------------------------------------------------------


def test_tail_one_missing_in_registry(monkeypatch, capsys):
    from scitex_agent_container._state.registry import Registry as _R

    class FakeReg:
        def get(self, name):
            return None

    monkeypatch.setattr(_R, "__init__", lambda self: None)
    monkeypatch.setattr(_R, "get", lambda self, name: None)
    assert (
        _tail_one("ghost", lines=5, show_tools=False, as_json=False, prefix=False)
        is False
    )


def test_tail_one_no_transcript_file(monkeypatch, tmp_path):
    from scitex_agent_container._state.registry import Registry as _R

    monkeypatch.setattr(_R, "__init__", lambda self: None)
    monkeypatch.setattr(_R, "get", lambda self, name: {"name": name})
    monkeypatch.setattr(info_cmds.Path, "home", classmethod(lambda cls: tmp_path))

    assert (
        _tail_one("absent", lines=5, show_tools=False, as_json=False, prefix=False)
        is False
    )


def _build_transcript(tmp_path: Path, name: str, records: list[dict]) -> None:
    state_dir = tmp_path / ".scitex" / "agent-container" / "runtime" / name
    state_dir.mkdir(parents=True)
    transcript = state_dir / "session.jsonl"
    transcript.write_text("\n".join(json.dumps(r) for r in records) + "\n")


def test_tail_one_renders_records(monkeypatch, tmp_path, capsys):
    from scitex_agent_container._state.registry import Registry as _R

    monkeypatch.setattr(_R, "__init__", lambda self: None)
    monkeypatch.setattr(_R, "get", lambda self, name: {"name": name})
    monkeypatch.setattr(info_cmds.Path, "home", classmethod(lambda cls: tmp_path))

    _build_transcript(
        tmp_path,
        "ag",
        [
            {"type": "assistant", "text": "hello world"},
            {
                "type": "result",
                "session_id": "abcdefgh12345",
                "usage": {
                    "input_tokens": 10,
                    "output_tokens": 5,
                    "cache_creation_input_tokens": 1,
                    "cache_read_input_tokens": 2,
                },
            },
            {"type": "error", "kind": "ToolError", "detail": "x"},
            {"type": "user_echo", "raw": "tool result raw"},
        ],
    )

    ok = _tail_one("ag", lines=10, show_tools=True, as_json=False, prefix=False)
    assert ok is True


def test_tail_one_as_json(monkeypatch, tmp_path, capsys):
    from scitex_agent_container._state.registry import Registry as _R

    monkeypatch.setattr(_R, "__init__", lambda self: None)
    monkeypatch.setattr(_R, "get", lambda self, name: {"name": name})
    monkeypatch.setattr(info_cmds.Path, "home", classmethod(lambda cls: tmp_path))

    _build_transcript(
        tmp_path, "ag", [{"type": "assistant", "text": "hi"}, {"bad": "no-type"}]
    )

    ok = _tail_one("ag", lines=10, show_tools=False, as_json=True, prefix=False)
    assert ok is True


def test_tail_skips_invalid_json_lines(monkeypatch, tmp_path):
    """A malformed JSONL line must be silently skipped, not crash the tail."""
    from scitex_agent_container._state.registry import Registry as _R

    monkeypatch.setattr(_R, "__init__", lambda self: None)
    monkeypatch.setattr(_R, "get", lambda self, name: {"name": name})
    monkeypatch.setattr(info_cmds.Path, "home", classmethod(lambda cls: tmp_path))

    state_dir = tmp_path / ".scitex" / "agent-container" / "runtime" / "ag"
    state_dir.mkdir(parents=True)
    (state_dir / "session.jsonl").write_text(
        json.dumps({"type": "assistant", "text": "real"}) + "\nnot valid json line\n"
    )

    ok = _tail_one("ag", lines=10, show_tools=False, as_json=False, prefix=True)
    assert ok is True


def test_tail_session_aggregates_exit_status(monkeypatch, tmp_path):
    """tail-session sums per-agent failure into exit code 1."""
    from scitex_agent_container._state.registry import Registry as _R

    monkeypatch.setattr(_R, "__init__", lambda self: None)
    monkeypatch.setattr(_R, "get", lambda self, name: None)  # all missing
    runner = CliRunner()
    result = runner.invoke(tail_session, ["a", "b"])
    assert result.exit_code == 1


# ---------------------------------------------------------------------------
# list-python-apis
# ---------------------------------------------------------------------------


def test_list_python_apis_json(monkeypatch):
    monkeypatch.setattr(
        info_cmds,
        "get_api_tree",
        lambda mod, max_depth, docstring: [
            {
                "Name": "scitex_agent_container",
                "Type": "M",
                "Depth": 0,
                "Docstring": "doc",
            },
        ],
    )
    runner = CliRunner()
    result = runner.invoke(list_python_apis, ["--json"])
    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert data[0]["Name"] == "scitex_agent_container"


def test_list_python_apis_human_default(monkeypatch):
    monkeypatch.setattr(
        info_cmds,
        "get_api_tree",
        lambda mod, max_depth, docstring: [
            {
                "Name": "scitex_agent_container.cli",
                "Type": "M",
                "Depth": 1,
                "Docstring": "",
            },
            {
                "Name": "scitex_agent_container.cli.main",
                "Type": "F",
                "Depth": 2,
                "Docstring": "Run.",
            },
            {
                "Name": "scitex_agent_container.X",
                "Type": "V",
                "Depth": 1,
                "Docstring": "",
            },
        ],
    )
    runner = CliRunner()
    result = runner.invoke(list_python_apis, [])
    assert result.exit_code == 0, result.output
    # Verbose=0 prints type tags and names
    assert "[M] cli" in result.output
    assert "[F] main" in result.output
    assert "[V] X" in result.output


def test_list_python_apis_verbose_truncates_docstring(monkeypatch):
    monkeypatch.setattr(
        info_cmds,
        "get_api_tree",
        lambda mod, max_depth, docstring: [
            {
                "Name": "scitex_agent_container.foo",
                "Type": "F",
                "Depth": 1,
                "Docstring": "First line.\nSecond line.",
            },
        ],
    )
    runner = CliRunner()
    result = runner.invoke(list_python_apis, ["-v"])
    assert result.exit_code == 0
    # -v keeps only the first line of the docstring.
    assert "First line." in result.output


def test_list_python_apis_double_verbose_full(monkeypatch):
    monkeypatch.setattr(
        info_cmds,
        "get_api_tree",
        lambda mod, max_depth, docstring: [
            {
                "Name": "scitex_agent_container.foo",
                "Type": "F",
                "Depth": 1,
                "Docstring": "Line A\nLine B",
            },
        ],
    )
    runner = CliRunner()
    result = runner.invoke(list_python_apis, ["-vv"])
    assert result.exit_code == 0
    assert "Line A" in result.output
    assert "Line B" in result.output
