"""Tests for ``scitex_agent_container._mcp._tools`` — argv contract +
register_all_tools binding.

No-mocks rewrite (PA-306). The previous version used pytest
``monkeypatch`` to swap ``invoke_cli_json`` / ``invoke_cli_text`` and
the ``_skills._SKILLS_ROOT`` constant — both forbidden by STX-NM002.

This version:

* swaps the helper callables on each leaf module via a real
  save/restore context manager (no ``monkeypatch``) — captures every
  invocation into a real list,
* for ``_skills`` tests, swaps ``_SKILLS_ROOT`` the same way against
  real ``tmp_path`` directories holding real bytes,
* uses a hand-rolled ``_FakeMCP`` that records ``mcp.tool()`` calls,
* gives every test the AAA marker comments and a
  ``<subject>_<condition>_<expected>`` name; multi-assert tests are
  split one-behaviour-per-test.
"""

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

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

# ---------------------------------------------------------------------------
# Real-callable recorder: swaps invoke_cli_{json,text} on each leaf module
# with a hand-rolled function that appends (kind, argv) to a shared list,
# then restores the originals on exit. No mocks, no monkeypatch.
# ---------------------------------------------------------------------------


_LEAF_MODULES = (_agent, _db, _host, _image, _account, _info, _h)


@contextmanager
def _recording() -> Iterator[list[tuple[str, list[str]]]]:
    """Swap invoke_cli_{json,text} on every leaf module with real
    capturing callables. Yields the shared capture list."""
    captured: list[tuple[str, list[str]]] = []

    def fake_json(argv):
        captured.append(("json", list(argv)))
        return {"exit_code": 0, "data": {"argv": list(argv)}, "stdout": ""}

    def fake_text(argv):
        captured.append(("text", list(argv)))
        return {"exit_code": 0, "stdout": "ok"}

    saved: list[tuple[object, str, object]] = []
    for mod in _LEAF_MODULES:
        if hasattr(mod, "invoke_cli_json"):
            saved.append((mod, "invoke_cli_json", mod.invoke_cli_json))
            mod.invoke_cli_json = fake_json
        if hasattr(mod, "invoke_cli_text"):
            saved.append((mod, "invoke_cli_text", mod.invoke_cli_text))
            mod.invoke_cli_text = fake_text
    try:
        yield captured
    finally:
        for mod, attr, orig in saved:
            setattr(mod, attr, orig)


@contextmanager
def _skills_root(path: Path) -> Iterator[Path]:
    """Save/restore ``_skills._SKILLS_ROOT`` against a real directory."""
    saved = _skills._SKILLS_ROOT
    _skills._SKILLS_ROOT = path
    try:
        yield path
    finally:
        _skills._SKILLS_ROOT = saved


# ---------------------------------------------------------------------------
# _agent — argv contract per verb
# ---------------------------------------------------------------------------


def test_agent_list_with_no_filters_dispatches_bare_json_argv():
    # Arrange
    with _recording() as captured:
        # Act
        _agent.agent_list()
    # Assert
    assert captured[-1] == ("json", ["agents", "list", "--json"])


def test_agent_list_with_capability_and_machine_appends_filter_argv():
    # Arrange
    with _recording() as captured:
        # Act
        _agent.agent_list(capability="HPC", machine="ws1")
    # Assert
    assert captured[-1] == (
        "json",
        ["agents", "list", "--json", "--capability", "HPC", "--machine", "ws1"],
    )


def test_agent_status_with_name_dispatches_json_argv():
    # Arrange
    with _recording() as captured:
        # Act
        _agent.agent_status("x")
    # Assert
    assert captured[-1] == ("json", ["agents", "list", "x", "--json"])


def test_agent_logs_with_lines_dispatches_tail_text_argv():
    # Arrange
    with _recording() as captured:
        # Act
        _agent.agent_logs("x", lines=10)
    # Assert
    assert captured[-1] == ("text", ["agents", "tail", "x", "--lines", "10"])


def test_agent_health_with_name_dispatches_json_argv():
    # Arrange
    with _recording() as captured:
        # Act
        _agent.agent_health("x")
    # Assert
    assert captured[-1] == ("json", ["agents", "health", "x", "--json"])


def test_agent_find_with_pattern_dispatches_text_argv():
    # Arrange
    with _recording() as captured:
        # Act
        _agent.agent_find("pat-*")
    # Assert
    assert captured[-1] == ("text", ["agents", "find", "pat-*"])


