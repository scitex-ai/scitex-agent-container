"""Pre-configure a ``.claude/`` settings file to suppress interactive prompts.

Claude Code shows TUI prompts for --dangerously-skip-permissions and
--dangerously-load-development-channels on startup. These prompts block
headless agents because screen/tmux keystroke injection is unreliable
in Claude Code's raw terminal mode.

The fix: write the right settings *before* launching Claude Code so it
never shows these prompts at all.

Filename — ``settings.json`` vs ``settings.local.json`` (it MATTERS):
    Claude Code discovers settings by SCOPE, keyed off the filename and
    its directory (official docs: code.claude.com/docs/en/settings.md):

      * ``<cwd>/.claude/settings.json``        → PROJECT scope
      * ``<cwd>/.claude/settings.local.json``  → LOCAL (project) scope
      * ``$HOME/.claude/settings.json``        → USER scope

    There is NO ``$HOME/.claude/settings.local.json`` — ``.local.json``
    only ever exists at PROJECT scope. So a hook suite written to the
    container ``$HOME`` MUST land in ``settings.json`` to be discovered
    by the interactive TUI at USER scope; a ``$HOME/.claude/
    settings.local.json`` is read by no one (and ``--settings`` is a
    no-op for the interactive TUI — it replaces discovery and applies
    only to print/SDK mode). ``setup_settings_json`` /
    ``cleanup_settings_json`` therefore take a ``filename=`` argument:
    the default ``settings.local.json`` stays correct for PROJECT/workdir-
    scope callers (the host tmux runner writes the agent's project
    ``.claude/``), while the TUI runtime passes ``filename="settings.json"``
    for its container ``$HOME`` writes.

Settings written:
- skipDangerousModePermissionPrompt: true
    Skips the "Bypass Permissions" radio selector.
- enableAllProjectMcpServers: true
    Auto-enables MCP servers from .mcp.json without asking.
- enabledMcpjsonServers: [<server names>]
    Explicitly whitelists the MCP servers defined in .mcp.json.

The file is merged (not overwritten) so user-added settings survive.
Cleanup removes only the keys this module managed.

Global seed (ensure_global_settings_json):
- Also ensures ~/.claude/settings.json exists and is not a broken symlink.
- If missing or broken, drops a minimal seed from
  ~/.scitex/orochi/templates/claude-code-seed.json (or a built-in default).
- Idempotent: no-op when the global file already resolves correctly.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path

from ..config import AgentConfig

# The Claude ``hooks``-block algebra lives in its own module (pure dict->dict,
# no config/filesystem dependency). Re-exported here because callers — and
# tests — import these names through ``settings_json``.
from ._settings_hooks import (  # noqa: F401
    _exclude_hooks,
    _merge_hooks_blocks,
    _strip_stale_sac_ingest_hooks,
)

logger = logging.getLogger(__name__)

# Keys managed by this module — cleanup removes exactly these.
_MANAGED_KEYS = frozenset(
    {
        "skipDangerousModePermissionPrompt",
        "enableAllProjectMcpServers",
        "enabledMcpjsonServers",
        "hooks",
        "statusLine",
    }
)

# Hook config pushed into every spawned agent's settings file so
# PreToolUse / PostToolUse / UserPromptSubmit / Stop events flow into
# the per-agent event ring-buffer (~/.scitex/agent-container/runtime/events/
# <agent>.jsonl). Consumed by event_log.summarize() which feeds the
# Orochi dashboard's Last tool / Last MCP / Last action rows. Without
# this wiring those rows render as dashes (scitex-orochi todo#59).
_HOOKS_CONFIG = {
    "PreToolUse": [
        {
            "matcher": "",
            "hooks": [
                {
                    "type": "command",
                    "command": "scitex-agent-container event ingest pretool",
                }
            ],
        }
    ],
    "PostToolUse": [
        {
            "matcher": "",
            "hooks": [
                {
                    "type": "command",
                    "command": "scitex-agent-container event ingest posttool",
                }
            ],
        }
    ],
    "UserPromptSubmit": [
        {
            "matcher": "",
            "hooks": [
                {
                    "type": "command",
                    "command": "scitex-agent-container event ingest prompt",
                }
            ],
        }
    ],
    "Stop": [
        {
            "matcher": "",
            "hooks": [
                {
                    "type": "command",
                    "command": "scitex-agent-container event ingest stop",
                },
                # The never-stop actuator. While the agent's board holds
                # runnable work, this CONVERTS the stop into taking the next
                # item: the completion of one unit of work is the trigger to
                # pull the next, so "idle with work pending" is unreachable
                # by design rather than a state something notices later and
                # repairs. (Incident 2026-07-18: an agent sat idle at its
                # prompt for 80+ minutes holding 5 in_progress cards, and the
                # OPERATOR noticed it twice; a notification was sent and
                # changed nothing, because a stopped agent reads nothing.)
                #
                # Blocking alone would not be enough — a refused stop leaves
                # the agent sitting there idle — so the hook hands back the
                # detector's parsed next_action list as the continuation
                # prompt. Fails OPEN (allows the stop, logs loudly) whenever
                # the detector cannot be read. See the _never_stop package.
                {
                    "type": "command",
                    "command": "scitex-agent-container take-next-item",
                },
            ],
        }
    ],
}


def _load_settings_dict(path: Path) -> dict:
    """Read a settings JSON file as a dict, or ``{}`` on any problem.

    Tolerates a missing file, malformed JSON, and a non-dict top-level
    payload — every caller here treats "unreadable" as "no settings yet"
    and writes a clean managed payload over it.
    """
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text())
    except (
        json.JSONDecodeError,
        OSError,
    ):  # stx-allow: fallback (reason: malformed JSON tolerated)
        return {}
    return data if isinstance(data, dict) else {}


def _mcp_server_names(config: AgentConfig, workdir: str) -> list[str]:
    """Collect MCP server names from config and on-disk .mcp.json."""
    names: set[str] = set()

    # From config.mcp_servers (v2 path)
    if config.mcp_servers:
        names.update(config.mcp_servers.keys())

    # From on-disk .mcp.json (may have been written by setup_mcp_config or
    # deploy_to_home earlier in the start flow)
    mcp_path = Path(workdir) / ".mcp.json"
    if mcp_path.exists():
        try:
            data = json.loads(mcp_path.read_text())
            names.update(data.get("mcpServers", {}).keys())
        except (
            json.JSONDecodeError,
            OSError,
        ):  # stx-allow: fallback (reason: malformed JSON tolerated)
            pass

    return sorted(names)


def _needs_skip_permissions(config: AgentConfig) -> bool:
    """Check if config uses --dangerously-skip-permissions."""
    return any("--dangerously-skip-permissions" in f for f in config.claude.flags)


def _needs_dev_channels(config: AgentConfig) -> bool:
    """Check if config uses --dangerously-load-development-channels."""
    return any(
        "--dangerously-load-development-channels" in f for f in config.claude.flags
    )


_SEED_DEFAULTS: dict = {
    "skipDangerousModePermissionPrompt": True,
    "promptSuggestionEnabled": False,
    "permissions": {
        "allow": [
            "Bash(*)",
            "Read(*)",
            "Write(*)",
            "Edit(*)",
            "Glob(*)",
            "Grep(*)",
            "Agent(*)",
            "mcp__scitex-orochi__*",
            "mcp__scitex__*",
            "mcp__filesystem__*",
        ]
    },
}

_SEED_TEMPLATE = (
    Path.home() / ".scitex" / "agent-container" / "templates" / "claude-code-seed.json"
)


def ensure_global_settings_json() -> None:
    """Ensure ~/.claude/settings.json exists and is not a broken symlink.

    If the file is missing or the symlink target does not exist, writes a
    minimal seed so Claude Code suppresses all interactive onboarding and
    permission prompts on the next launch.  Existing valid files are left
    unchanged (idempotent).
    """
    global_path = Path.home() / ".claude" / "settings.json"

    # Resolve: if symlink, check the *target* exists.
    is_broken = global_path.is_symlink() and not global_path.exists()
    is_missing = not global_path.exists() and not global_path.is_symlink()

    if not (is_broken or is_missing):
        return  # already healthy — nothing to do

    seed: dict
    if _SEED_TEMPLATE.exists():
        try:
            seed = json.loads(_SEED_TEMPLATE.read_text())
            seed.pop("_comment", None)
        except (json.JSONDecodeError, OSError):
            seed = _SEED_DEFAULTS.copy()
    else:
        seed = _SEED_DEFAULTS.copy()

    if is_broken:
        # Remove the broken symlink so we can write a real file.
        try:
            os.unlink(global_path)
        except OSError as exc:
            logger.warning("Could not remove broken symlink %s: %s", global_path, exc)
            return

    global_path.parent.mkdir(parents=True, exist_ok=True)
    global_path.write_text(json.dumps(seed, indent=2) + "\n")
    logger.info(
        "Seeded %s (was %s)",
        global_path,
        "broken symlink" if is_broken else "missing",
    )


def setup_settings_json(
    config: AgentConfig,
    workdir: str,
    *,
    filename: str = "settings.local.json",
) -> None:
    """Write ``.claude/<filename>`` to pre-accept interactive prompts.

    Merges with any existing content so user settings are preserved.
    Only writes keys that are relevant to the agent's flags.

    ``filename`` selects which settings file in ``<workdir>/.claude/`` to
    write. It MUST match the discovery scope of the directory:

      * ``settings.local.json`` (default) — PROJECT/LOCAL scope. Correct
        when ``workdir`` is the agent's project root (the host tmux
        runner's ``config.expanded_workdir``); Claude discovers it at
        ``<cwd>/.claude/settings.local.json``.
      * ``settings.json`` — pass this when ``workdir`` is the container
        ``$HOME``. The interactive TUI reads hooks from ``$HOME/.claude/
        settings.json`` at USER scope; it does NOT read a
        ``$HOME/.claude/settings.local.json`` (no such scope exists), so
        writing ``.local.json`` there leaves the whole hook suite INERT.
        See the module docstring.
    """
    settings: dict = {}

    if _needs_skip_permissions(config):
        settings["skipDangerousModePermissionPrompt"] = True

    if _needs_dev_channels(config):
        settings["enableAllProjectMcpServers"] = True
        server_names = _mcp_server_names(config, workdir)
        if server_names:
            settings["enabledMcpjsonServers"] = server_names

    # Always inject hook wiring — even if skip-permissions / dev-channels
    # aren't active we still want Last tool / Last MCP / Last action rows
    # to populate on the dashboard (scitex-orochi todo#59).
    settings["hooks"] = _HOOKS_CONFIG

    # Register sac-statusline as the statusLine command so the JSON payload
    # is persisted to ~/.scitex/agent-container/runtime/statusline/<agent>.json each
    # turn. sac agent status prefers this authoritative source over the JSONL
    # approximation (sac issue #52). No-op if claude-hud is absent — the
    # script falls back to a minimal echo.
    settings["statusLine"] = {"type": "command", "command": "sac-statusline"}

    if not settings:
        return

    claude_dir = Path(workdir) / ".claude"
    settings_path = claude_dir / filename

    # Merge with existing file
    existing = _load_settings_dict(settings_path)

    # Filename migration (settings.local.json -> settings.json): when we are
    # writing the USER-scope ``settings.json`` but a legacy sibling
    # ``settings.local.json`` is present in the same ``.claude/`` dir, fold its
    # content into the merge base and REMOVE it. ``deploy_to_home`` may still
    # land the ``_shared`` baseline (honest-grounding Stop gate / lint
    # PostToolUse) under the old name on hosts whose baseline source has not
    # been renamed yet; without this fold the deep-merge below would have an
    # empty base and the baseline gate would survive ONLY in a
    # ``$HOME/.claude/settings.local.json`` — a path the interactive TUI never
    # reads at user scope, leaving the gate INERT. Folding + deleting the
    # sibling keeps every critical hook in the one file the TUI discovers and
    # avoids a stale split-brain copy. No-op for project/workdir-scope callers
    # (filename == settings.local.json) and when no sibling exists.
    if filename == "settings.json":
        legacy_path = claude_dir / "settings.local.json"
        if legacy_path.is_file():
            legacy = _load_settings_dict(legacy_path)
            if legacy:
                legacy_hooks = legacy.pop("hooks", None)
                # Non-hook keys: keep already-present ``existing`` values
                # (settings.json wins over the legacy sibling).
                for key, val in legacy.items():
                    existing.setdefault(key, val)
                if isinstance(legacy_hooks, dict):
                    existing["hooks"] = _merge_hooks_blocks(
                        existing.get("hooks"), legacy_hooks
                    )
            legacy_path.unlink(missing_ok=True)
            logger.info(
                "Migrated legacy %s into %s (settings.local.json -> "
                "settings.json at user scope)",
                legacy_path,
                settings_path,
            )

    # For enabledMcpjsonServers, merge lists rather than replace
    if "enabledMcpjsonServers" in settings and "enabledMcpjsonServers" in existing:
        merged = set(existing["enabledMcpjsonServers"])
        merged.update(settings["enabledMcpjsonServers"])
        settings["enabledMcpjsonServers"] = sorted(merged)

    # Deep-merge the hooks block (per event) so pre-existing hooks deployed via
    # to_home — notably a project's _shared baseline honest-grounding Stop gate
    # (clew verify --strict) and scitex-io lint PostToolUse hook — SURVIVE
    # alongside SAC's event-ring-buffer hooks. A plain existing.update() clobbers
    # them with _HOOKS_CONFIG, leaving the harness gate INERT (observed: the
    # deployed settings.hooks lost the baseline gate, so it never fired and an
    # agent could reach DONE with an empty clew DAG). Concatenate per event,
    # de-duping identical matcher-groups so re-runs stay idempotent.
    if isinstance(existing.get("hooks"), dict) and isinstance(
        settings.get("hooks"), dict
    ):
        # SAC owns its ingest hook: prune any prior-form copy (the renamed-away
        # ``ingest-hook-event``) from the base so it cannot survive in duplicate
        # and block every prompt via its deprecation-shim error (2026-06-23).
        base_hooks = _strip_stale_sac_ingest_hooks(existing["hooks"])
        settings["hooks"] = _merge_hooks_blocks(base_hooks, settings["hooks"])

    existing.update(settings)

    # Opt-out: drop hooks the spec listed in ``exclude_hooks`` (No-Surprise —
    # the operator SAW the full set via `sac agents explain`, then switched
    # specific ones off). Applied LAST, to the fully-merged hooks.
    excludes = list(getattr(config, "exclude_hooks", []) or [])
    if excludes and isinstance(existing.get("hooks"), dict):
        existing["hooks"] = _exclude_hooks(existing["hooks"], excludes)

    settings_path.parent.mkdir(parents=True, exist_ok=True)
    settings_path.write_text(json.dumps(existing, indent=2) + "\n")

    logger.info(
        "Pre-configured .claude/%s for %s at %s (keys: %s)",
        filename,
        config.name,
        settings_path,
        ", ".join(sorted(settings.keys())),
    )


def cleanup_settings_json(
    config: AgentConfig,
    workdir: str,
    *,
    filename: str = "settings.local.json",
) -> None:
    """Remove managed keys from ``.claude/<filename>`` on agent stop.

    Preserves any user-added keys. Deletes the file only if it becomes
    empty after cleanup. ``filename`` must match the value the matching
    :func:`setup_settings_json` call wrote (default ``settings.local.json``
    for project/workdir scope; ``settings.json`` for the container
    ``$HOME``).
    """
    settings_path = Path(workdir) / ".claude" / filename
    if not settings_path.exists():
        return

    try:
        data = json.loads(settings_path.read_text())
    except (
        json.JSONDecodeError,
        OSError,
    ):  # stx-allow: fallback (reason: malformed JSON tolerated)
        return

    if not isinstance(data, dict):
        return

    removed = []
    for key in _MANAGED_KEYS:
        if key in data:
            del data[key]
            removed.append(key)

    if not removed:
        return

    if not data:
        settings_path.unlink(missing_ok=True)
        logger.info("Removed empty .claude/%s at %s", filename, settings_path)
    else:
        settings_path.write_text(json.dumps(data, indent=2) + "\n")
        logger.info(
            "Removed managed keys from .claude/%s at %s: %s",
            filename,
            settings_path,
            ", ".join(removed),
        )
