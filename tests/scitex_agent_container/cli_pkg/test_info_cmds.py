"""Tests for ``cli_pkg.info_cmds`` (find, tail, list-python-apis).

PA-306 no-mocks rewrite. The previous version monkeypatched
``Registry``, ``Path.home``, ``get_api_tree``, and ``chdir``. This
version exercises real production collaborators:

* ``Registry`` is redirected to ``tmp_path`` via the documented
  ``SCITEX_AGENT_CONTAINER_REGISTRY_DIR`` env var; the module is
  reloaded so the module-level ``REGISTRY_DIR`` picks up the new
  value. ``info_cmds`` imports ``Registry`` lazily inside
  ``_tail_one``, so the reload is honoured without a re-bind. Real
  reload, no monkeypatch.
* ``Path.home()`` is redirected by setting ``HOME`` via the shared
  ``env_save_restore`` fixture. ``pathlib.Path.home()`` is documented
  to honour ``$HOME`` on POSIX -- a real env-based seam.
* ``list-python-apis`` runs ``get_api_tree`` against the *real*
  ``scitex_agent_container`` module -- no callable injection. Tests
  assert against shape invariants of the real public API (top-level
  module entry exists, type tags + indentation are formatted, verbose
  docstring slicing is applied).
* The ``find --dir DEFAULT`` test changes cwd by hand (real ``os.chdir``)
  with try/finally restore -- no ``monkeypatch.chdir``.
"""

from __future__ import annotations

import importlib
import json
import os
from pathlib import Path

import pytest
from click.testing import CliRunner

from scitex_agent_container.cli_pkg.info_cmds import (
    _tail_one,
    find,
    list_python_apis,
    tail_session,
)

# ---------------------------------------------------------------------------
# Real-collaborator fixtures
# ---------------------------------------------------------------------------


def _write_spec_dir(tmp_path: Path, name: str, caps: str = "HPC,GPU") -> Path:
    """Write a v3-shaped spec.yaml at ``<tmp_path>/<name>/spec.yaml``."""
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


@pytest.fixture
def tmp_registry(tmp_path, env_save_restore):
    """Redirect the file-backed registry to ``tmp_path / registry``.

    Reloads ``_state.registry`` so its module-level ``REGISTRY_DIR``
    picks up the env var. ``info_cmds`` imports ``Registry`` lazily,
    so the reload is honoured automatically.
    """
    reg = tmp_path / "registry"
    reg.mkdir()
    env_save_restore.set("SCITEX_AGENT_CONTAINER_REGISTRY_DIR", str(reg))
    import scitex_agent_container._state.registry as _reg

    importlib.reload(_reg)
    yield reg


@pytest.fixture
def tmp_home(tmp_path, env_save_restore):
    """Redirect ``Path.home()`` to ``tmp_path`` via ``$HOME``.

    On POSIX, ``pathlib.Path.home()`` honours ``$HOME``. This is the
    real env-based seam -- no monkeypatch on ``Path``.
    """
    env_save_restore.set("HOME", str(tmp_path))
    return tmp_path


def _register(reg_dir: Path, name: str) -> None:
    """Write a minimal real registry JSON entry for ``name``."""
    (reg_dir / f"{name}.json").write_text(
        json.dumps(
            {
                "name": name,
                "config": "",
                "pid": 1,
                "started_at": "2026-01-01T00:00:00Z",
                "screen": name,
            }
        )
    )


def _build_transcript(home_dir: Path, name: str, records: list[dict]) -> Path:
    """Write a real session.jsonl under ``~/.scitex/agent-container/runtime/<name>/``."""
    state_dir = home_dir / ".scitex" / "agent-container" / "runtime" / name
    state_dir.mkdir(parents=True)
    transcript = state_dir / "session.jsonl"
    transcript.write_text("\n".join(json.dumps(r) for r in records) + "\n")
    return transcript


# ===========================================================================
# find
# ===========================================================================


def test_find_no_matches_human_says_no_agents(tmp_path):
    # Arrange
    _write_spec_dir(tmp_path, "x", caps="other-thing")
    runner = CliRunner()
    # Act
    result = runner.invoke(find, ["HPC", "--dir", str(tmp_path)])
    # Assert
    assert "No agents" in result.output


