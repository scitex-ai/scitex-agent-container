"""Deny-by-default enforcement, proven on the rendered launch surface.

One Claude-TUI spec in a fake ``$HOME`` that holds every host source a
grant can deliver (fleet binds, host ~/.claude, credentials, the listen
token, a secrets pool, a quota cache, a shared baseline, an ``.envrc``).
Each test renders the spec through the production materialisers and argv
builder (``_grants._render.render_surface``) and checks one authority:

* no ``grants:`` key and an all-granted block render IDENTICALLY (the
  gates are transparent — legacy is "everything granted");
* withholding one grant removes exactly that grant's piece.

Real files, real loader, real argv builder; no mocks.
"""

from __future__ import annotations

import pytest

from scitex_agent_container._grants._infer import _mask, _volatile, load_variant
from scitex_agent_container._grants._render import LaunchSurface, render_surface
from scitex_agent_container.config._grants import all_granted
from tests.scitex_agent_container._helpers.grants_harness import (
    HARNESSES as _HARNESSES,
)
from tests.scitex_agent_container._helpers.grants_harness import harness_variant


def _render(world, block) -> LaunchSurface:
    raw, spec, root = world
    return render_surface(load_variant(raw, spec, block), root=root)


def _without(key: str, part: str | None = None) -> dict:
    block = all_granted().to_block()
    if part is None:
        block[key] = False
    else:
        block[key] = [p for p in block[key] if p != part]
    if key == "sac_control":
        block["host_exec"] = False
    return block


def _has(surface: LaunchSurface, needle: str) -> bool:
    return any(needle in line for lines in surface.lines().values() for line in lines)


def test_legacy_surface_equals_all_granted_surface(world) -> None:
    # Arrange
    legacy = _render(world, None)
    # Act
    granted = _render(world, all_granted().to_block())
    # Assert — only volatile digests may differ; compare argv + env exactly.
    assert (legacy.argv, legacy.env_names) == (granted.argv, granted.env_names)


def test_legacy_render_is_not_refused(world) -> None:
    # Arrange
    block = None
    # Act
    legacy = _render(world, block)
    # Assert — positive control for every absence assertion below.
    assert legacy.error is None


@pytest.mark.parametrize(
    ("part", "needle"),
    [
        ("cards", ":/home/agent/.scitex/cards"),
        ("cct_store", ":/home/agent/.scitex/claude-code-telegrammer"),
        ("host_tmp", "/tmp:/tmp/host:ro"),
        ("quota_cache", "CCT_QUOTA_CACHE_PATH"),
    ],
)
def test_withheld_host_bind_is_not_mounted(world, part, needle) -> None:
    # Arrange
    legacy = _render(world, None)
    # Act
    withheld = _render(world, _without("host_binds", part))
    # Assert
    assert (_has(legacy, needle), _has(withheld, needle)) == (True, False)


@pytest.mark.parametrize(
    "name",
    [
        "SCITEX_AGENT_CONTAINER_RUNTIME_DIR",
        "SCITEX_CARDS_AGENT_ID",
        "PGUSER",
        "SCITEX_GENAI_BASE_URL",
        "DIRENV_CONFIG",
        "SCITEX_AGENT_CONTAINER_YAML_DIRS",
    ],
)
def test_withheld_fleet_env_drops_fleet_variable(world, name) -> None:
    # Arrange
    legacy = _render(world, None)
    # Act
    withheld = _render(world, _without("fleet_env"))
    # Assert
    assert (name in legacy.env_names, name in withheld.env_names) == (True, False)


@pytest.mark.parametrize(
    "needle",
    ["SAC_LISTEN_BASE_URL", "SAC_LISTEN_BEARER", "SAC_AGENT_TOKEN"],
)
def test_withheld_sac_control_drops_listen_wiring(world, needle) -> None:
    # Arrange
    legacy = _render(world, None)
    # Act
    withheld = _render(world, _without("sac_control"))
    # Assert
    assert (_has(legacy, needle), _has(withheld, needle)) == (True, False)


def test_withheld_sac_control_drops_builtin_mcp(world) -> None:
    # Arrange
    raw, spec, _root = world
    # Act
    config = load_variant(raw, spec, _without("sac_control"))
    # Assert
    assert "scitex-agent-container" not in (config.mcp_servers or {})


