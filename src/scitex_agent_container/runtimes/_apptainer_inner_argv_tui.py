"""Interactive ``claude`` TUI argv + channel-plan helpers.

Split out of ``_apptainer_inner_argv.py`` (2026-07-05) to keep that
orchestrator under the repo's 512-line cap — this module is the
cohesive "everything about the interactive TUI inner process" half;
the `kind: Agent` / `kind: AgentProxy` SDK-runner dispatch and the
shell-wrapper (``build_inner_argv``) stay in the parent module. Every
symbol here is still importable as
``scitex_agent_container.runtimes._apptainer_inner_argv.<name>`` — the
parent module re-imports them for back-compat with existing callers.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..config import AgentConfig
    from ._sdk_channels import ChannelPlan

_CLAUDE_TUI_BIN = "claude"


def _home_has_resumable_conversation(config: "AgentConfig"):
    """Return ``(has_conversation, home_path)`` for the agent's container $HOME.

    Interactive ``claude -c`` (continue) with NO prior conversation prints
    ``No conversation found to continue`` and **exits** — which kills the tmux
    PTY at boot, so the boot-drain only ever sees a vanished session (the exact
    failure that cost a 240 s misdiagnosis as "login wall"). Continue-mode must
    therefore be gated on a transcript actually existing.

    Resolves the SAME host dir that backs the container ``$HOME`` in
    :func:`_apptainer_build_argv.build_run_argv` — the overlay upper-home for a
    relaxed-directory-overlay agent, else the workspace-home bind — and checks
    for any ``.claude/projects/*/*.jsonl`` transcript. Lazy imports keep this
    free of an import cycle with ``tui_session`` / ``_to_home_overlay``.
    """
    from pathlib import Path

    from ._to_home_overlay import resolve_overlay_upper_home

    upper = resolve_overlay_upper_home(config)
    if upper is not None and upper.is_dir():
        home = upper
    else:
        from .tui_session import state_dir_for_config

        home = state_dir_for_config(config) / "home"
    projects = Path(home) / ".claude" / "projects"
    has = projects.is_dir() and any(projects.glob("*/*.jsonl"))
    return has, home


