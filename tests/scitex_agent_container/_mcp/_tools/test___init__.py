"""Tests for _mcp._tools — every wrapper dispatches to the right CLI argv.

The wrappers are intentionally thin: each forwards to invoke_cli_json
or invoke_cli_text with a fixed argv. We verify the argv contract by
recording what the helper sees.
"""

from __future__ import annotations

import pytest

from scitex_agent_container._mcp._tools import (
    _account,
    _agent,
    _db,
    _host,
    _image,
    _info,
    _skills,
    register_all_tools,
)
from scitex_agent_container._mcp._tools import (
    _helpers as _h,
)


@pytest.fixture
def recorder(monkeypatch):
    """Patch invoke_cli_json / invoke_cli_text on every leaf module so
    every call is captured into a list and the heavy CLI runner is
    bypassed."""
    captured: list[tuple[str, list[str]]] = []

    def fake_json(argv):
        captured.append(("json", list(argv)))
        return {"exit_code": 0, "data": {"argv": argv}, "stdout": ""}

    def fake_text(argv):
        captured.append(("text", list(argv)))
        return {"exit_code": 0, "stdout": "ok"}

    for mod in (_agent, _db, _host, _image, _account, _info, _helpers := _h):
        if hasattr(mod, "invoke_cli_json"):
            monkeypatch.setattr(mod, "invoke_cli_json", fake_json)
        if hasattr(mod, "invoke_cli_text"):
            monkeypatch.setattr(mod, "invoke_cli_text", fake_text)
    return captured


# ---------------------------------------------------------------------------
# _agent
# ---------------------------------------------------------------------------


def test_agent_list_no_filters(recorder):
    _agent.agent_list()
    assert recorder[-1] == ("json", ["agent", "list", "--json"])


def test_agent_list_filtered(recorder):
    _agent.agent_list(capability="HPC", machine="ws1")
    assert recorder[-1] == (
        "json",
        ["agent", "list", "--json", "--capability", "HPC", "--machine", "ws1"],
    )


def test_agent_status(recorder):
    _agent.agent_status("x")
    assert recorder[-1] == ("json", ["agent", "status", "x", "--json"])


def test_agent_logs(recorder):
    _agent.agent_logs("x", lines=10)
    assert recorder[-1] == ("text", ["agent", "logs", "x", "--lines", "10"])


def test_agent_health(recorder):
    _agent.agent_health("x")
    assert recorder[-1] == ("json", ["agent", "health", "x", "--json"])


def test_agent_find(recorder):
    _agent.agent_find("pat-*")
    assert recorder[-1] == ("text", ["agent", "find", "pat-*"])


def test_agent_check(recorder):
    _agent.agent_check("x")
    assert recorder[-1] == ("text", ["agent", "check", "x"])


def test_agent_validate(recorder):
    _agent.agent_validate("/p.yaml")
    assert recorder[-1] == ("text", ["agent", "validate", "/p.yaml"])


def test_agent_inspect(recorder):
    _agent.agent_inspect("x")
    assert recorder[-1] == ("text", ["agent", "inspect", "x"])


def test_agent_recall(recorder):
    _agent.agent_recall("x")
    assert recorder[-1] == ("text", ["agent", "recall", "x"])


def test_agent_check_priority(recorder):
    _agent.agent_check_priority("x")
    assert recorder[-1] == ("text", ["agent", "check-priority", "x"])


def test_agent_take_snapshot(recorder):
    _agent.agent_take_snapshot("x")
    assert recorder[-1] == ("text", ["agent", "take-snapshot", "x"])


def test_agent_attach(recorder):
    _agent.agent_attach("x")
    assert recorder[-1] == ("text", ["agent", "attach", "x"])


def test_agent_start_default(recorder):
    _agent.agent_start("x")
    assert recorder[-1] == ("text", ["agent", "start", "x"])


def test_agent_start_foreground(recorder):
    _agent.agent_start("x", foreground=True)
    assert recorder[-1] == ("text", ["agent", "start", "x", "--foreground"])


def test_agent_stop(recorder):
    _agent.agent_stop("x")
    assert recorder[-1] == ("text", ["agent", "stop", "x"])


def test_agent_restart(recorder):
    _agent.agent_restart("x")
    assert recorder[-1] == ("text", ["agent", "restart", "x"])


# ---------------------------------------------------------------------------
# _db
# ---------------------------------------------------------------------------


def test_db_show(recorder):
    _db.db_show()
    assert recorder[-1] == ("json", ["db", "show", "--json"])


def test_db_query_no_filters(recorder):
    _db.db_query()
    assert recorder[-1] == ("json", ["db", "query", "--json", "--limit", "50"])


def test_db_query_with_filters(recorder):
    _db.db_query(table="instances", agent="a", host="h", limit=5)
    assert recorder[-1] == (
        "json",
        [
            "db",
            "query",
            "--json",
            "--limit",
            "5",
            "--table",
            "instances",
            "--agent",
            "a",
            "--host",
            "h",
        ],
    )


def test_db_clean(recorder):
    _db.db_clean(heartbeat_stale_seconds=120)
    assert recorder[-1] == (
        "json",
        ["db", "clean", "--heartbeat-stale-seconds", "120", "--json"],
    )


def test_db_tick(recorder):
    _db.db_tick(heartbeat_stale_seconds=42)
    assert recorder[-1] == (
        "text",
        ["db", "tick", "--heartbeat-stale-seconds", "42"],
    )


def test_db_migrate(recorder):
    _db.db_migrate()
    assert recorder[-1] == ("text", ["db", "migrate"])


def test_db_migrate_force(recorder):
    _db.db_migrate(force=True)
    assert recorder[-1] == ("text", ["db", "migrate", "--force"])


