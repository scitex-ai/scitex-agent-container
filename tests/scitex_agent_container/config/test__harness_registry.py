"""Tests for ``config/_harness_registry.py`` (v4 migration step 4).

The registry is the single source for the harness axes: the spec→key
resolution (:func:`resolve_harness_key`) and every formerly-hardcoded
set the codebase derives from it (``_VALID_RUNTIMES``, ``_get_runtime``'s
branch sets, both inner-argv dispatches, ``_TUI_RUNTIMES``, the
harness-axis enums). Card
``sac-v4-layering-refactor-harness-runtime-inference-20260813``.

No mocks: every assertion runs the real resolver / real descriptors /
real derived consumers against real ``AgentConfig`` objects.
"""

from __future__ import annotations

import pytest

from scitex_agent_container.config import AgentConfig
from scitex_agent_container.config._harness_registry import (
    CLAUDE_AGENT_SDK,
    CLAUDE_CODE_TUI,
    CODEX_SDK,
    CODEX_TUI,
    HARNESS_DESCRIPTORS,
    OPENAI_AGENTS,
    UnmappableHarnessError,
    host_probed_runtime_spellings,
    known_harnesses,
    resolve_harness_key,
    runtime_spellings_for,
    valid_runtime_spellings,
)
from scitex_agent_container.config._harness_types import (
    AGENT_HARNESSES,
    V4_HARNESS_DISPATCH_CARD,
    HarnessKeyConflictError,
)

# ---------------------------------------------------------------------------
# resolve_harness_key — the spec→key mapping, raw-Mapping surface
# ---------------------------------------------------------------------------


def test_resolve_empty_spec_maps_to_the_tui_key():
    # Arrange — a spec stating neither axis: the fleet default.
    spec: dict = {}
    # Act
    key = resolve_harness_key(spec)
    # Assert
    assert key == CLAUDE_CODE_TUI


def test_resolve_empty_runtime_spelling_maps_to_the_tui_key():
    # Arrange
    spec = {"runtime": ""}
    # Act
    key = resolve_harness_key(spec)
    # Assert
    assert key == CLAUDE_CODE_TUI


def test_resolve_runtime_tui_maps_to_the_tui_key():
    # Arrange
    spec = {"runtime": "tui"}
    # Act
    key = resolve_harness_key(spec)
    # Assert
    assert key == CLAUDE_CODE_TUI


def test_resolve_runtime_claude_agent_sdk_maps_to_the_sdk_key():
    # Arrange
    spec = {"runtime": "claude-agent-sdk"}
    # Act
    key = resolve_harness_key(spec)
    # Assert
    assert key == CLAUDE_AGENT_SDK


def test_resolve_legacy_apptainer_spelling_maps_to_the_sdk_key():
    # Arrange — the pre-2026-06-13 container-engine value stays an alias.
    spec = {"runtime": "apptainer"}
    # Act
    key = resolve_harness_key(spec)
    # Assert
    assert key == CLAUDE_AGENT_SDK


def test_resolve_harness_openai_maps_to_the_openai_key():
    # Arrange
    spec = {"harness": "openai"}
    # Act
    key = resolve_harness_key(spec)
    # Assert
    assert key == OPENAI_AGENTS


def test_resolve_harness_axis_wins_over_the_runtime_axis():
    # Arrange — the harness states the SDK family while the runtime
    # spelling names an Anthropic launch mode; the family must win
    # (same precedence _get_runtime's guard applies).
    spec = {"harness": "openai", "runtime": "tui"}
    # Act
    key = resolve_harness_key(spec)
    # Assert
    assert key == OPENAI_AGENTS


def test_resolve_honours_the_legacy_provider_alias():
    # Arrange — spec.provider is the deprecated spelling of the harness
    # axis and must resolve identically (the live corpus still uses it).
    spec = {"provider": "openai"}
    # Act
    key = resolve_harness_key(spec)
    # Assert
    assert key == OPENAI_AGENTS


def test_resolve_raises_the_stated_conflict_like_the_loader():
    # Arrange — harness and provider stating DIFFERENT values.
    spec = {"harness": "anthropic", "provider": "openai"}
    # Act — resolution refuses to pick one silently.
    # Assert — same exception type the loader raises.
    with pytest.raises(HarnessKeyConflictError):
        resolve_harness_key(spec)