def _tui_runner_argv(
    config: "AgentConfig",
    *,
    mcp_config: str | None = None,
    channel_mcp: str | None = None,
    dev_channels: str | None = None,
    settings: str | None = None,
) -> list[str]:
    """Argv for the interactive ``claude`` TUI (``spec.runtime: tui``).

    The inner process is the bundled ``claude`` binary running its
    interactive Ink TUI — the tmux PTY the caller wraps this argv in is
    what gives it a terminal. Threads the declarative spec surface:

      * ``spec.claude.model``  → ``--model <name>`` (when set).
      * ``spec.claude.flags``  → appended verbatim (e.g.
        ``--dangerously-skip-permissions``).

    Settings/hooks: the interactive ``claude`` reads hooks/settings from
    ``$HOME/.claude/settings.json`` at USER scope (and project-scope
    ``<cwd>/.claude/settings{,.local}.json``). It does NOT read a
    ``$HOME/.claude/settings.local.json`` — there is no ``.local.json`` at
    user scope. So the skip-permissions key + SAC channel hooks + the
    ``_shared`` baseline honest-grounding Stop gate / lint PostToolUse are
    materialised into ``$HOME/.claude/settings.json``
    (``setup_settings_json(..., filename="settings.json")``) and discovered
    automatically — no flag needed. The caller still passes the in-container
    path as ``settings`` and we add ``--settings <path>`` as belt-and-
    suspenders / SDK parity, but for the interactive TUI that flag is a no-op
    (it replaces discovery and only applies to print/SDK mode — see
    ``_sdk_common.build_sdk_options`` and skill ``25_claude-setup-delivery``).
    The earlier bug — the suite materialised under ``settings.local.json`` at
    ``$HOME`` — left it INERT: the TUI hit live "Do you want to proceed?"
    prompts AND reached DONE with an empty clew DAG because the Stop gate
    never fired (paper-scitex-clew solver run 2026-06-20).

    MCP servers: the interactive ``claude`` auto-discovers ``.mcp.json``
    from the PROJECT ROOT (its cwd, ``--pwd``), NOT from ``$HOME`` like
    the SDK runner. So when to_home materialised a ``$HOME/.mcp.json``,
    the caller passes its in-container path as ``mcp_config`` and we add
    ``--mcp-config <path>`` so the TUI loads those servers (figrecipe /
    scitex-agent-container / scitex-todo …). Without this the TUI shows
    "No MCP servers configured".

    Channels (SDK parity — see ``runtimes._sdk_channels.apply_channels``):
    ``spec.claude.channels`` drives two flags. ``dev_channels`` →
    ``--dangerously-load-development-channels <set>`` (any channel).
    ``channel_mcp`` → an inline ``--mcp-config`` JSON registering the
    ``sac mcp channel`` stdio subscriber (``server:sac`` only) so the TUI
    actually receives a2a-bus pushes — the interactive ``claude`` has no
    ``--channels`` flag, so the subscriber must be injected as an MCP
    server (the bus-auth env from ``listen_env_flags`` lets it connect).
    Each mcp config rides on its OWN ``--mcp-config`` flag (P0 fix
    2026-06-15): claude's ``<configs...>`` syntax claims it accepts
    multiple space-separated values after one flag, but the binary
    silently drops everything past the first — the operator-facing
    symptom was the TUI complaining "no MCP server configured with that
    name" even though the values were passed. Repeated flags match the
    SDK runtime's pattern and are observably loaded.

    No tini wrapper: the TUI is the foreground interactive process in the
    tmux pane; apptainer + tmux own signal delivery. ``startup_commands``
    wrapping (``build_inner_argv``) still ``exec``s this as the tail.
    """
    argv: list[str] = [_CLAUDE_TUI_BIN]
    claude_spec = getattr(config, "claude", None)
    model = str(getattr(claude_spec, "model", "") or "").strip()
    if not model:
        model = str(getattr(config, "model", "") or "").strip()
    if model:
        argv += ["--model", model]
    # Session continuity: append ``-c`` (continue) ONLY when the resolved
    # session mode is ``continue``. ``config.claude.session`` is already fully
    # resolved (CLI override > explicit spec > role-default > global ``fresh``)
    # by the loader/CLI before this builder runs — we only translate it to the
    # flag here. Experiment capsules (no coordinator role → ``fresh``) run
    # hermetic; coordinator roles keep continuity.
    from ..config._session_continuity import wants_continue

    if wants_continue(getattr(claude_spec, "session", None)):
        has_history, home = _home_has_resumable_conversation(config)
        if has_history:
            argv.append("-c")
        else:
            import logging

            logging.getLogger(__name__).warning(
                "TuiSessionRuntime: agent %r is continue-mode but its "
                "container-home %s holds NO prior conversation transcript — "
                "OMITTING `-c` and starting a FRESH session this boot. Reason: "
                "interactive `claude -c` with no history prints 'No conversation "
                "found to continue' and EXITS, silently killing the tmux session "
                "during boot-drain (misdiagnosed as a login wall). The session "
                "becomes resumable once this first boot writes a transcript. If "
                "this recurs EVERY boot the home is not persisting across "
                "restarts — check the overlay-upper bind / to_home materialisation.",
                getattr(config, "name", "?"),
                home,
            )
    # One ``--mcp-config`` per value (P0 fix 2026-06-15, operator-reported):
    # ``claude --help`` documents ``--mcp-config <configs...>`` as accepting
    # multiple space-separated values after a single flag, but the real
    # binary silently drops every value past the first. Symptom on the
    # failing fleet (figrecipe / todo / neurovista): the TUI pane showed
    # ``server:<external-channel>,server:sac · no MCP server
    # configured with that name`` even though the workspace ``.mcp.json``
    # path AND the inline ``sac mcp channel`` JSON were both passed.
    # Emitting one flag per value matches the SDK runtime's repeated-
    # flag pattern and is observably loaded by the TUI.
    if mcp_config:
        argv += ["--mcp-config", mcp_config]
    if channel_mcp:
        argv += ["--mcp-config", channel_mcp]
    # Explicit settings/hooks load (belt-and-suspenders / SDK parity). The
    # interactive TUI actually loads these via USER-scope discovery of
    # ``$HOME/.claude/settings.json`` — this ``--settings`` is a no-op for it
    # (it only takes effect in print/SDK mode), but harmless and kept for
    # parity. Emitted before the verbatim ``spec.claude.flags`` so an operator
    # who hand-passes their own ``--settings`` still wins (claude takes the
    # last occurrence).
    if settings:
        argv += ["--settings", settings]
    if dev_channels:
        # --dangerously-load-development-channels is REPEATABLE — claude wants
        # one occurrence per channel (the ``server:<mcp>`` entry, prefix kept).
        # A comma-joined value is read as ONE channel entry → "no MCP server
        # configured with that name", losing every channel. dev_channels
        # arrives comma-joined; split it and emit one flag per channel.
        for _channel in dev_channels.split(","):
            argv += ["--dangerously-load-development-channels", _channel]
    for flag in list(getattr(claude_spec, "flags", []) or []):
        flag = str(flag).strip()
        if flag:
            argv.append(flag)
    return argv