def test_db_export_minimal(recorder):
    _db.db_export()
    assert recorder[-1] == ("text", ["db", "export"])


def test_db_export_filters(recorder):
    _db.db_export(since="2024-01-01", host="h")
    assert recorder[-1] == (
        "text",
        ["db", "export", "--since", "2024-01-01", "--host", "h"],
    )


def test_db_import(recorder):
    _db.db_import("/blob.json")
    assert recorder[-1] == ("json", ["db", "import", "/blob.json", "--json"])


# ---------------------------------------------------------------------------
# _host / _image / _account / _info
# ---------------------------------------------------------------------------


def test_host_tools_argv(recorder):
    _host.host_list()
    _host.host_show()
    _host.host_probe("h")
    _host.host_validate()
    _host.host_exec("h", "ls")
    forms = [r[1] for r in recorder[-5:]]
    for argv in forms:
        assert argv[0] == "host"


def test_image_tools_argv(recorder):
    _image.image_build()
    assert recorder[-1][1][0] == "image"


def test_account_tools_argv(recorder):
    _account.account_show()
    _account.quota_watch()
    forms = [r[1] for r in recorder[-2:]]
    # account_show uses `accounts show`, quota_watch uses `account quota-watch`
    assert all(isinstance(argv, list) and argv for argv in forms)


def test_info_tools_argv(recorder):
    _info.mcp_doctor()
    _info.mcp_list_tools()
    _info.list_python_apis()
    forms = [r[1] for r in recorder[-3:]]
    assert all(isinstance(argv, list) and argv for argv in forms)


# ---------------------------------------------------------------------------
# register_all_tools binds every fn onto the supplied mcp
# ---------------------------------------------------------------------------


class _FakeMCP:
    """Captures every fn registered via @mcp.tool()()."""

    def __init__(self):
        self.registered = []

    def tool(self, *args, **kw):
        def decorator(fn):
            self.registered.append(fn)
            return fn

        return decorator


def test_register_all_tools_attaches_many_tools():
    mcp = _FakeMCP()
    register_all_tools(mcp)
    names = {fn.__name__ for fn in mcp.registered}
    # spot-check coverage of every group
    assert "agent_list" in names
    assert "agent_start" in names
    assert "db_show" in names
    assert "host_list" in names
    assert "image_build" in names
    assert "skills_list" in names
    assert "skills_get" in names


# ---------------------------------------------------------------------------
# _skills — pure-fs helpers (no CLI dispatch)
# ---------------------------------------------------------------------------


def test_skills_list_real_dir():
    """Real package skills dir exists; returns a count + list shape."""
    result = _skills.skills_list()
    assert "count" in result
    assert "skills" in result
    assert isinstance(result["skills"], list)


def test_skills_list_missing_root(tmp_path, monkeypatch):
    monkeypatch.setattr(_skills, "_SKILLS_ROOT", tmp_path / "no-such")
    out = _skills.skills_list()
    assert out == {"count": 0, "skills": []}


def test_skills_list_extracts_description_from_frontmatter(tmp_path, monkeypatch):
    root = tmp_path / "skills-root"
    root.mkdir()
    (root / "alpha.md").write_text(
        "---\nname: alpha\ndescription: 'short desc here'\n---\n\nbody text\n"
    )
    monkeypatch.setattr(_skills, "_SKILLS_ROOT", root)
    out = _skills.skills_list()
    assert out["count"] == 1
    assert out["skills"][0]["name"] == "alpha"
    assert out["skills"][0]["description"] == "short desc here"


def test_skills_list_falls_back_to_first_line(tmp_path, monkeypatch):
    root = tmp_path / "r"
    root.mkdir()
    (root / "x.md").write_text("# heading\n\nactual first line\n")
    monkeypatch.setattr(_skills, "_SKILLS_ROOT", root)
    out = _skills.skills_list()
    assert out["skills"][0]["description"] == "actual first line"


def test_skills_get_not_found(tmp_path, monkeypatch):
    root = tmp_path / "r"
    root.mkdir()
    (root / "exists.md").write_text("hi")
    monkeypatch.setattr(_skills, "_SKILLS_ROOT", root)
    out = _skills.skills_get("missing")
    assert out["error"] == "skill not found"
    assert out["available"] == ["exists"]


def test_skills_get_found(tmp_path, monkeypatch):
    root = tmp_path / "r"
    root.mkdir()
    (root / "z.md").write_text("content body")
    monkeypatch.setattr(_skills, "_SKILLS_ROOT", root)
    out = _skills.skills_get("z")
    assert out["name"] == "z"
    assert out["content"] == "content body"


def test_skills_extract_description_edge_no_frontmatter_close(tmp_path, monkeypatch):
    """Frontmatter opens but never closes — falls back to first non-blank, non-#/--- line."""
    root = tmp_path / "r"
    root.mkdir()
    (root / "y.md").write_text(
        "---\nname: y\ndescription: never-reached\nno closing fence\n\nfallback line"
    )
    monkeypatch.setattr(_skills, "_SKILLS_ROOT", root)
    out = _skills.skills_list()
    # Without a closing ``---`` the frontmatter parser falls through to the
    # body-scan branch; first non-blank line is the literal first line.
    desc = out["skills"][0]["description"]
    assert desc != ""


def test_skills_extract_description_all_blank_or_comment(tmp_path, monkeypatch):
    root = tmp_path / "r"
    root.mkdir()
    (root / "blank.md").write_text("# only heading\n\n# another\n")
    monkeypatch.setattr(_skills, "_SKILLS_ROOT", root)
    out = _skills.skills_list()
    assert out["skills"][0]["description"] == ""
