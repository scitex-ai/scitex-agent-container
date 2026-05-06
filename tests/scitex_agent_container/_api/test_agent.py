"""Tests for ``scitex_agent_container._api`` noun submodules.

Confirms the nested form (``sac.agent.list``) and the flat form
(``sac.agent_list``) reference the same function objects — there's
no aliasing layer that could drift.
"""

from __future__ import annotations

import pytest

pytest.importorskip("fastmcp")

import scitex_agent_container as sac  # noqa: E402


@pytest.mark.parametrize(
    "submodule, verb, flat_name",
    [
        ("agent", "list", "agent_list"),
        ("agent", "status", "agent_status"),
        ("agent", "logs", "agent_logs"),
        ("agent", "start", "agent_start"),
        ("agent", "stop", "agent_stop"),
        ("agent", "restart", "agent_restart"),
        ("agent", "attach", "agent_attach"),
        ("agent", "check", "agent_check"),
        ("agent", "validate", "agent_validate"),
        ("db", "show", "db_show"),
        ("db", "query", "db_query"),
        ("db", "clean", "db_clean"),
        ("db", "tick", "db_tick"),
        ("db", "migrate", "db_migrate"),
        ("db", "export", "db_export"),
        ("db", "import_", "db_import"),
        ("host", "show", "host_show"),
        ("host", "list", "host_list"),
        ("host", "validate", "host_validate"),
        ("host", "probe", "host_probe"),
        ("host", "exec", "host_exec"),
        ("image", "build", "image_build"),
        ("template", "render_contributor_spec", "template_render_contributor_spec"),
        ("account", "show", "account_show"),
        ("skills", "list", "skills_list"),
        ("skills", "get", "skills_get"),
        ("mcp", "list_tools", "mcp_list_tools"),
        ("mcp", "doctor", "mcp_doctor"),
    ],
)
def test_nested_form_is_same_object_as_flat(
    submodule: str, verb: str, flat_name: str
) -> None:
    nested_fn = getattr(getattr(sac, submodule), verb)
    flat_fn = getattr(sac, flat_name)
    assert nested_fn is flat_fn, (
        f"sac.{submodule}.{verb} should be the same function object "
        f"as sac.{flat_name}, but they differ"
    )


def test_every_submodule_listed_in_package_all() -> None:
    """The eight noun submodules must appear in ``sac.__all__`` so
    Sphinx + the linter discover them."""
    for noun in (
        "agent",
        "db",
        "host",
        "image",
        "template",
        "account",
        "skills",
        "mcp",
    ):
        assert noun in sac.__all__, f"{noun!r} missing from sac.__all__"


def test_account_watch_quota_reverse_aliased():
    """``sac.account.watch_quota`` mirrors the flat ``quota_watch``."""
    assert sac.account.watch_quota is sac.quota_watch