# In-SIF resolution of the bundled ``sac`` console-script for the
# channel-MCP subscriber. The bundled ``claude`` TUI spawns it as an
# stdio MCP subprocess INSIDE the SIF, so the command must point at
# sac's REAL in-SIF location — which varies by image build:
#
#   * sac-base.sif (current, verified 2026-06-17): /opt/venv-sac/bin/sac
#   * a prior build:                               /opt/venv-agent/bin/sac
#
# A single hardcoded path silently broke the channel the moment the SIF
# layout flipped: claude reported ``server:sac · no MCP server configured
# with that name`` because the subprocess ``command`` did not exist
# (2026-06-17 figrecipe/neurovista/todo — the constant had been flipped
# to ``/opt/venv-agent/bin/sac``, absent from sac-base.sif). The HOST
# cannot probe the in-SIF filesystem while building the apptainer-exec
# argv, so resolution is DEFERRED to spawn time: the MCP ``command`` is
# ``/bin/sh -c <resolver>`` that tries the known venvs + PATH inside the
# SIF and fails loud if none is executable (see :func:`_sac_channel_mcp_server`).
# Absolute candidates make it PATH-independent under ``--containall`` /
# ``--cleanenv`` where the venv bin dir may not be exported.

# Ordered absolute in-SIF candidates (current build first), mirroring the
# SDK runner's ``_sdk_channels._resolve_sac_binary`` candidate philosophy.
_SAC_BIN_IN_SIF_CANDIDATES: tuple[str, ...] = (
    "/opt/venv-sac/bin/sac",
    "/opt/venv-agent/bin/sac",
    "/usr/local/bin/sac",
)

# Back-compat single-path constant + helper. The channel MCP no longer
# consumes these (it uses the spawn-time resolver below); kept for
# external imports / single-path callers. Default corrected to the
# verified current sac-base.sif location.
_SAC_BIN_IN_SIF_DEFAULT = "/opt/venv-sac/bin/sac"


def resolve_sac_bin_in_sif() -> str:
    """Return a best-effort single in-SIF path to the ``sac`` script.

    Honours the ``SAC_BIN_IN_SIF`` env override, else returns
    :data:`_SAC_BIN_IN_SIF_DEFAULT`. NOTE: the channel-MCP subscriber no
    longer uses this — it resolves sac at SPAWN time across
    :data:`_SAC_BIN_IN_SIF_CANDIDATES` (robust to SIF venv layout); see
    :func:`_sac_channel_mcp_server`. This helper remains for
    backward-compatible imports and single-path callers.
    """
    import os as _os

    override = _os.environ.get("SAC_BIN_IN_SIF", "").strip()
    if override:
        return override
    return _SAC_BIN_IN_SIF_DEFAULT


# Backward-compat alias — keep imports of ``_SAC_BIN_IN_SIF`` working
# (e.g. external skill-doc examples that referenced the legacy constant
# name). New call sites should call :func:`resolve_sac_bin_in_sif`.
_SAC_BIN_IN_SIF = _SAC_BIN_IN_SIF_DEFAULT