def test_resolve_unknown_runtime_raises_unmappable():
    # Arrange
    spec = {"runtime": "docker"}
    # Act — the mapping is total; anything outside it is a loud error.
    # Assert
    with pytest.raises(UnmappableHarnessError):
        resolve_harness_key(spec)


def test_resolve_unknown_runtime_error_names_the_runtime_value():
    # Arrange — the errors-reach-the-caller directive: the message must
    # name what the spec actually said.
    spec = {"runtime": "docker"}
    # Act
    # Assert
    with pytest.raises(UnmappableHarnessError, match="'docker'"):
        resolve_harness_key(spec)


def test_resolve_unknown_runtime_error_names_the_harness_value():
    # Arrange — both axes must appear in the message, not just the
    # offending one.
    spec = {"runtime": "docker"}
    # Act
    # Assert
    with pytest.raises(UnmappableHarnessError, match="'anthropic'"):
        resolve_harness_key(spec)


def test_resolve_unknown_runtime_error_names_the_v4_card():
    # Arrange
    spec = {"runtime": "docker"}
    # Act
    # Assert
    with pytest.raises(UnmappableHarnessError, match=V4_HARNESS_DISPATCH_CARD):
        resolve_harness_key(spec)


def test_resolve_unknown_harness_raises_unmappable():
    # Arrange
    spec = {"harness": "qwen"}
    # Act
    # Assert
    with pytest.raises(UnmappableHarnessError):
        resolve_harness_key(spec)


def test_resolve_unknown_harness_error_names_the_value():
    # Arrange
    spec = {"harness": "qwen"}
    # Act
    # Assert
    with pytest.raises(UnmappableHarnessError, match="'qwen'"):
        resolve_harness_key(spec)


def test_resolve_unknown_harness_error_names_the_v4_card():
    # Arrange
    spec = {"harness": "qwen"}
    # Act
    # Assert
    with pytest.raises(UnmappableHarnessError, match=V4_HARNESS_DISPATCH_CARD):
        resolve_harness_key(spec)


def test_unmappable_error_is_a_value_error():
    # Arrange — callers that historically caught _get_runtime's
    # ValueError("Unsupported runtime: ...") must keep working.
    error_type = UnmappableHarnessError
    # Act
    is_value_error = issubclass(error_type, ValueError)
    # Assert
    assert is_value_error


# ---------------------------------------------------------------------------
# resolve_harness_key — loaded-AgentConfig surface
# ---------------------------------------------------------------------------


def test_resolve_accepts_a_loaded_config_default_runtime():
    # Arrange — AgentConfig's runtime field defaults to "tui" (the
    # loader default).
    cfg = AgentConfig(name="t")
    # Act
    key = resolve_harness_key(cfg)
    # Assert
    assert key == CLAUDE_CODE_TUI


def test_resolve_accepts_a_loaded_config_sdk_runtime():
    # Arrange
    cfg = AgentConfig(name="t", runtime="claude-agent-sdk")
    # Act
    key = resolve_harness_key(cfg)
    # Assert
    assert key == CLAUDE_AGENT_SDK


def test_resolve_accepts_a_loaded_config_legacy_apptainer():
    # Arrange
    cfg = AgentConfig(name="t", runtime="apptainer")
    # Act
    key = resolve_harness_key(cfg)
    # Assert
    assert key == CLAUDE_AGENT_SDK


def test_resolve_accepts_a_loaded_config_openai_harness():
    # Arrange
    cfg = AgentConfig(name="t", runtime="tui", harness="openai")
    # Act
    key = resolve_harness_key(cfg)
    # Assert
    assert key == OPENAI_AGENTS


def test_resolve_config_and_mapping_surfaces_agree():
    # Arrange — the same axes must resolve identically whichever
    # surface hands them over: the loader's AgentConfig or the raw
    # YAML mapping.
    spellings = sorted(valid_runtime_spellings())
    # Act
    per_config = [
        resolve_harness_key(AgentConfig(name="t", runtime=s)) for s in spellings
    ]
    per_mapping = [resolve_harness_key({"runtime": s}) for s in spellings]
    # Assert
    assert per_config == per_mapping


# ---------------------------------------------------------------------------
# The registry entries — shape and stated contracts
# ---------------------------------------------------------------------------