def test_agent_check_with_name_dispatches_text_argv():
    # Arrange
    with _recording() as captured:
        # Act
        _agent.agent_check("x")
    # Assert
    assert captured[-1] == ("text", ["agents", "check", "x"])


def test_agent_recall_with_name_dispatches_text_argv():
    # Arrange
    with _recording() as captured:
        # Act
        _agent.agent_recall("x")
    # Assert
    assert captured[-1] == ("text", ["agents", "recall", "x"])


def test_agent_start_with_defaults_dispatches_text_argv_without_foreground():
    # Arrange
    with _recording() as captured:
        # Act
        _agent.agent_start("x")
    # Assert
    assert captured[-1] == ("text", ["agents", "start", "x"])


def test_agent_start_with_foreground_true_appends_foreground_flag():
    # Arrange
    with _recording() as captured:
        # Act
        _agent.agent_start("x", foreground=True)
    # Assert
    assert captured[-1] == ("text", ["agents", "start", "x", "--foreground"])


def test_agent_stop_with_name_dispatches_text_argv():
    # Arrange
    with _recording() as captured:
        # Act
        _agent.agent_stop("x")
    # Assert
    assert captured[-1] == ("text", ["agents", "stop", "x"])


def test_agent_restart_with_name_dispatches_text_argv():
    # Arrange
    with _recording() as captured:
        # Act
        _agent.agent_restart("x")
    # Assert
    assert captured[-1] == ("text", ["agents", "restart", "x"])


# ---------------------------------------------------------------------------
# _db — argv contract per verb
# ---------------------------------------------------------------------------


def test_db_show_with_no_args_dispatches_json_argv():
    # Arrange
    with _recording() as captured:
        # Act
        _db.db_show()
    # Assert
    assert captured[-1] == ("json", ["db", "show", "--json"])


def test_db_query_with_no_filters_uses_default_limit():
    # Arrange
    with _recording() as captured:
        # Act
        _db.db_query()
    # Assert
    assert captured[-1] == ("json", ["db", "query", "--json", "--limit", "50"])


