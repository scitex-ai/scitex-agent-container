"""Tests for cli_pkg._helpers._agent_list — listing assembly + presentation."""

from __future__ import annotations

import json
from types import SimpleNamespace

import scitex_agent_container.cli_pkg._helpers._agent_list as _al
from scitex_agent_container.cli_pkg._helpers._agent_list import (
    _discover_defined_agents,
    _extract_damaged_fields,
    _probe_local,
    get_agent_list_data,
    print_agent_list,
    print_agent_list_json,
)

# ---------------------------------------------------------------------------
# _extract_damaged_fields — pure regex extraction
# ---------------------------------------------------------------------------


def test_extract_damaged_fields_collects_unique_fields():
    errors = [
        "spec.runtime is required",
        "spec.runtime cannot be empty",  # dup
        "metadata.name is no longer accepted",
    ]
    out = _extract_damaged_fields(errors)
    assert out == ["spec.runtime", "metadata.name"]


def test_extract_damaged_fields_caps_at_four():
    errors = [f"spec.field_{i} is bad" for i in range(7)]
    out = _extract_damaged_fields(errors)
    # >4 → cap at first three + summary
    assert len(out) == 4
    assert out[-1].startswith("+4 more")


def test_extract_damaged_fields_empty_input():
    assert _extract_damaged_fields([]) == []


def test_extract_damaged_fields_no_matches():
    assert _extract_damaged_fields(["random text", "no fields"]) == []


def test_extract_damaged_fields_dotted_path():
    out = _extract_damaged_fields(["spec.claude.flags is required"])
    assert out == ["spec.claude.flags"]


# ---------------------------------------------------------------------------
# _probe_local — exception → None
# ---------------------------------------------------------------------------


def test_probe_local_returns_runtime_value(monkeypatch):
    class _RT:
        def is_running(self, cfg):
            return True

    monkeypatch.setattr(
        "scitex_agent_container.runtimes.claude_session.ClaudeSessionRuntime",
        lambda: _RT(),
    )
    assert _probe_local(SimpleNamespace()) is True


def test_probe_local_returns_none_on_error(monkeypatch):
    class _RT:
        def is_running(self, cfg):
            raise RuntimeError("nope")

    monkeypatch.setattr(
        "scitex_agent_container.runtimes.claude_session.ClaudeSessionRuntime",
        lambda: _RT(),
    )
    assert _probe_local(SimpleNamespace()) is None


# ---------------------------------------------------------------------------
# get_agent_list_data
# ---------------------------------------------------------------------------


class _FakeRegistry:
    def __init__(self, entries):
        self._entries = entries

    def list_all(self):
        return list(self._entries)


def _cfg(name, runtime="apptainer", labels=None):
    return SimpleNamespace(
        name=name,
        runtime=runtime,
        labels=labels or {},
    )


def test_get_data_empty_registry(monkeypatch):
    monkeypatch.setattr(_al, "_discover_defined_agents", lambda: [])
    out = get_agent_list_data(_FakeRegistry([]))
    assert out == []


def test_get_data_invalid_config_yields_unknown(monkeypatch):
    monkeypatch.setattr(_al, "_discover_defined_agents", lambda: [])
    monkeypatch.setattr(
        _al,
        "load_config",
        lambda p: (_ for _ in ()).throw(ValueError("bad yaml")),
    )
    entries = [{"name": "x", "screen": "s", "started_at": "ts", "config": "/p"}]
    out = get_agent_list_data(_FakeRegistry(entries))
    row = out[0]
    assert row["name"] == "x"
    assert row["status"] == "unknown"
    assert row.get("liveness_unknown") is True


def test_get_data_capability_filter(monkeypatch):
    monkeypatch.setattr(_al, "_discover_defined_agents", lambda: [])
    monkeypatch.setattr(
        _al,
        "load_config",
        lambda p: _cfg("x", labels={"capabilities": "HPC, GPU"}),
    )
    monkeypatch.setattr(_al, "_probe_local", lambda cfg: True)
    # Patch validate_config to return no errors.
    from scitex_agent_container.config import _validation as v

    monkeypatch.setattr(v, "validate_config", lambda p: [])
    entries = [{"name": "x", "screen": "s", "started_at": "ts", "config": "/p"}]
    out = get_agent_list_data(_FakeRegistry(entries), capability="HPC")
    assert len(out) == 1