def test_registry_has_exactly_the_five_real_entries():
    # Arrange — CODEX_SDK joined on 2026-08-14 (card
    # sac-codex-python-sdk-harness-20260814), the first vendor added
    # since the registry landed. A closed-set assertion like this one is
    # deliberately allowed to break when a row is added: the break is
    # the review prompt asking whether the new entry was intended.
    # CODEX_TUI joined on 2026-09-05 (the operator's move off Claude Code).
    expected = {CLAUDE_CODE_TUI, CLAUDE_AGENT_SDK, OPENAI_AGENTS, CODEX_SDK, CODEX_TUI}
    # Act
    keys = set(HARNESS_DESCRIPTORS)
    # Assert
    assert keys == expected


def test_every_dict_key_matches_its_entry_key():
    # Arrange
    items = HARNESS_DESCRIPTORS.items()
    # Act
    all_match = all(key == descriptor.key for key, descriptor in items)
    # Assert
    assert all_match


def test_the_tui_entry_is_external_hosted():
    # Arrange — the interactive `claude` binary owns its own loop; sac
    # supervises from outside (tmux pane).
    entry = HARNESS_DESCRIPTORS[CLAUDE_CODE_TUI]
    # Act
    hosted = entry.hosted
    # Assert
    assert hosted == "external"


def test_the_sdk_entry_is_runner_hosted():
    # Arrange
    entry = HARNESS_DESCRIPTORS[CLAUDE_AGENT_SDK]
    # Act
    hosted = entry.hosted
    # Assert
    assert hosted == "runner"


def test_the_openai_entry_is_runner_hosted():
    # Arrange
    entry = HARNESS_DESCRIPTORS[OPENAI_AGENTS]
    # Act
    hosted = entry.hosted
    # Assert
    assert hosted == "runner"


def test_the_tui_entry_beats_are_host_probed():
    # Arrange — an external process cannot beat for itself; the TUI
    # heartbeat loop stamps the pane-activity epoch from the host side.
    entry = HARNESS_DESCRIPTORS[CLAUDE_CODE_TUI]
    # Act
    beat_writer = entry.beat_writer
    # Assert
    assert beat_writer == "host-probe"


def test_the_sdk_entry_beats_in_process():
    # Arrange
    entry = HARNESS_DESCRIPTORS[CLAUDE_AGENT_SDK]
    # Act
    beat_writer = entry.beat_writer
    # Assert
    assert beat_writer == "in-process"


def test_the_openai_entry_beats_in_process():
    # Arrange
    entry = HARNESS_DESCRIPTORS[OPENAI_AGENTS]
    # Act
    beat_writer = entry.beat_writer
    # Assert
    assert beat_writer == "in-process"


def test_the_tui_entry_can_resume():
    # Arrange
    entry = HARNESS_DESCRIPTORS[CLAUDE_CODE_TUI]
    # Act
    can_resume = entry.can_resume
    # Assert
    assert can_resume


def test_the_sdk_entry_can_resume():
    # Arrange
    entry = HARNESS_DESCRIPTORS[CLAUDE_AGENT_SDK]
    # Act
    can_resume = entry.can_resume
    # Assert
    assert can_resume


def test_the_openai_entry_cannot_resume():
    # Arrange — the openai session CLI's resume flags are parity-only
    # today.
    entry = HARNESS_DESCRIPTORS[OPENAI_AGENTS]
    # Act
    can_resume = entry.can_resume
    # Assert
    assert not can_resume


def test_the_tui_entry_has_no_runner_module():
    # Arrange — its inner process is the `claude` binary, not a
    # `python -m` runner.
    entry = HARNESS_DESCRIPTORS[CLAUDE_CODE_TUI]
    # Act
    module = entry.runner_module
    # Assert
    assert module is None


def test_the_sdk_entry_names_the_claude_session_runner():
    # Arrange
    entry = HARNESS_DESCRIPTORS[CLAUDE_AGENT_SDK]
    # Act
    module = entry.runner_module
    # Assert
    assert module == "scitex_agent_container._runners.claude_session"


def test_the_openai_entry_names_the_openai_session_runner():
    # Arrange
    entry = HARNESS_DESCRIPTORS[OPENAI_AGENTS]
    # Act
    module = entry.runner_module
    # Assert
    assert module == "scitex_agent_container._runners.openai_session"


