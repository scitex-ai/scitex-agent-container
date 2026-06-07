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
"""

from __future__ import annotations

import json as _json
import os
import re as _re
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ._sdk_channels import apply_channels, merge_home_mcp_servers
from ._skills_manifest import build_skills_manifest_block

if TYPE_CHECKING:  # pragma: no cover — typing only
    from claude_agent_sdk import ClaudeAgentOptions

    from ..config._types import AgentConfig

# PR #319 (lead msg a456b610 2026-06-06): provider-aware tool whitelist.
#
# Root cause v8: LiteLLM 1.52.16's Anthropic-shim doesn't recognize newer
# Claude Code builtins (ExitPlanMode, BashOutput, KillShell — added after
# the LiteLLM version pinned in the cohort's vLLM stack). The shim's
# pydantic Union of recognized AnthropicTool subclasses falls through to
# the last subclass (``AnthropicComputerTool``), which requires
# ``display_width_px`` that the unknown tool's payload doesn't have →
# 422 on every API call → capsule errors all 60 turns.
#
# Fix: when a provider backend is active, REGISTER only the
# shim-recognized tool set so unrecognized builtins never enter the
# outbound ``tools[]`` array. ``ClaudeAgentOptions.tools`` is the
# registration knob — maps to ``--tools <csv>`` per the SDK transport
# layer at ``claude_agent_sdk/_internal/transport/subprocess_cli.py
# :241-250``, where the CLI honours it as "the list of available tools
# from the built-in set" (CLI ``--help``).
#
# Spec-side override: ``spec.claude.provider.allowed_tools: list[str]``
# lets an operator declare their shim's recognized set explicitly. When
# absent, the default below applies. The default is calibrated for
# LiteLLM-1.52.16-known tools (clew bm172 cohort 2026-06-06 baseline) +
# the ``Agent`` subagent registrar; bump this list as the shim ecosystem
# catches up.
_PROVIDER_DEFAULT_ALLOWED_TOOLS: tuple[str, ...] = (
    "Bash",
    "Read",
    "Edit",
    "Write",
    "Glob",
    "Grep",
    "WebFetch",
    "WebSearch",
    "TodoWrite",
    "Task",
    "NotebookEdit",
    "Agent",
)

__all__ = [
    "SDKCommonError",
    "provision_anthropic_auth",
    "resolve_agent_workspace",
    "build_sdk_options",
    "project_runtime_root",
]


def project_runtime_root(config: "AgentConfig") -> "Path | None":
    """If the agent's YAML lives under a project-scope
    ``.scitex/agent-container/`` tree, return the sibling ``runtime/``
    so per-agent state lands inside the same repo. Otherwise None.

    In-repo test agents get in-repo state, keeping ``~/.scitex`` clean
    and letting CI snapshot transcripts as build artifacts.
    """
    src = getattr(config, "config_path", "") or ""
    if not src:
        return None
    try:
        from scitex_config._ecosystem import local_state
    except Exception:  # stx-allow: fallback (reason: scitex-config optional; degrade to home-scope state)
        return None
    scope = local_state.find_project_scope("agent-container", start=Path(src).parent)
    return (scope / "runtime") if scope is not None else None


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


def _container_skills_root() -> Path | None:
    """Resolve the in-container ``$HOME/.claude/skills`` directory, or None.

    The ``to_home`` materializer drops every skill at exactly this
    prefix inside the container (see ``_skills/scitex-agent-container/
    25_claude-setup-delivery.md``). Detection mirrors the existing
    ``_container_settings_path`` resolver: read ``$HOME`` directly
    (the runner already executes inside the container, so ``$HOME``
    points at the container's home), regardless of whether the
    apptainer env vars are set.

    Returns ``None`` when ``$HOME`` is unset (rare; defensive).
    Returns the directory PATH unconditionally when ``$HOME`` is set —
    the existence check is the manifest builder's job (per-skill,
    not whole-dir). A missing whole-skills dir is a valid state
    (some agents have no skills mounted); the manifest builder then
    rolls every named skill into a path-only bullet, which is the
    correct "operator must see the gap" signal.
    """
    home = os.environ.get("HOME")
    if not home:
        return None
    return Path(home) / ".claude" / "skills"


def _container_settings_path() -> str | None:
    """Resolve the in-container ``settings.local.json`` path, or ``None``.

    sac mirrors the agent's ``to_home`` tree (including
    ``.claude/settings.local.json``) into the container ``$HOME`` — both via
    the workspace-home bind (hardened mode) and via the overlay upper home
    (relaxed ``--home``/``--overlay`` specs). The runner executes INSIDE the
    container, so ``$HOME`` already points at ``/home/agent`` (or whatever
    ``--home`` the spec set). We resolve the settings file against that
    ``$HOME`` and return its path only when the file is present — so a spec
    without a ``settings.local.json`` doesn't aim ``--settings`` at a missing
    file.

    The hook ``command``s inside that settings file use ``$HOME/.claude/...``,
    so they resolve in-container regardless of what ``$HOME`` resolves to.
    """
    home = os.environ.get("HOME")
    if not home:
        return None
    candidate = Path(home) / ".claude" / "settings.local.json"
    return str(candidate) if candidate.is_file() else None


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
# Workspace + MCP wiring
# ---------------------------------------------------------------------------


def _resolve_env_refs(value: Any) -> Any:
    """Substitute ``${VAR}`` references against the current process env.

    Strings, dicts, and lists are walked recursively. Any other type is
    passed through unchanged. Unresolved references are left literal so
    misconfigurations are visible at the SDK call rather than silently
    becoming empty strings.
    """
    if isinstance(value, str):
        return _re.sub(
            r"\$\{(\w+)\}",
            lambda m: os.environ.get(m.group(1), m.group(0)),
            value,
        )
    if isinstance(value, dict):
        return {k: _resolve_env_refs(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_resolve_env_refs(v) for v in value]
    return value


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


# Idempotency anchor — same literal as ``_skills_manifest._BLOCK_HEADING``.
# Inlined here (instead of imported) so the build-sdk-options path stays
# readable as a self-contained predicate.
_SKILLS_MANIFEST_HEADING = "## Available skills"


def _augment_system_prompt_with_skills_manifest(
    system_prompt: str | None, agent_cfg: "AgentConfig | None"
) -> str | None:
    """Return the system_prompt augmented with the SOFT skill manifest.

    Returns ``None`` when no augmentation should be applied — caller
    leaves the system_prompt as-is. Returns the new string otherwise.

    The wiring contract (LOCKED — see ``_skills_manifest.py`` for the
    block-shape doctrine):

    * ``agent_cfg`` is ``None`` → no augmentation (no per-agent skills
      surface to consult).
    * ``agent_cfg.labels.skills`` empty / absent → no augmentation.
    * ``system_prompt`` already contains the LOCKED ``## Available
      skills`` heading → already augmented by a prior call on the
      same config; skip (idempotent).
    * Otherwise: parse the CSV, build the block, and APPEND to the
      system_prompt with a single blank line separator. When the
      caller passed ``system_prompt=None`` (no base), the block IS
      the system_prompt.

    Best-effort: any failure to compute the augmentation returns
    ``None`` (caller leaves the prompt alone). The manifest is
    advisory; a malformed config must not block agent start.
    """
    if agent_cfg is None:
        return None
    # The labels attribute is a plain dict[str, str] on AgentConfig.
    labels = getattr(agent_cfg, "labels", None)
    if not isinstance(labels, dict):
        return None
    skills_csv = labels.get("skills") or ""
    skill_names = [s.strip() for s in skills_csv.split(",") if s.strip()]
    if not skill_names:
        return None

    # Idempotency: a system_prompt that already carries the heading
    # was augmented on a prior pass. Skip silently — re-applying would
    # double the block and bloat context on every restart.
    if system_prompt and _SKILLS_MANIFEST_HEADING in system_prompt:
        return None

    skills_root = _container_skills_root()
    if skills_root is None:
        # No $HOME → no in-container skills dir to point at. Refuse to
        # augment rather than emit a manifest whose paths can't resolve.
        return None

    try:
        block = build_skills_manifest_block(skill_names, skills_root)
    except Exception:  # stx-allow: fallback (reason: manifest is advisory; a malformed input must not block agent start)
        return None
    if block is None:
        return None

    if system_prompt:
        return f"{system_prompt}\n\n{block}"
    return block


def resolve_agent_workspace(agent_name: str) -> tuple[dict, str | None]:
    """Resolve ``(mcp_servers, cwd)`` for a registered agent.

    Returns ``({}, None)`` if the agent isn't registered or its workspace
    has no ``.mcp.json``. Best-effort: any IO / parse failure produces
    the empty result rather than raising — the caller (option-builder)
    decides what to do with an unknown workspace.

    The ``mcp_servers`` dict matches the SDK's expected shape: each
    entry has ``type`` defaulted to ``"stdio"`` if absent, and any
    ``${VAR}`` references resolved.
    """
    try:
        from scitex_agent_container._state.registry import Registry
    except Exception:  # stx-allow: fallback (reason: optional dep at runtime; broaden beyond ImportError so a misbuilt transitive dep can't crash the option-builder)
        return {}, None

    try:
        entry = Registry().get(agent_name)
    except Exception:  # stx-allow: fallback (reason: registry IO best-effort)
        return {}, None
    if not entry:
        return {}, None
    config_path = entry.get("config")
    if not config_path:
        return {}, None

    try:
        from scitex_agent_container.config import load_config

        cfg = load_config(config_path)
        workdir = str(Path(cfg.expanded_workdir).expanduser())
    except Exception:  # stx-allow: fallback (reason: config load best-effort)
        return {}, None

    mcp_path = Path(workdir) / ".mcp.json"
    if not mcp_path.is_file():
        return {}, workdir
    try:
        raw = _json.loads(mcp_path.read_text(encoding="utf-8"))
    except (
        OSError,
        _json.JSONDecodeError,
    ):  # stx-allow: fallback (reason: malformed JSON tolerated)
        return {}, workdir

    mcp_servers = raw.get("mcpServers", {}) if isinstance(raw, dict) else {}
    if not isinstance(mcp_servers, dict):
        return {}, workdir

    resolved: dict = {}
    for name, entry_dict in mcp_servers.items():
        if not isinstance(entry_dict, dict):
            continue
        e = _resolve_env_refs(dict(entry_dict))
        e.setdefault("type", "stdio")
        resolved[name] = e
    return resolved, workdir


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
    # Load the AgentConfig ONCE — both the provider-tools whitelist
    # (PR #319, further down) and the skill-manifest augmentation
    # (below) consume the same config. A second silent load would
    # double the registry/disk IO on every option-builder entry.
    _agent_cfg = _load_agent_config_silent(agent_name)
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

    # SOFT skill-manifest augmentation. The per-agent
    # ``metadata.labels.skills`` CSV (parsed at config load into
    # ``AgentConfig.labels``) names which skills the operator wants the
    # agent to know about; we append a bullet-list block to the
    # ``system_prompt`` so the agent discovers them and lazy-loads via
    # the ``Skill`` tool.
    #
    # Why HERE and not at body-inlining time: bodies are NEVER inlined —
    # the skill body is loaded on demand via the Skill tool. The
    # manifest just lists name + first-paragraph description + path. See
    # ``_skills_manifest.py`` module docstring for the full doctrine.
    #
    # Idempotency: when the resolved ``system_prompt`` already contains
    # the LOCKED ``"## Available skills\n"`` heading, the manifest was
    # already appended by a prior call on the same config — skip.
    # Lifecycle paths can re-enter ``build_sdk_options`` more than once
    # per agent start; a doubled block would leak the same advice twice
    # and bloat context.
    _augmented = _augment_system_prompt_with_skills_manifest(
        kwargs.get("system_prompt"), _agent_cfg
    )
    if _augmented is not None:
        kwargs["system_prompt"] = _augmented

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
    # ``settings.local.json`` into the container ``$HOME/.claude/`` via the
    # ``to_home`` mirror; without ``--settings`` the SDK would never load it
    # (empty ``setting_sources`` skips the $HOME settings layer entirely).
    #
    # We resolve the path from the in-container ``$HOME`` (where the runner
    # actually executes) and only set it when the file is present, so a
    # spec without a settings.local.json doesn't point ``--settings`` at a
    # missing file.
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

    # PR #319: provider-aware tool REGISTRATION whitelist. When the
    # agent routes through a non-Anthropic provider (LiteLLM / vLLM /
    # gateway via ``spec.claude.provider``), restrict the registered
    # built-in tool set so unrecognized newer Claude Code builtins
    # (ExitPlanMode, BashOutput, KillShell) never enter the outbound
    # API request body. See the module-level constant docstring for
    # the root-cause + spec contract. Resolution order:
    #
    #   1. ``spec.claude.provider.allowed_tools`` (operator override) —
    #      used verbatim; the operator KNOWS their shim's recognized set.
    #   2. ``_PROVIDER_DEFAULT_ALLOWED_TOOLS`` (runner default) — the
    #      LiteLLM-1.52.16-known set + Agent.
    #
    # Non-provider agents (real Anthropic backend) leave ``tools``
    # unset → CLI registers its full default toolset (back-compat).
    # The caller can also explicitly pass ``tools=...`` via ``extra``;
    # an explicit caller value WINS over this auto-populate.
    from ._apptainer_provider import provider_active

    if _agent_cfg is not None and provider_active(_agent_cfg) and "tools" not in kwargs:
        provider = getattr(getattr(_agent_cfg, "claude", None), "provider", None)
        spec_tools = list(getattr(provider, "allowed_tools", []) or [])
        if spec_tools:
            kwargs["tools"] = spec_tools
        else:
            kwargs["tools"] = list(_PROVIDER_DEFAULT_ALLOWED_TOOLS)

    return ClaudeAgentOptions(**kwargs)
