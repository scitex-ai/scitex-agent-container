"""Shared helpers for the ``claude-agent-sdk`` runtime path.

This module is the single source of truth for three concerns that any
SDK-backed code path needs:

1. **Auth provisioning** — :func:`provision_anthropic_auth` ensures the
   SDK can authenticate without forcing the pay-per-token API-key path.
   Production agents run on Pro/Max **OAuth** (flat-rate); we bridge a
   stored OAuth token into ``ANTHROPIC_API_KEY`` only when neither
   ``ANTHROPIC_API_KEY`` nor ``~/.claude/.credentials.json`` is already
   present (headless contexts: SLURM, CI, fresh containers).

2. **Workspace resolution** — :func:`resolve_agent_workspace` reads the
   running agent's registry entry, computes its workspace ``cwd``, and
   parses the on-disk ``.mcp.json`` (already materialized by sac at
   agent-start time from ``spec.mcp_servers``) into the SDK's expected
   shape, with ``${VAR}`` references resolved against the current
   process environment.

3. **Options building** — :func:`build_sdk_options` composes the result
   of the previous two with per-caller knobs (system prompt, model,
   permission mode, hooks) into a ``ClaudeAgentOptions`` dataclass.

Both the existing one-shot A2A handler
(:func:`scitex_agent_container.a2a._handlers.handle_claude_session`)
and the upcoming long-lived ``claude-session`` runtime consume the same
helpers — guaranteeing that MCP wiring, auth, and model resolution
behave identically across the request/response path and the
lifecycle path.

The ``claude-agent-sdk`` import is **lazy**: importing this module does
not require the SDK to be installed. Each public helper imports the SDK
on demand and raises a clear error otherwise.

openai-compat-1: concern (2) is extracted (verbatim) to
``runtimes._provider_common``, re-exported below unchanged.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import TYPE_CHECKING

from ._provider_common import project_runtime_root, resolve_agent_workspace
from ._sdk_channels import apply_channels, merge_home_mcp_servers

if TYPE_CHECKING:  # pragma: no cover — typing only
    from claude_agent_sdk import ClaudeAgentOptions

    from ..config._types import AgentConfig

# PR #319 provider-aware tool whitelist: extracted to
# ``._sdk_provider_tools`` (line-cap split, 2026-07-21). The constant
# ``_PROVIDER_DEFAULT_ALLOWED_TOOLS`` and the root-cause record live there.

__all__ = [
    "SDKCommonError",
    "provision_anthropic_auth",
    "resolve_agent_workspace",
    "build_sdk_options",
    "project_runtime_root",
]


# NOTE: project_runtime_root is imported from ._provider_common above and
# re-exported via __all__ (openai-compat-1 extraction — see module
# docstring). Its definition used to live here, verbatim, unchanged.


def _cred_file_path() -> Path:
    """Resolve the credentials.json path the SDK reads.

    Honours ``CLAUDE_CONFIG_DIR`` (same env Claude Code itself respects)
    so the apptainer runtime can place the file outside ``$HOME``. Under
    hardened isolation the D2 preflight requires ``$HOME`` to be empty,
    so the runtime binds the host's credentials.json at
    ``/tmp/sac-claude/.credentials.json`` and sets
    ``CLAUDE_CONFIG_DIR=/tmp/sac-claude``; both the SDK and this helper
    then resolve to the same file.
    """
    cfg_dir = os.environ.get("CLAUDE_CONFIG_DIR")
    if cfg_dir:
        return Path(cfg_dir) / ".credentials.json"
    return Path.home() / ".claude" / ".credentials.json"


def _container_settings_path() -> str | None:
    """Resolve the in-container ``$HOME/.claude`` settings path, or ``None``.

    sac mirrors the agent's ``to_home`` tree (including the ``.claude``
    settings file) into the container ``$HOME`` — both via the
    workspace-home bind (hardened mode) and via the overlay upper home
    (relaxed ``--home``/``--overlay`` specs). The runner executes INSIDE the
    container, so ``$HOME`` already points at ``/home/agent`` (or whatever
    ``--home`` the spec set). We resolve the settings file against that
    ``$HOME`` and return its path only when a file is present — so a spec
    without one doesn't aim ``--settings`` at a missing file.

    Filename: prefer ``settings.json`` (the container ``$HOME`` settings
    file is delivered at USER scope so the interactive TUI also reads it —
    see :mod:`settings_json`), falling back to the legacy
    ``settings.local.json`` for older baselines that still ship that name.
    The SDK loads whichever we return via an explicit ``--settings`` flag
    (which, unlike the interactive TUI, IS honoured in print/SDK mode),
    independently of ``setting_sources=[]``.

    The hook ``command``s inside that settings file use ``$HOME/.claude/...``,
    so they resolve in-container regardless of what ``$HOME`` resolves to.
    """
    home = os.environ.get("HOME")
    if not home:
        return None
    claude_dir = Path(home) / ".claude"
    for name in ("settings.json", "settings.local.json"):
        candidate = claude_dir / name
        if candidate.is_file():
            return str(candidate)
    return None


# ---------------------------------------------------------------------------
# Why we never honour a pre-set ANTHROPIC_API_KEY
# ---------------------------------------------------------------------------
# The Anthropic SDK auto-reads ``ANTHROPIC_API_KEY`` from the process
# env. That auto-pickup is hostile to sac because:
#
#   1. **Dotfiles drift.** Operators historically exported stale API
#      keys (or OAuth bearers under various names) from ``.bashrc``.
#      An expired value silently survives every shell, every container
#      bind-mounting the parent env, and every CI runner that inherits
#      the secret. Symptom in production: "401 Invalid auth" or
#      "Command failed exit 1" with no obvious cause — the SDK
#      preferred the env var over the working OAuth credentials file.
#
#   2. **Surprise pay-per-token billing.** A pre-set
#      ``ANTHROPIC_API_KEY`` shadows the Pro/Max flat-rate OAuth path
#      in the credentials file. Operators paying for Pro/Max suddenly
#      see "Credit balance is too low" because the SDK quietly
#      switched to API-key billing.
#
#   3. **Provenance.** sac wants ONE tracked source of truth for the
#      key. If an operator can side-load a value via
#      ``ANTHROPIC_API_KEY``, every audit / quota / log lies about
#      where the credential came from.
#
# The contract is therefore: **``SAC_ANTHROPIC_API_KEY`` is the only
# env input we honour.** Whenever this function runs we:
#
#   * If ``SAC_ANTHROPIC_API_KEY`` is set → unconditionally OVERWRITE
#     ``ANTHROPIC_API_KEY`` with it (highest priority, no fallback).
#   * If ``SAC_ANTHROPIC_API_KEY`` is unset → POP ``ANTHROPIC_API_KEY``
#     from the env so a stale dotfiles export can't be picked up by
#     the SDK auto-reader after we return.
#
# Then the credentials-file path takes precedence (Pro/Max flat-rate),
# falling back to the SAC-provided env value (api-key form bridged
# directly; OAuth form synthesised into a credentials.json).
# ---------------------------------------------------------------------------

_SAC_API_KEY_ENV = "SAC_ANTHROPIC_API_KEY"


class SDKCommonError(RuntimeError):
    """Raised when the SDK common helpers cannot satisfy a precondition."""


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------


def provision_anthropic_auth() -> str:
    """Make sure the SDK can authenticate; return the path that will be used.

    Auth flow is *one-directional*:

        ``~/.claude/.credentials.json``  →  ``SAC_ANTHROPIC_API_KEY``
                                            (extracted by ``sac dev
                                            extract-apikey-from-credentials`` or the
                                            bash bridge)
        ``SAC_ANTHROPIC_API_KEY``        →  ``ANTHROPIC_API_KEY``
                                            (overridden here)

    Sac NEVER writes/synthesises ``credentials.json``. It is treated
    as a read-only artefact produced by ``claude /login``.

    Step 1 (always): ``SAC_ANTHROPIC_API_KEY`` overrides
    ``ANTHROPIC_API_KEY``. If SAC is unset, ``ANTHROPIC_API_KEY`` is
    popped. See the module-level comment for *why*.

    Step 2: pick a path, in precedence order:

    1. ``~/.claude/.credentials.json`` exists → ``"credentials_file"``
       (Pro/Max OAuth, flat-rate; SDK reads the file directly).
    2. ``SAC_ANTHROPIC_API_KEY`` set → ``"sac_env"`` (already mirrored
       to ``ANTHROPIC_API_KEY`` in step 1; the SDK reads it as-is).
    3. Neither → :class:`SDKCommonError`.
    """
    # Pre-1: scrub any pre-existing ANTHROPIC_API_KEY. The SDK
    # auto-reads it; a stale dotfiles export must not survive past
    # this point regardless of which path we pick below.
    os.environ.pop("ANTHROPIC_API_KEY", None)

    # Resolve the credentials path at CALL time (not import time) so it
    # honours the current ``CLAUDE_CONFIG_DIR`` / ``HOME`` — the apptainer
    # runtime sets these per-dispatch, and tests redirect them at a tmp
    # dir. An import-frozen constant would leak onto the real host file.
    cred_file = _cred_file_path()

    # Path A: credentials.json wins (Pro/Max OAuth flat-rate, real
    # refresh_token). The SDK reads the file directly. Critically we
    # do NOT set ANTHROPIC_API_KEY here even if SAC is also set —
    # Anthropic rejects ``sk-ant-oat*`` OAuth tokens passed as a bare
    # env, so an env override would shadow the working file path
    # and the SDK would fall back to the rejected env value
    # ("Invalid API key").
    if cred_file.is_file():
        # The file existing is NOT enough: a token that expired (or is
        # about to) while the file lingers on disk would otherwise sail
        # past this check and die with an ambiguous 401 the moment the
        # SDK opens a session. Fail LOUDLY here instead, with the
        # manual-refresh hint, so the operator gets a clear cause rather
        # than silent mid-session death.
        from .._state._preflight_creds import check_oauth_token_expiry

        try:
            check_oauth_token_expiry(cred_file)
        except (FileNotFoundError, ValueError, RuntimeError) as exc:
            raise SDKCommonError(
                f"Anthropic OAuth credentials at {cred_file} are not "
                f"usable: {exc} Run `claude login` to refresh the token, "
                "then restart the agent."
            ) from exc
        return "credentials_file"

    # Path B: SAC env (api-key form for pay-per-token, or oauth where
    # the operator deliberately doesn't have a credentials.json on
    # this host). Mirror SAC value into ANTHROPIC_API_KEY for the SDK.
    sac_value = os.environ.get(_SAC_API_KEY_ENV)
    if sac_value:
        os.environ["ANTHROPIC_API_KEY"] = sac_value
        return "sac_env"

    raise SDKCommonError(
        f"no Anthropic auth available — run `claude /login` so "
        f"{cred_file} exists, or export {_SAC_API_KEY_ENV}. "
        "sac does NOT honour a pre-set ANTHROPIC_API_KEY (see the "
        "module-level comment in runtimes/_sdk_common.py for why), "
        "and never writes/synthesises credentials.json itself."
    )


# ---------------------------------------------------------------------------
# Workspace + MCP wiring — see runtimes/_provider_common (openai-compat-1
# extraction). resolve_agent_workspace is imported above; nothing
# Claude-specific lives in this concern anymore.
# ---------------------------------------------------------------------------


def _load_agent_config_silent(agent_name: str) -> "AgentConfig | None":
    """Best-effort: load the agent's ``AgentConfig``; return ``None`` on any failure.

    Used by :func:`build_sdk_options` to consult ``spec.claude.provider``
    for the registered-tools whitelist (PR #319). Mirrors the
    resolve-from-registry shape of :func:`resolve_agent_workspace` so
    the two share the same best-effort failure profile: an unregistered
    agent / unreadable spec collapses to ``None`` and the option-builder
    proceeds as if no provider were configured (i.e. no tools= override),
    keeping pre-existing tests that don't wire a registry green.
    """
    try:
        from scitex_agent_container._state.registry import Registry
    except Exception:  # stx-allow: fallback (reason: optional dep at runtime; mirrors resolve_agent_workspace)
        return None
    try:
        entry = Registry().get(agent_name)
    except Exception:  # stx-allow: fallback (reason: registry IO best-effort)
        return None
    if not entry:
        return None
    config_path = entry.get("config")
    if not config_path:
        return None
    try:
        from scitex_agent_container.config import load_config

        return load_config(config_path)
    except Exception:  # stx-allow: fallback (reason: config load best-effort)
        return None


# NOTE: resolve_agent_workspace is imported from ._provider_common above
# and re-exported via __all__ (openai-compat-1 extraction). Its definition
# used to live here, verbatim, unchanged.


# ---------------------------------------------------------------------------
# Options builder
# ---------------------------------------------------------------------------


def build_sdk_options(
    agent_name: str,
    *,
    system_prompt: str | None = None,
    model: str | None = None,
    permission_mode: str | None = None,
    hooks: dict | None = None,
    resume: str | None = None,
    extra: dict | None = None,
) -> "ClaudeAgentOptions":
    """Compose a ``ClaudeAgentOptions`` for ``agent_name``.

    Calls :func:`provision_anthropic_auth` (so callers don't have to
    sequence it themselves) and :func:`resolve_agent_workspace` (so the
    agent's MCP servers and workspace cwd are wired automatically).
    Per-caller knobs (``system_prompt``, ``model``, ``permission_mode``,
    ``hooks``, ``resume``) layer on top. ``extra`` is a dict of any
    other supported ``ClaudeAgentOptions`` field — used sparingly for
    forward-compat with new SDK options.

    Raises :class:`SDKCommonError` if the SDK is not installed or no
    auth path is available.
    """
    try:
        from claude_agent_sdk import ClaudeAgentOptions
    except Exception as exc:  # stx-allow: fallback (reason: optional dep at runtime; broaden beyond ImportError so misbuilt transitive deps surface as actionable SDKCommonError)
        raise SDKCommonError(
            "claude-agent-sdk is not installed (`pip install claude-agent-sdk`)"
        ) from exc

    provision_anthropic_auth()
    mcp_servers, workdir = resolve_agent_workspace(agent_name)
    # Merge to_home-deployed $HOME/.mcp.json (the per-agent MCP delivery
    # path) — resolve_agent_workspace returns {} inside an apptainer
    # container, and setting_sources=[] kills the SDK's own project-scope
    # discovery, so this is the only path a per-agent MCP reaches the SDK.
    mcp_servers = merge_home_mcp_servers(mcp_servers)

    # Apptainer/Docker dispatch binds the host workdir at /work inside
    # the container; the config's workdir field carries the HOST path
    # (so the apptainer driver knows what to mount) but the SDK runs
    # INSIDE the container and must chdir to the BIND TARGET.
    #
    # Detection: apptainer sets APPTAINER_CONTAINER (or singularity's
    # SINGULARITY_CONTAINER) to the SIF path inside the container.
    # Path heuristics aren't reliable because apptainer auto-binds
    # /home/$USER, which makes the host workdir's path appear to
    # exist inside the container — but it points at the host fs, not
    # the bind target with --pwd /work semantics.
    if (
        workdir
        and (
            os.environ.get("APPTAINER_CONTAINER")
            or os.environ.get("SINGULARITY_CONTAINER")
        )
        and Path("/work").is_dir()
    ):
        workdir = "/work"

    kwargs: dict = {}
    if system_prompt is not None:
        kwargs["system_prompt"] = system_prompt
    if model:
        kwargs["model"] = model
    if permission_mode:
        kwargs["permission_mode"] = permission_mode
    if hooks:
        kwargs["hooks"] = hooks
    if resume:
        kwargs["resume"] = resume
    if workdir:
        kwargs["cwd"] = workdir
    if mcp_servers:
        kwargs["mcp_servers"] = mcp_servers
    # ``setting_sources=[]`` — match newb's working SDK setup.
    # The default loads ``~/.claude/`` state files (state.json,
    # projects/, settings.json) and treats "no state" as a not-yet-
    # logged-in session even when the credentials.json is mounted.
    # Empty sources tells the SDK the bind-mounted credentials.json
    # is the entire login context — required for CI containers that
    # only have the single file mounted, not a full ``~/.claude/``
    # tree. (The host operator's CLAUDE.md never reaches the agent
    # this way either, which is the right default for container-as-
    # boundary anyway.)
    kwargs.setdefault("setting_sources", [])
    # Load hooks/settings explicitly via the SDK ``settings`` field
    # (emits ``--settings <path>``). This is the "flag settings" layer —
    # the highest-priority user-controlled layer — and loads INDEPENDENTLY
    # of ``setting_sources``, which stays ``[]`` for machine-independence
    # (no host ``~/.claude`` auto-discovery). sac delivers the agent's
    # ``$HOME/.claude/settings.json`` (USER scope, so the interactive TUI
    # reads it too; legacy ``settings.local.json`` accepted as fallback) via
    # the ``to_home`` mirror; without ``--settings`` the SDK would never load
    # it (empty ``setting_sources`` skips the $HOME settings layer entirely).
    #
    # We resolve the path from the in-container ``$HOME`` (where the runner
    # actually executes) and only set it when a file is present, so a spec
    # without one doesn't point ``--settings`` at a missing file.
    settings_path = _container_settings_path()
    if settings_path is not None and "settings" not in kwargs:
        kwargs["settings"] = settings_path
    # Pop sac-private keys from ``extra`` BEFORE merging into kwargs —
    # ClaudeAgentOptions is strict and rejects unknown fields. The
    # ``_*`` prefix marks these as sac-internal (not SDK fields).
    channels: list[str] | None = None
    a2a_port: int | None = None
    if extra:
        extra = dict(extra)  # shallow copy so we can mutate
        channels = extra.pop("_channels", None)
        # ``_a2a_port`` is threaded for two purposes:
        #   1. The /v1/turn registration path (handled by the runner).
        #   2. The channel sidecar's WAKE path (WI-1) — the adapter POSTs
        #      received bus events to the agent's OWN ``/v1/turn`` on this
        #      port so a push WAKES an idle session (push ≡ Telegram). This
        #      is the agent's loopback turn endpoint, NOT the bus inbox SSE
        #      (that still lives on SAC_LISTEN_BASE_URL — see below).
        a2a_port = extra.pop("_a2a_port", None)
        if extra:
            kwargs.update(extra)

    # spec.claude.channels → dev-channels flag + sac MCP sidecar.
    # See ``_sdk_channels.apply_channels`` for the two gated concerns
    # (any-channel dev-flag vs server:sac-only sidecar registration).
    apply_channels(kwargs, channels, a2a_port, agent_name)

    # Enable subagents. The Agent (Task) tool is only OFFERED to the model
    # when it appears in ``allowed_tools`` (Anthropic "Subagents in the SDK"
    # guide — the built-in general-purpose subagent is invokable once "Agent"
    # is listed). The runner uses permission_mode=bypassPermissions, where
    # listing a tool only AUTO-APPROVES it and unlisted tools (Bash/Read/
    # Edit/…) stay available — so appending "Agent" enables subagent
    # delegation WITHOUT narrowing the toolset. Merge so any spec-provided
    # allowed_tools list is preserved.
    _allowed = list(kwargs.get("allowed_tools") or [])
    if "Agent" not in _allowed:
        _allowed.append("Agent")
    kwargs["allowed_tools"] = _allowed

    # PR #319: provider-aware tool REGISTRATION whitelist — restrict the
    # registered built-in tool set when a non-Anthropic provider backend
    # is active. Extracted to ``._sdk_provider_tools`` (root cause + spec
    # contract documented there). An explicit caller ``tools=...`` WINS.
    from ._sdk_provider_tools import apply_provider_tools

    apply_provider_tools(kwargs, _load_agent_config_silent(agent_name))

    # DURABLE SPEC ENV for every stdio MCP server (P1, card
    # sac-env-injection-lost-on-mcp-reconnect-20260721). Runs AFTER all
    # entries are assembled (registry spec.mcp_servers + $HOME/.mcp.json +
    # channel sidecars) so every one of them gets the bake. The spec env
    # reaches this process only as inherited environment (apptainer --env);
    # the FIRST MCP spawn inherits it too, but a mid-session RECONNECT
    # respawn through the sanitized stdio transport env does not — the
    # entry's env block is the only channel that survives every spawn path.
    # So the launch-manifested keys (SAC_SPEC_ENV_KEYS) are resolved from
    # this process's environ and baked in as literals. Entry-declared env
    # keys win; fail-loud when the manifest names an absent key; no-op on
    # pre-manifest launches. See runtimes/_mcp_spec_env.
    from ._mcp_spec_env import bake_spec_env_into_servers

    bake_spec_env_into_servers(kwargs.get("mcp_servers"), os.environ)

    return ClaudeAgentOptions(**kwargs)