def test_prepare_home_defaults_to_a_no_op_for_every_entry():
    # Arrange — the design gives prepare_home a default no-op;
    # per-harness home extras hook in at a later migration step.
    cfg = AgentConfig(name="t")
    # Act
    results = [d.prepare_home(cfg) for d in HARNESS_DESCRIPTORS.values()]
    # Assert
    assert results == [None] * len(HARNESS_DESCRIPTORS)


def test_the_claude_entries_share_one_env_and_binds_builder():
    # Arrange — both Claude-family launches flow through the same real
    # auth builder (runtimes._apptainer_auth.auth_argv); the entries
    # must not fork it.
    tui_entry = HARNESS_DESCRIPTORS[CLAUDE_CODE_TUI]
    sdk_entry = HARNESS_DESCRIPTORS[CLAUDE_AGENT_SDK]
    # Act
    same_builder = tui_entry.env_and_binds is sdk_entry.env_and_binds
    # Assert
    assert same_builder


def test_the_openai_env_builder_declines_a_non_openai_launch():
    # Arrange — real call, no env manipulation: openai_env_flags
    # returns [] when the launch does not resolve to the openai harness.
    entry = HARNESS_DESCRIPTORS[OPENAI_AGENTS]
    # Act
    flags = entry.env_and_binds(AgentConfig(name="t"), None)
    # Assert
    assert flags == []


# ---------------------------------------------------------------------------
# inner_argv — the entries build the REAL runner tails
# ---------------------------------------------------------------------------


def test_sdk_inner_argv_is_the_tini_wrapped_session_runner():
    # Arrange
    cfg = AgentConfig(name="t", runtime="apptainer")
    # Act
    argv = HARNESS_DESCRIPTORS[CLAUDE_AGENT_SDK].inner_argv(cfg)
    # Assert
    assert argv[:5] == ["/usr/bin/tini", "-s", "--", "python3", "-m"]


def test_sdk_inner_argv_dispatches_the_claude_session_module():
    # Arrange
    cfg = AgentConfig(name="t", runtime="apptainer")
    # Act
    argv = HARNESS_DESCRIPTORS[CLAUDE_AGENT_SDK].inner_argv(cfg)
    # Assert
    assert argv[5] == "scitex_agent_container._runners.claude_session"


def test_sdk_inner_argv_carries_the_agent_name():
    # Arrange
    cfg = AgentConfig(name="t", runtime="apptainer")
    # Act
    argv = HARNESS_DESCRIPTORS[CLAUDE_AGENT_SDK].inner_argv(cfg)
    # Assert
    assert argv[argv.index("--name") + 1] == "t"


def test_openai_inner_argv_dispatches_the_openai_session_module():
    # Arrange
    cfg = AgentConfig(name="t", runtime="apptainer", harness="openai")
    # Act
    argv = HARNESS_DESCRIPTORS[OPENAI_AGENTS].inner_argv(cfg)
    # Assert
    assert argv[5] == "scitex_agent_container._runners.openai_session"


def test_openai_inner_argv_never_names_the_claude_runner():
    # Arrange
    cfg = AgentConfig(name="t", runtime="apptainer", harness="openai")
    # Act
    argv = HARNESS_DESCRIPTORS[OPENAI_AGENTS].inner_argv(cfg)
    # Assert
    assert "claude_session" not in " ".join(argv)


def test_tui_inner_argv_launches_the_claude_binary():
    # Arrange
    cfg = AgentConfig(name="t", runtime="tui")
    # Act
    argv = HARNESS_DESCRIPTORS[CLAUDE_CODE_TUI].inner_argv(cfg)
    # Assert
    assert argv[0] == "claude"


def test_tui_inner_argv_threads_the_mcp_config_option():
    # Arrange
    cfg = AgentConfig(name="t", runtime="tui")
    # Act
    argv = HARNESS_DESCRIPTORS[CLAUDE_CODE_TUI].inner_argv(
        cfg, {"tui_mcp_config": "/home/agent/.mcp.json"}
    )
    # Assert
    assert argv[argv.index("--mcp-config") + 1] == "/home/agent/.mcp.json"