def test_get_data_capability_filter_rejects(monkeypatch):
    monkeypatch.setattr(_al, "_discover_defined_agents", lambda: [])
    monkeypatch.setattr(
        _al, "load_config", lambda p: _cfg("x", labels={"capabilities": "GPU"})
    )
    entries = [{"name": "x", "config": "/p"}]
    out = get_agent_list_data(_FakeRegistry(entries), capability="HPC")
    assert out == []


def test_get_data_machine_filter_rejects(monkeypatch):
    monkeypatch.setattr(_al, "_discover_defined_agents", lambda: [])
    monkeypatch.setattr(
        _al, "load_config", lambda p: _cfg("x", labels={"machine": "m2"})
    )
    entries = [{"name": "x", "config": "/p"}]
    out = get_agent_list_data(_FakeRegistry(entries), machine="m1")
    assert out == []


def test_get_data_running_when_probe_true(monkeypatch):
    monkeypatch.setattr(_al, "_discover_defined_agents", lambda: [])
    monkeypatch.setattr(_al, "load_config", lambda p: _cfg("x"))
    # Probe fn is resolved dynamically via the parent pkg — patch there too.
    import scitex_agent_container.cli_pkg._helpers as _h

    monkeypatch.setattr(_h, "_probe_local", lambda cfg: True, raising=False)
    monkeypatch.setattr(_al, "_probe_local", lambda cfg: True)
    from scitex_agent_container.config import _validation as v

    monkeypatch.setattr(v, "validate_config", lambda p: [])
    entries = [{"name": "x", "config": "/p", "screen": "-", "started_at": "-"}]
    out = get_agent_list_data(_FakeRegistry(entries))
    assert out[0]["status"] == "running"


def test_get_data_stopped_when_probe_false(monkeypatch):
    monkeypatch.setattr(_al, "_discover_defined_agents", lambda: [])
    monkeypatch.setattr(_al, "load_config", lambda p: _cfg("x"))
    monkeypatch.setattr(_al, "_probe_local", lambda cfg: False)
    from scitex_agent_container.config import _validation as v

    monkeypatch.setattr(v, "validate_config", lambda p: [])
    entries = [{"name": "x", "config": "/p"}]
    out = get_agent_list_data(_FakeRegistry(entries))
    assert out[0]["status"] == "stopped"


def test_get_data_probe_unknown_when_probe_none(monkeypatch):
    monkeypatch.setattr(_al, "_discover_defined_agents", lambda: [])
    monkeypatch.setattr(_al, "load_config", lambda p: _cfg("x"))
    import scitex_agent_container.cli_pkg._helpers as _h

    monkeypatch.setattr(_h, "_probe_local", lambda cfg: None, raising=False)
    monkeypatch.setattr(_al, "_probe_local", lambda cfg: None)
    from scitex_agent_container.config import _validation as v

    monkeypatch.setattr(v, "validate_config", lambda p: [])
    entries = [{"name": "x", "config": "/p"}]
    out = get_agent_list_data(_FakeRegistry(entries))
    assert out[0]["status"] == "unknown"
    assert out[0].get("liveness_unknown") is True


def test_get_data_merges_defined_agents(monkeypatch, tmp_path):
    """An agent on disk but absent from registry shows as 'defined'."""
    spec = tmp_path / "ondisk" / "spec.yaml"
    spec.parent.mkdir()
    spec.write_text(
        "apiVersion: scitex-agent-container/v3\nkind: Agent\nspec:\n  runtime: apptainer\n"
    )

    monkeypatch.setattr(_al, "_discover_defined_agents", lambda: [("ondisk", spec)])
    out = get_agent_list_data(_FakeRegistry([]))
    names = [r["name"] for r in out]
    assert "ondisk" in names
    ondisk_row = next(r for r in out if r["name"] == "ondisk")
    assert ondisk_row["status"] in ("defined", "invalid")