def test_find_no_matches_human_exits_zero(tmp_path):
    # Arrange
    _write_spec_dir(tmp_path, "x", caps="other-thing")
    runner = CliRunner()
    # Act
    result = runner.invoke(find, ["HPC", "--dir", str(tmp_path)])
    # Assert
    assert result.exit_code == 0


def test_find_json_includes_matching_agent(tmp_path):
    # Arrange
    _write_spec_dir(tmp_path, "alpha", caps="HPC,GPU")
    _write_spec_dir(tmp_path, "beta", caps="GPU")
    runner = CliRunner()
    # Act
    result = runner.invoke(find, ["HPC", "--dir", str(tmp_path), "--json"])
    names = [m["name"] for m in json.loads(result.output)]
    # Assert
    assert "alpha" in names


def test_find_json_excludes_non_matching_agent(tmp_path):
    # Arrange
    _write_spec_dir(tmp_path, "alpha", caps="HPC,GPU")
    _write_spec_dir(tmp_path, "beta", caps="GPU")
    runner = CliRunner()
    # Act
    result = runner.invoke(find, ["HPC", "--dir", str(tmp_path), "--json"])
    names = [m["name"] for m in json.loads(result.output)]
    # Assert
    assert "beta" not in names


def test_find_table_renders_agent_name(tmp_path):
    # Arrange
    _write_spec_dir(tmp_path, "alpha", caps="HPC")
    runner = CliRunner()
    # Act
    result = runner.invoke(find, ["HPC", "--dir", str(tmp_path)])
    # Assert
    assert "alpha" in result.output


def test_find_table_renders_capability_label(tmp_path):
    # Arrange
    _write_spec_dir(tmp_path, "alpha", caps="HPC")
    runner = CliRunner()
    # Act
    result = runner.invoke(find, ["HPC", "--dir", str(tmp_path)])
    # Assert
    assert "HPC" in result.output


def test_find_skips_invalid_yaml_but_includes_valid(tmp_path):
    # Arrange -- one valid spec next to one malformed YAML; the bad
    # config is silently skipped (real ``load_config`` raises, the
    # production ``try/except`` continues).
    _write_spec_dir(tmp_path, "ok", caps="HPC")
    bad = tmp_path / "broken"
    bad.mkdir()
    (bad / "spec.yaml").write_text("not: valid: yaml: ---")
    runner = CliRunner()
    # Act
    result = runner.invoke(find, ["HPC", "--dir", str(tmp_path), "--json"])
    names = [m["name"] for m in json.loads(result.output)]
    # Assert
    assert "ok" in names


def test_find_default_dir_is_cwd(tmp_path):
    # Arrange -- real chdir + try/finally restore (no monkeypatch.chdir).
    _write_spec_dir(tmp_path, "ad", caps="X")
    saved_cwd = os.getcwd()
    os.chdir(tmp_path)
    try:
        runner = CliRunner()
        # Act
        result = runner.invoke(find, ["X", "--json"])
        data = json.loads(result.output)
        # Assert
        assert any(m["name"] == "ad" for m in data)
    finally:
        os.chdir(saved_cwd)


def test_find_search_path_not_a_dir_returns_empty(tmp_path):
    # Arrange -- pointing ``--dir`` at a file instead of a directory.
    p = tmp_path / "not-a-dir"
    p.write_text("file")
    runner = CliRunner()
    # Act
    result = runner.invoke(find, ["X", "--dir", str(p), "--json"])
    # Assert
    assert json.loads(result.output) == []


# ===========================================================================
# _tail_one / tail
# ===========================================================================


def test_tail_one_missing_in_registry_returns_false(tmp_registry):
    # Arrange -- registry is empty (no JSON entry for "ghost").
    # Act
    ok = _tail_one("ghost", lines=5, show_tools=False, as_json=False, prefix=False)
    # Assert
    assert ok is False


def test_tail_one_no_transcript_file_returns_false(tmp_registry, tmp_home):
    # Arrange -- agent is registered but no session.jsonl on disk.
    _register(tmp_registry, "absent")
    # Act
    ok = _tail_one("absent", lines=5, show_tools=False, as_json=False, prefix=False)
    # Assert
    assert ok is False