def test_build_inner_argv_embeds_the_registry_built_sdk_tail():
    # Arrange — the dispatch site must emit exactly what the registry
    # entry builds: the derivation is real, not parallel code.
    from scitex_agent_container.runtimes._apptainer_inner_argv import (
        build_inner_argv,
    )

    cfg = AgentConfig(name="t", runtime="apptainer")
    tail = HARNESS_DESCRIPTORS[CLAUDE_AGENT_SDK].inner_argv(cfg, {"one_shot": False})
    # Act
    wrapped = build_inner_argv(cfg)
    # Assert
    assert " ".join(tail) in wrapped[2]


# ---------------------------------------------------------------------------
# Derivations — the formerly-hardcoded sets all read from the registry
# ---------------------------------------------------------------------------


def test_valid_runtime_spellings_is_the_union_of_the_entries():
    # Arrange
    expected = frozenset({"", "tui", "apptainer", "claude-agent-sdk"})
    # Act
    spellings = valid_runtime_spellings()
    # Assert
    assert spellings == expected


def test_validation_valid_runtimes_derives_from_the_registry():
    # Arrange
    from scitex_agent_container.config._validation import _VALID_RUNTIMES

    # Act
    derived = valid_runtime_spellings()
    # Assert
    assert _VALID_RUNTIMES == derived


def test_tui_spellings_cover_default_and_explicit():
    # Arrange
    expected = frozenset({"", "tui"})
    # Act
    spellings = runtime_spellings_for(CLAUDE_CODE_TUI)
    # Assert
    assert spellings == expected


def test_sdk_spellings_cover_current_and_legacy():
    # Arrange
    expected = frozenset({"apptainer", "claude-agent-sdk"})
    # Act
    spellings = runtime_spellings_for(CLAUDE_AGENT_SDK)
    # Assert
    assert spellings == expected


def test_host_probed_spellings_are_exactly_the_tui_entrys():
    # Arrange
    tui_spellings = runtime_spellings_for(CLAUDE_CODE_TUI)
    # Act
    probed = host_probed_runtime_spellings()
    # Assert
    assert probed == tui_spellings


def test_sdk_heartbeat_loop_skip_set_derives_from_the_registry():
    # Arrange
    from scitex_agent_container._lifecycle._sdk_heartbeat_loop import _TUI_RUNTIMES

    # Act
    derived = host_probed_runtime_spellings()
    # Assert
    assert _TUI_RUNTIMES == derived


def test_known_harnesses_lists_the_three_families_sorted():
    # Arrange — "codex" joined the axis with the fourth registry row.
    expected = ("anthropic", "codex", "openai")
    # Act
    harnesses = known_harnesses()
    # Assert
    assert harnesses == expected


def test_harness_types_enum_derives_from_the_registry():
    # Arrange
    derived = known_harnesses()
    # Act
    enum_value = AGENT_HARNESSES
    # Assert
    assert enum_value == derived


def test_provider_env_override_enum_derives_from_the_registry():
    # Arrange
    from scitex_agent_container.runtimes._apptainer_provider import (
        _VALID_AGENT_HARNESSES,
    )

    # Act
    derived = known_harnesses()
    # Assert
    assert _VALID_AGENT_HARNESSES == derived


def test_inner_argv_agent_runner_module_derives_from_the_registry():
    # Arrange
    from scitex_agent_container.runtimes._apptainer_inner_argv import (
        RUNNER_MODULE_AGENT,
    )

    # Act
    derived = HARNESS_DESCRIPTORS[CLAUDE_AGENT_SDK].runner_module
    # Assert
    assert RUNNER_MODULE_AGENT == derived


def test_inner_argv_openai_runner_module_derives_from_the_registry():
    # Arrange
    from scitex_agent_container.runtimes._apptainer_inner_argv import (
        RUNNER_MODULE_OPENAI,
    )

    # Act
    derived = HARNESS_DESCRIPTORS[OPENAI_AGENTS].runner_module
    # Assert
    assert RUNNER_MODULE_OPENAI == derived


def test_build_argv_runner_module_constant_derives_from_the_registry():
    # Arrange
    from scitex_agent_container.runtimes._apptainer_build_argv import RUNNER_MODULE

    # Act
    derived = HARNESS_DESCRIPTORS[CLAUDE_AGENT_SDK].runner_module
    # Assert
    assert RUNNER_MODULE == derived