def test_withheld_host_network_refuses_unsealed_launch(world) -> None:
    # Arrange
    block = _without("host_network")
    # Act
    withheld = _render(world, block)
    # Assert
    assert (withheld.error or "").startswith("GrantRefused")


@pytest.mark.parametrize(
    ("part", "needle"),
    [
        ("commands", ".claude/commands/probe-cmd.md"),
        ("skills", ".claude/skills/scitex"),
        ("baseline", "BASELINE_PROBE.md"),
    ],
)
def test_withheld_host_claude_part_is_not_materialised(world, part, needle) -> None:
    # Arrange
    legacy = _render(world, None)
    # Act
    withheld = _render(world, _without("host_claude", part))
    # Assert
    assert (_has(legacy, needle), _has(withheld, needle)) == (True, False)


@pytest.mark.parametrize(
    ("part", "needle"),
    [("envrc", "GRANT_ENVRC_PROBE"), ("github_token", "GITHUB_TOKEN")],
)
def test_withheld_secret_is_not_delivered(world, part, needle) -> None:
    # Arrange
    legacy = _render(world, None)
    # Act
    withheld = _render(world, _without("secrets", part))
    # Assert
    assert (_has(legacy, needle), _has(withheld, needle)) == (True, False)


def test_withheld_host_model_auth_drops_host_oauth_bind(world) -> None:
    # Arrange
    needle = ":/tmp/sac-claude:rw"
    legacy = _render(world, None)
    # Act
    withheld = _render(world, _without("host_model_auth"))
    # Assert
    assert (_has(legacy, needle), _has(withheld, needle)) == (True, False)


def test_empty_block_withholds_every_surface_piece(world) -> None:
    # Arrange
    block = {
        **all_granted().to_block(),
        **{
            k: False
            for k in ("host_exec", "sac_control", "fleet_env", "host_model_auth")
        },
    }
    block.update(host_binds=[], host_claude=[], secrets=[])
    # Act
    withheld = _render(world, block)
    # Assert — host_network stays granted so the launch renders at all.
    assert not any(
        _has(withheld, n)
        for n in (
            "/home/agent/.scitex/cards",
            "SAC_LISTEN_BEARER",
            "PGUSER",
            "probe-cmd.md",
            "GITHUB_TOKEN",
            "/tmp/sac-claude",
        )
    )


# ---------------------------------------------------------------------------
# Legacy == all-granted across harnesses (the byte-identity pin): the gates
# are transparent for every harness path, not just the Claude TUI.
# ---------------------------------------------------------------------------

def _full_block(raw: dict) -> dict:
    """Every grant — minus host_network for a sealed run (its allowlist)."""
    sealed = "sandbox" in raw["spec"]
    return {**all_granted().to_block(), **({"host_network": False} if sealed else {})}


@pytest.mark.parametrize("name", sorted(_HARNESSES))
def test_harness_legacy_surface_equals_all_granted(world, name) -> None:
    # Arrange
    raw, spec, root = harness_variant(world, name)
    full = _full_block(raw)
    legacy = render_surface(load_variant(raw, spec, None), root=root)
    # Per-render values (an allocated port) differ between two IDENTICAL
    # renders; mask them exactly as `sac agents grants check` does.
    volatile = _volatile(legacy, render_surface(load_variant(raw, spec, None), root=root))
    # Act
    granted = render_surface(load_variant(raw, spec, full), root=root)
    # Assert
    assert _mask(legacy, volatile)["argv"] + _mask(legacy, volatile)["env"] == (
        _mask(granted, volatile)["argv"] + _mask(granted, volatile)["env"]
    )


@pytest.mark.parametrize("name", sorted(_HARNESSES))
def test_harness_legacy_render_reaches_launch_argv(world, name) -> None:
    # Arrange — positive control: the pin above is not vacuous.
    raw, spec, root = harness_variant(world, name)
    # Act
    legacy = render_surface(load_variant(raw, spec, None), root=root)
    # Assert
    assert (legacy.error, legacy.argv[:2]) == (None, ("apptainer", "exec"))