def _sac_channel_mcp_server(channel_args: list[str]) -> dict:
    """Build the stdio MCP-server spec for the ``sac mcp channel`` sub.

    The ``command`` resolves sac's absolute path INSIDE the SIF at spawn
    time — trying ``$SAC_BIN`` / ``$SAC_BIN_IN_SIF`` (operator override
    via ``spec.env``), the known venv locations
    (:data:`_SAC_BIN_IN_SIF_CANDIDATES`), then ``command -v sac`` — and
    ``exec``s the first executable, passing ``channel_args`` through
    unchanged via ``"$@"``. Fails loud (a stderr diagnostic + ``exit
    127``) when sac is absent, so a missing binary surfaces as a named
    error rather than the silent ``server:sac · no MCP server configured
    with that name`` (2026-06-17). Absolute candidates make resolution
    PATH-independent under ``--containall`` / ``--cleanenv``.

    ``/bin/sh -c <resolver> sac <channel_args...>``: ``"sac"`` becomes
    ``$0`` and ``channel_args`` become ``"$@"``, so the resolved binary
    runs ``sac mcp channel --name <agent> [...]`` exactly.
    """
    candidates = (
        '"$SAC_BIN" "$SAC_BIN_IN_SIF" '
        + " ".join(_SAC_BIN_IN_SIF_CANDIDATES)
        + ' "$(command -v sac 2>/dev/null)"'
    )
    resolver = (
        f"for c in {candidates}; do "
        'if [ -n "$c" ] && [ -x "$c" ]; then exec "$c" "$@"; fi; '
        "done; "
        ">&2 echo 'sac: binary not found in SIF; channel MCP cannot "
        "start (set SAC_BIN_IN_SIF in spec.env or rebuild the image "
        "with sac installed)'; exit 127"
    )
    return {
        "type": "stdio",
        "command": "/bin/sh",
        "args": ["-c", resolver, "sac", *channel_args],
    }


def tui_channel_plan(config: "AgentConfig") -> "ChannelPlan":
    """Compute the shared :class:`ChannelPlan` for a TUI agent from its config.

    The bridge from the TUI's config-shaped inputs (``spec.claude.channels``,
    the resolved ``spec.a2a.port``, the agent name) to the runtime-agnostic
    ``_sdk_channels.compute_channel_plan``. Both :func:`tui_channel_config`
    (the inner ``--mcp-config`` / ``--dangerously-load-development-channels``)
    and ``build_run_argv`` (the generic ``SAC_AGENT_TURN_URL`` wake ``--env``)
    call this, so the TUI wires the SAME channel decisions as the SDK
    ``apply_channels`` path — no drift.
    """
    from ._sdk_channels import compute_channel_plan

    claude_spec = getattr(config, "claude", None)
    channels = [
        str(c).strip()
        for c in (getattr(claude_spec, "channels", []) or [])
        if str(c).strip()
    ]
    a2a_spec = getattr(config, "a2a", None)
    raw_port = getattr(a2a_spec, "port", None) if a2a_spec else None
    a2a_port = raw_port if isinstance(raw_port, int) and raw_port > 0 else None
    return compute_channel_plan(channels, a2a_port, config.name)


def tui_channel_config(config: "AgentConfig") -> tuple[str | None, str | None]:
    """Resolve ``spec.claude.channels`` into TUI channel flags.

    Returns ``(dev_channels, channel_mcp_json)`` — SDK parity with
    :func:`runtimes._sdk_channels.apply_channels`:

      * ``dev_channels`` — comma-joined channel set for
        ``--dangerously-load-development-channels`` (fires for ANY
        channel entry), or ``None`` when no channels are declared.
      * ``channel_mcp_json`` — inline ``--mcp-config`` JSON registering
        the ``sac mcp channel --name <agent>`` stdio subscriber under
        ``mcpServers.sac`` (``server:sac`` ONLY), or ``None``. The
        subscriber's ``--listen-url`` defaults to ``$SAC_LISTEN_BASE_URL``
        (already forwarded by ``listen_env_flags``); when the a2a port is
        resolved it also gets ``--turn-url`` for the WAKE path.
    """
    plan = tui_channel_plan(config)
    if not plan.channels:
        return None, None
    # One --dangerously-load flag per channel (the emission loop in
    # _tui_runner_argv splits this comma-joined value) — the SAME set the SDK
    # comma-joins into extra_args, so the two runtimes never disagree.
    dev_channels = ",".join(plan.channels)
    channel_mcp: str | None = None
    if plan.sac_sidecar_args is not None:
        channel_mcp = json.dumps(
            {
                "mcpServers": {
                    "sac": _sac_channel_mcp_server(list(plan.sac_sidecar_args))
                }
            }
        )
    return dev_channels, channel_mcp


__all__ = [
    "resolve_sac_bin_in_sif",
    "tui_channel_config",
    "tui_channel_plan",
]