# ---------------------------------------------------------------------------
# _get_runtime — key-based dispatch, preserved behavior
# ---------------------------------------------------------------------------


def test_get_runtime_default_still_selects_the_tui_adapter():
    # Arrange
    from scitex_agent_container._lifecycle._runtime_select import _get_runtime
    from scitex_agent_container.runtimes.tui_session import TuiSessionRuntime

    cfg = AgentConfig(name="t")
    # Act
    adapter = _get_runtime(cfg)
    # Assert
    assert isinstance(adapter, TuiSessionRuntime)


def test_get_runtime_sdk_spelling_still_selects_the_sdk_adapter():
    # Arrange
    from scitex_agent_container._lifecycle._runtime_select import _get_runtime
    from scitex_agent_container.runtimes.claude_session import ClaudeSessionRuntime

    cfg = AgentConfig(name="t", runtime="claude-agent-sdk")
    # Act
    adapter = _get_runtime(cfg)
    # Assert
    assert isinstance(adapter, ClaudeSessionRuntime)


def test_get_runtime_unknown_spelling_error_names_the_card():
    # Arrange — the registry's loud config error replaces the old
    # hand-written message and must carry the card id (the
    # "Unsupported runtime" phrasing is preserved — see
    # test_lifecycle.test_get_runtime_unsupported_runtime_raises).
    from scitex_agent_container._lifecycle._runtime_select import _get_runtime

    cfg = AgentConfig(name="t", runtime="docker-legacy")
    # Act
    # Assert
    with pytest.raises(ValueError, match=V4_HARNESS_DISPATCH_CARD):
        _get_runtime(cfg)


def test_get_runtime_proxy_resolves_by_launch_mode_alone():
    # Arrange — an AgentProxy is not a harness (vendor-neutral
    # forwarder): a harness value on a proxy spec must not change its
    # adapter.
    from scitex_agent_container._lifecycle._runtime_select import _get_runtime
    from scitex_agent_container.config import ProxySpec
    from scitex_agent_container.runtimes.tui_session import TuiSessionRuntime

    cfg = AgentConfig(
        name="t",
        runtime="tui",
        harness="openai",
        kind="AgentProxy",
        proxy=ProxySpec(upstream="http://u"),
    )
    # Act
    adapter = _get_runtime(cfg)
    # Assert
    assert isinstance(adapter, TuiSessionRuntime)


# ---------------------------------------------------------------------------
# codex-tui (2026-09-05): the first non-Anthropic entry with a launch path
# ---------------------------------------------------------------------------


def test_resolve_harness_codex_maps_to_the_codex_tui_key():
    # Arrange -- a spec that only flipped harness: anthropic -> codex.
    spec = {"harness": "codex"}
    # Act
    key = resolve_harness_key(spec)
    # Assert
    assert key == CODEX_TUI


def test_resolve_harness_codex_runtime_tui_maps_to_the_codex_tui_key():
    # Arrange
    spec = {"harness": "codex", "runtime": "tui"}
    # Act
    key = resolve_harness_key(spec)
    # Assert
    assert key == CODEX_TUI


def test_resolve_harness_codex_has_no_runtime_spelling_for_the_headless_runner():
    # Arrange -- codex-sdk has no lifecycle adapter, so no spelling selects it.
    spec = {"harness": "codex", "runtime": "codex-sdk"}
    # Act
    try:
        resolve_harness_key(spec)
        message = ""
    except UnmappableHarnessError as exc:
        message = str(exc)
    # Assert
    assert "codex-sdk" in message


def test_resolve_harness_codex_legacy_apptainer_runtime_is_unmappable():
    # Arrange -- "apptainer" spells the CLAUDE sdk runner, not a codex mode.
    spec = {"harness": "codex", "runtime": "apptainer"}
    # Act
    try:
        resolve_harness_key(spec)
        message = ""
    except UnmappableHarnessError as exc:
        message = str(exc)
    # Assert
    assert "apptainer" in message


def test_codex_tui_entry_is_host_probed_like_the_claude_tui():
    # Arrange
    descriptor = HARNESS_DESCRIPTORS[CODEX_TUI]
    # Act
    writer = descriptor.beat_writer
    # Assert
    assert writer == "host-probe"