def test_get_data_invalid_yaml_marks_invalid(monkeypatch, tmp_path):
    bad = tmp_path / "bad" / "spec.yaml"
    bad.parent.mkdir()
    bad.write_text("not: valid: yaml: ---")
    monkeypatch.setattr(_al, "_discover_defined_agents", lambda: [("bad", bad)])
    out = get_agent_list_data(_FakeRegistry([]))
    row = next(r for r in out if r["name"] == "bad")
    assert row["status"] == "invalid"
    assert row["validation_errors"]


# ---------------------------------------------------------------------------
# print_agent_list (table) + print_agent_list_json
# ---------------------------------------------------------------------------


def test_print_agent_list_empty(monkeypatch, capsys):
    monkeypatch.setattr(_al, "_discover_defined_agents", lambda: [])
    print_agent_list(_FakeRegistry([]))
    out = capsys.readouterr().out
    assert "No agents found" in out


def test_print_agent_list_renders_rows(monkeypatch, capsys):
    monkeypatch.setattr(_al, "_discover_defined_agents", lambda: [])
    monkeypatch.setattr(_al, "load_config", lambda p: _cfg("x"))
    import scitex_agent_container.cli_pkg._helpers as _h

    monkeypatch.setattr(_h, "_probe_local", lambda cfg: True, raising=False)
    monkeypatch.setattr(_al, "_probe_local", lambda cfg: True)
    from scitex_agent_container.config import _validation as v

    monkeypatch.setattr(v, "validate_config", lambda p: [])
    entries = [{"name": "x", "config": "/p", "screen": "s", "started_at": "ts"}]
    print_agent_list(_FakeRegistry(entries))
    out = capsys.readouterr().out
    assert "x" in out
    # Status of the row — rich may color but the text always contains the word.
    assert "running" in out or "stopped" in out


def test_print_agent_list_renders_errors_under_table(monkeypatch, capsys):
    monkeypatch.setattr(_al, "_discover_defined_agents", lambda: [])
    monkeypatch.setattr(_al, "load_config", lambda p: _cfg("x"))
    monkeypatch.setattr(_al, "_probe_local", lambda cfg: True)
    from scitex_agent_container.config import _validation as v

    monkeypatch.setattr(v, "validate_config", lambda p: ["spec.runtime is required"])
    entries = [{"name": "x", "config": "/p"}]
    print_agent_list(_FakeRegistry(entries))
    out = capsys.readouterr().out
    assert "spec.runtime" in out


def test_print_agent_list_json(monkeypatch, capsys):
    monkeypatch.setattr(_al, "_discover_defined_agents", lambda: [])
    monkeypatch.setattr(_al, "load_config", lambda p: _cfg("x"))
    monkeypatch.setattr(_al, "_probe_local", lambda cfg: True)
    from scitex_agent_container.config import _validation as v

    monkeypatch.setattr(v, "validate_config", lambda p: [])
    entries = [{"name": "x", "config": "/p"}]
    print_agent_list_json(_FakeRegistry(entries))
    out = capsys.readouterr().out
    data = json.loads(out)
    assert data[0]["name"] == "x"


# ---------------------------------------------------------------------------
# _discover_defined_agents — walks tmp scope roots
# ---------------------------------------------------------------------------


def test_discover_defined_agents_walks_home_scope(monkeypatch, tmp_path):
    """When ~/.scitex/agent-container/agents has agents, they're discovered."""
    agents_root = tmp_path / ".scitex" / "agent-container" / "agents"
    agents_root.mkdir(parents=True)
    (agents_root / "a1").mkdir()
    (agents_root / "a1" / "spec.yaml").write_text("apiVersion: x")
    (agents_root / "no-spec").mkdir()  # has no spec.yaml — skipped

    from pathlib import Path as _RealPath

    monkeypatch.setattr(_RealPath, "home", classmethod(lambda cls: tmp_path))
    # Force the project-scope branch to do nothing so only home-scope is used.
    monkeypatch.setattr(
        "scitex_config._ecosystem.local_state.find_project_scope",
        lambda *a, **kw: None,
    )

    pairs = _discover_defined_agents()
    names = [n for n, _ in pairs]
    assert "a1" in names
    assert "no-spec" not in names