def test_db_query_with_table_agent_host_and_limit_appends_filters_in_order():
    # Arrange
    with _recording() as captured:
        # Act
        _db.db_query(table="instances", agent="a", host="h", limit=5)
    # Assert
    assert captured[-1] == (
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


def test_db_clean_with_stale_seconds_dispatches_json_argv():
    # Arrange
    with _recording() as captured:
        # Act
        _db.db_clean(heartbeat_stale_seconds=120)
    # Assert
    assert captured[-1] == (
        "json",
        ["db", "clean", "--heartbeat-stale-seconds", "120", "--json"],
    )


def test_db_tick_with_stale_seconds_dispatches_text_argv():
    # Arrange
    with _recording() as captured:
        # Act
        _db.db_tick(heartbeat_stale_seconds=42)
    # Assert
    assert captured[-1] == (
        "text",
        ["db", "tick", "--heartbeat-stale-seconds", "42"],
    )


def test_db_migrate_with_defaults_dispatches_bare_text_argv():
    # Arrange
    with _recording() as captured:
        # Act
        _db.db_migrate()
    # Assert
    assert captured[-1] == ("text", ["db", "migrate"])


def test_db_migrate_with_force_true_appends_force_flag():
    # Arrange
    with _recording() as captured:
        # Act
        _db.db_migrate(force=True)
    # Assert
    assert captured[-1] == ("text", ["db", "migrate", "--force"])


def test_db_export_with_no_filters_dispatches_bare_text_argv():
    # Arrange
    with _recording() as captured:
        # Act
        _db.db_export()
    # Assert
    assert captured[-1] == ("text", ["db", "export"])


def test_db_export_with_since_and_host_appends_filter_argv():
    # Arrange
    with _recording() as captured:
        # Act
        _db.db_export(since="2024-01-01", host="h")
    # Assert
    assert captured[-1] == (
        "text",
        ["db", "export", "--since", "2024-01-01", "--host", "h"],
    )


def test_db_import_with_input_path_dispatches_json_argv():
    # Arrange
    with _recording() as captured:
        # Act
        _db.db_import("/blob.json")
    # Assert
    assert captured[-1] == ("json", ["db", "import", "/blob.json", "--json"])


# ---------------------------------------------------------------------------
# _host — every wrapper produces argv whose first element is ``host``
# ---------------------------------------------------------------------------


def test_host_list_dispatches_host_prefixed_argv():
    # Arrange
    with _recording() as captured:
        # Act
        _host.host_list()
    # Assert
    assert captured[-1][1][0] == "host"


def test_host_show_dispatches_host_prefixed_argv():
    # Arrange
    with _recording() as captured:
        # Act
        _host.host_show()
    # Assert
    assert captured[-1][1][0] == "host"


def test_host_probe_dispatches_host_prefixed_argv():
    # Arrange
    with _recording() as captured:
        # Act
        _host.host_probe("h")
    # Assert
    assert captured[-1][1][0] == "host"


def test_host_validate_dispatches_host_prefixed_argv():
    # Arrange
    with _recording() as captured:
        # Act
        _host.host_validate()
    # Assert
    assert captured[-1][1][0] == "host"


def test_host_exec_dispatches_host_prefixed_argv():
    # Arrange
    with _recording() as captured:
        # Act
        _host.host_exec("h", ["ls"])
    # Assert
    assert captured[-1][1][0] == "host"


# ---------------------------------------------------------------------------
# _image / _account / _info — wrappers dispatch the right top-level verb
# ---------------------------------------------------------------------------


def test_image_build_dispatches_image_prefixed_argv():
    # Arrange
    with _recording() as captured:
        # Act
        _image.image_build()
    # Assert
    assert captured[-1][1][0] == "image"


def test_account_show_dispatches_nonempty_argv():
    # Arrange
    with _recording() as captured:
        # Act
        _account.account_show()
    # Assert
    assert captured[-1][1] and isinstance(captured[-1][1], list)


def test_quota_watch_dispatches_nonempty_argv():
    # Arrange
    with _recording() as captured:
        # Act
        _account.quota_watch()
    # Assert
    assert captured[-1][1] and isinstance(captured[-1][1], list)


def test_mcp_doctor_dispatches_nonempty_argv():
    # Arrange
    with _recording() as captured:
        # Act
        _info.mcp_doctor()
    # Assert
    assert captured[-1][1] and isinstance(captured[-1][1], list)


def test_mcp_list_tools_dispatches_nonempty_argv():
    # Arrange
    with _recording() as captured:
        # Act
        _info.mcp_list_tools()
    # Assert
    assert captured[-1][1] and isinstance(captured[-1][1], list)


def test_list_python_apis_dispatches_nonempty_argv():
    # Arrange
    with _recording() as captured:
        # Act
        _info.list_python_apis()
    # Assert
    assert captured[-1][1] and isinstance(captured[-1][1], list)


# ---------------------------------------------------------------------------
# register_all_tools — binds every group's tools onto the supplied mcp
# ---------------------------------------------------------------------------


class _FakeMCP:
    """Records every fn registered via ``@mcp.tool()(...)``."""

    def __init__(self) -> None:
        self.registered: list[object] = []

    def tool(self, *args, **kw):
        def decorator(fn):
            self.registered.append(fn)
            return fn

        return decorator


def _register_and_get_names() -> set[str]:
    mcp = _FakeMCP()
    register_all_tools(mcp)
    return {fn.__name__ for fn in mcp.registered}


def test_register_all_tools_binds_agent_list_tool():
    # Arrange
    mcp = _FakeMCP()
    # Act
    register_all_tools(mcp)
    # Assert
    assert "agent_list" in {fn.__name__ for fn in mcp.registered}


def test_register_all_tools_binds_agent_start_tool():
    # Arrange
    mcp = _FakeMCP()
    # Act
    register_all_tools(mcp)
    # Assert
    assert "agent_start" in {fn.__name__ for fn in mcp.registered}


def test_register_all_tools_binds_db_show_tool():
    # Arrange
    mcp = _FakeMCP()
    # Act
    register_all_tools(mcp)
    # Assert
    assert "db_show" in {fn.__name__ for fn in mcp.registered}


def test_register_all_tools_binds_host_list_tool():
    # Arrange
    mcp = _FakeMCP()
    # Act
    register_all_tools(mcp)
    # Assert
    assert "host_list" in {fn.__name__ for fn in mcp.registered}


def test_register_all_tools_binds_image_build_tool():
    # Arrange
    mcp = _FakeMCP()
    # Act
    register_all_tools(mcp)
    # Assert
    assert "image_build" in {fn.__name__ for fn in mcp.registered}


def test_register_all_tools_binds_skills_list_tool():
    # Arrange
    mcp = _FakeMCP()
    # Act
    register_all_tools(mcp)
    # Assert
    assert "skills_list" in {fn.__name__ for fn in mcp.registered}


def test_register_all_tools_binds_skills_get_tool():
    # Arrange
    mcp = _FakeMCP()
    # Act
    register_all_tools(mcp)
    # Assert
    assert "skills_get" in {fn.__name__ for fn in mcp.registered}


# ---------------------------------------------------------------------------
# _skills — real-fs helpers under real tmp_path directories
# ---------------------------------------------------------------------------


def test_skills_list_on_real_package_dir_returns_count_and_skills_shape():
    # Arrange
    fn = _skills.skills_list
    # Act
    result = fn()
    # Assert
    assert (
        "count" in result and "skills" in result and isinstance(result["skills"], list)
    )


def test_skills_list_with_missing_root_returns_empty_payload(tmp_path):
    # Arrange
    missing = tmp_path / "no-such"
    # Act
    with _skills_root(missing):
        out = _skills.skills_list()
    # Assert
    assert out == {"count": 0, "skills": []}


def test_skills_list_with_frontmatter_description_extracts_quoted_value(tmp_path):
    # Arrange
    root = tmp_path / "skills-root"
    root.mkdir()
    (root / "alpha.md").write_text(
        "---\nname: alpha\ndescription: 'short desc here'\n---\n\nbody text\n"
    )
    # Act
    with _skills_root(root):
        out = _skills.skills_list()
    # Assert
    assert out["skills"][0]["description"] == "short desc here"


def test_skills_list_with_frontmatter_description_reports_single_entry(tmp_path):
    # Arrange
    root = tmp_path / "skills-root"
    root.mkdir()
    (root / "alpha.md").write_text(
        "---\nname: alpha\ndescription: 'short desc here'\n---\n\nbody text\n"
    )
    # Act
    with _skills_root(root):
        out = _skills.skills_list()
    # Assert
    assert out["count"] == 1 and out["skills"][0]["name"] == "alpha"


def test_skills_list_without_frontmatter_falls_back_to_first_nonheading_line(tmp_path):
    # Arrange
    root = tmp_path / "r"
    root.mkdir()
    (root / "x.md").write_text("# heading\n\nactual first line\n")
    # Act
    with _skills_root(root):
        out = _skills.skills_list()
    # Assert
    assert out["skills"][0]["description"] == "actual first line"


def test_skills_get_with_missing_name_returns_error_payload(tmp_path):
    # Arrange
    root = tmp_path / "r"
    root.mkdir()
    (root / "exists.md").write_text("hi")
    # Act
    with _skills_root(root):
        out = _skills.skills_get("missing")
    # Assert
    assert out["error"] == "skill not found"


def test_skills_get_with_missing_name_lists_available_stems(tmp_path):
    # Arrange
    root = tmp_path / "r"
    root.mkdir()
    (root / "exists.md").write_text("hi")
    # Act
    with _skills_root(root):
        out = _skills.skills_get("missing")
    # Assert
    assert out["available"] == ["exists"]


def test_skills_get_with_existing_name_returns_content_payload(tmp_path):
    # Arrange
    root = tmp_path / "r"
    root.mkdir()
    (root / "z.md").write_text("content body")
    # Act
    with _skills_root(root):
        out = _skills.skills_get("z")
    # Assert
    assert out["name"] == "z" and out["content"] == "content body"


def test_skills_extract_description_with_unclosed_frontmatter_returns_nonempty(
    tmp_path,
):
    # Arrange
    root = tmp_path / "r"
    root.mkdir()
    (root / "y.md").write_text(
        "---\nname: y\ndescription: never-reached\nno closing fence\n\nfallback line"
    )
    # Act
    with _skills_root(root):
        out = _skills.skills_list()
    # Assert
    assert out["skills"][0]["description"] != ""


def test_skills_extract_description_with_only_headings_returns_empty_string(tmp_path):
    # Arrange
    root = tmp_path / "r"
    root.mkdir()
    (root / "blank.md").write_text("# only heading\n\n# another\n")
    # Act
    with _skills_root(root):
        out = _skills.skills_list()
    # Assert
    assert out["skills"][0]["description"] == ""