def test_tail_one_renders_transcript_records_returns_true(tmp_registry, tmp_home):
    # Arrange
    _register(tmp_registry, "ag")
    _build_transcript(
        tmp_home,
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
    # Act
    ok = _tail_one("ag", lines=10, show_tools=True, as_json=False, prefix=False)
    # Assert
    assert ok is True


def test_tail_one_as_json_returns_true(tmp_registry, tmp_home):
    # Arrange
    _register(tmp_registry, "ag")
    _build_transcript(
        tmp_home,
        "ag",
        [{"type": "assistant", "text": "hi"}, {"bad": "no-type"}],
    )
    # Act
    ok = _tail_one("ag", lines=10, show_tools=False, as_json=True, prefix=False)
    # Assert
    assert ok is True


def test_tail_one_skips_invalid_jsonl_lines(tmp_registry, tmp_home):
    """A malformed JSONL line must be silently skipped, not crash."""
    # Arrange
    _register(tmp_registry, "ag")
    state_dir = tmp_home / ".scitex" / "agent-container" / "runtime" / "ag"
    state_dir.mkdir(parents=True)
    (state_dir / "session.jsonl").write_text(
        json.dumps({"type": "assistant", "text": "real"}) + "\nnot valid json line\n"
    )
    # Act
    ok = _tail_one("ag", lines=10, show_tools=False, as_json=False, prefix=True)
    # Assert
    assert ok is True


def test_tail_session_aggregates_exit_status_to_one(tmp_registry):
    """tail-session sums per-agent failure into exit code 1."""
    # Arrange -- empty registry, both names missing -> failure.
    runner = CliRunner()
    # Act
    result = runner.invoke(tail_session, ["a", "b"])
    # Assert
    assert result.exit_code == 1


# ===========================================================================
# list-python-apis -- runs against the REAL scitex_agent_container module
# ===========================================================================


def test_list_python_apis_json_exits_zero():
    # Arrange
    runner = CliRunner()
    # Act -- real get_api_tree walks the real package
    result = runner.invoke(list_python_apis, ["--json", "-d", "1"])
    # Assert
    assert result.exit_code == 0, result.output


def test_list_python_apis_json_includes_package_root():
    # Arrange
    runner = CliRunner()
    # Act
    result = runner.invoke(list_python_apis, ["--json", "-d", "1"])
    data = json.loads(result.output)
    names = [row["Name"] for row in data]
    # Assert -- the real top-level module always appears at depth 0.
    assert "scitex_agent_container" in names


def test_list_python_apis_human_default_renders_type_tag():
    # Arrange
    runner = CliRunner()
    # Act
    result = runner.invoke(list_python_apis, ["-d", "1"])
    # Assert -- at least one ``[M]`` tag appears in the rendered output.
    assert "[M]" in result.output


def test_list_python_apis_human_default_renders_legend():
    # Arrange
    runner = CliRunner()
    # Act
    result = runner.invoke(list_python_apis, ["-d", "1"])
    # Assert
    assert "Legend:" in result.output


def test_list_python_apis_human_default_renders_load_config_function():
    # Arrange -- ``load_config`` is a real public API symbol at depth 1.
    runner = CliRunner()
    # Act
    result = runner.invoke(list_python_apis, ["-d", "1"])
    # Assert -- functions render as ``[F] <name>(<sig>)``.
    assert "[F] load_config" in result.output


def test_list_python_apis_verbose_renders_docstring_first_line():
    # Arrange -- ``load_config``'s real docstring starts with "Load and validate".
    runner = CliRunner()
    # Act
    result = runner.invoke(list_python_apis, ["-v", "-d", "1"])
    # Assert -- -v adds a ``    - <first-doc-line>`` annotation.
    assert "Load and validate" in result.output


def test_list_python_apis_double_verbose_renders_multiple_doc_lines():
    # Arrange -- ``load_config``'s real docstring spans multiple lines
    # ("Load and validate ..." then later "Only ...v3 is accepted.").
    runner = CliRunner()
    # Act
    result = runner.invoke(list_python_apis, ["-vv", "-d", "1"])
    # Assert -- -vv prints every docstring line, not just the first.
    assert "Only" in result.output
