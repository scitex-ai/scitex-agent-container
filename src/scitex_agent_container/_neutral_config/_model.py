"""Load a harness-neutral agent-config layer (``.agents/``) — ADR-0033.

A layer is one directory::

    AGENTS.md                instructions (the only instruction file)
    *.md                     extra docs AGENTS.md may reference (e.g. RTK.md)
    skills/<name>/SKILL.md   Agent Skills open standard
    subagents/<name>.md      frontmatter (name, description, model) + prompt body
    commands/<name>.md       frontmatter (description, argument-hint) + body ($ARGUMENTS)
    hooks/                   hook scripts
    hooks.yaml               hook registrations (see :class:`Hook`)
    mcp.yaml                 ``servers: {name: {transport, command, args, env, url, headers}}``
    settings.yaml            ``model``, ``env``, ``permissions``, ``native: {<harness>: {...}}``

Two layers exist: the user layer (``~/.scitex/agent-container/.agents``) and the
project layer (``<git-root>/.scitex/agent-container/.agents``); the project
layer wins (:func:`merge_layers`).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

# Neutral hook events. Every file-based harness has these (ADR-0033 §Hooks).
# A harness-only event is written ``<harness>:<NativeEvent>`` (e.g.
# ``claude:WorktreeCreate``) and is skipped, with a report line, by the
# other adapters.
NEUTRAL_EVENTS = (
    "pre_tool",
    "post_tool",
    "stop",
    "session_start",
    "prompt_submit",
    "notification",
)

_SKIP_DOCS = {"AGENTS.md", "README.md"}


class NeutralConfigError(ValueError):
    """A neutral layer is malformed; the message names the file and field."""


@dataclass(frozen=True)
class Hook:
    """One hook registration from ``hooks.yaml``.

    ``command`` is relative to the layer root (``hooks/x.sh``) or verbatim
    (an absolute path, ``$HOME/...``, or a command line such as ``rtk hook``).
    ``matcher`` is a tool-name pattern in the Claude tool vocabulary
    (``Bash``, ``Write|Edit``, regex); empty means every tool.
    """

    name: str
    event: str
    command: str
    matcher: str = ""
    timeout: int | None = None

    @property
    def harness(self) -> str | None:
        """The harness a ``<harness>:<Event>`` hook is limited to, else None."""
        return self.event.split(":", 1)[0] if ":" in self.event else None


@dataclass
class Layer:
    root: Path
    agents_md: Path | None = None
    docs: dict[str, Path] = field(default_factory=dict)
    skills: dict[str, Path] = field(default_factory=dict)
    subagents: dict[str, Path] = field(default_factory=dict)
    commands: dict[str, Path] = field(default_factory=dict)
    hooks_dir: Path | None = None
    hooks: list[Hook] = field(default_factory=list)
    disabled_hooks: list[str] = field(default_factory=list)
    mcp_servers: dict[str, dict[str, Any]] = field(default_factory=dict)
    settings: dict[str, Any] = field(default_factory=dict)


def _read_yaml(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        raise NeutralConfigError(f"{path}: top level must be a mapping")
    return data


def _named_entries(directory: Path, *, suffix: str | None) -> dict[str, Path]:
    if not directory.is_dir():
        return {}
    out: dict[str, Path] = {}
    for entry in sorted(directory.iterdir()):
        if entry.name.startswith("."):
            continue
        if suffix is None:
            if entry.is_dir():
                out[entry.name] = entry
        elif entry.is_file() and entry.name.endswith(suffix):
            out[entry.name[: -len(suffix)]] = entry
    return out


def _parse_hooks(path: Path) -> tuple[list[Hook], list[str]]:
    data = _read_yaml(path)
    hooks: list[Hook] = []
    seen: set[str] = set()
    for i, raw in enumerate(data.get("hooks") or []):
        if not isinstance(raw, dict):
            raise NeutralConfigError(f"{path}: hooks[{i}] must be a mapping")
        for key in ("name", "event", "command"):
            if not raw.get(key):
                raise NeutralConfigError(f"{path}: hooks[{i}] needs '{key}'")
        event = str(raw["event"])
        if ":" not in event and event not in NEUTRAL_EVENTS:
            raise NeutralConfigError(
                f"{path}: hooks[{i}] event '{event}' is not one of "
                f"{', '.join(NEUTRAL_EVENTS)} (or '<harness>:<Event>')"
            )
        name = str(raw["name"])
        if name in seen:
            raise NeutralConfigError(f"{path}: duplicate hook name '{name}'")
        seen.add(name)
        timeout = raw.get("timeout")
        hooks.append(
            Hook(
                name=name,
                event=event,
                command=str(raw["command"]),
                matcher=str(raw.get("matcher") or ""),
                timeout=int(timeout) if timeout is not None else None,
            )
        )
    disabled = [str(n) for n in data.get("disable") or []]
    return hooks, disabled


def load_layer(root: Path) -> Layer:
    """Read one ``.agents`` layer. A missing directory is an empty layer."""
    root = Path(root)
    layer = Layer(root=root)
    if not root.is_dir():
        return layer
    agents_md = root / "AGENTS.md"
    layer.agents_md = agents_md if agents_md.is_file() else None
    layer.docs = {
        p.name: p
        for p in sorted(root.glob("*.md"))
        if p.is_file() and p.name not in _SKIP_DOCS
    }
    layer.skills = _named_entries(root / "skills", suffix=None)
    layer.subagents = _named_entries(root / "subagents", suffix=".md")
    layer.commands = _named_entries(root / "commands", suffix=".md")
    hooks_dir = root / "hooks"
    layer.hooks_dir = hooks_dir if hooks_dir.is_dir() else None
    layer.hooks, layer.disabled_hooks = _parse_hooks(root / "hooks.yaml")
    servers = _read_yaml(root / "mcp.yaml").get("servers") or {}
    if not isinstance(servers, dict):
        raise NeutralConfigError(f"{root / 'mcp.yaml'}: 'servers' must be a mapping")
    layer.mcp_servers = {str(k): dict(v or {}) for k, v in servers.items()}
    layer.settings = _read_yaml(root / "settings.yaml")
    return layer


def _deep_merge(base: dict[str, Any], top: dict[str, Any]) -> dict[str, Any]:
    out = dict(base)
    for key, value in top.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = value
    return out


def merge_layers(user: Layer, project: Layer) -> Layer:
    """Combine user and project layers; the project wins (ADR-0033 §Precedence).

    Named entries (skills, subagents, commands, docs, MCP servers): same name,
    project wins. Hooks: union, user first; the project removes a user hook
    only by naming it in ``hooks.yaml: disable``. Settings: deep merge, project
    wins, except permission lists, which are unioned so a user-level deny can
    never be dropped by a project.
    """
    merged = Layer(root=project.root if project.root.is_dir() else user.root)
    merged.agents_md = project.agents_md or user.agents_md
    for attr in ("docs", "skills", "subagents", "commands", "mcp_servers"):
        setattr(merged, attr, {**getattr(user, attr), **getattr(project, attr)})
    merged.hooks_dir = project.hooks_dir or user.hooks_dir
    disabled = set(project.disabled_hooks)
    merged.hooks = [h for h in user.hooks if h.name not in disabled] + list(
        project.hooks
    )
    settings = _deep_merge(user.settings, project.settings)
    perms: dict[str, list[str]] = {}
    for source in (user.settings, project.settings):
        for kind, rules in (source.get("permissions") or {}).items():
            bucket = perms.setdefault(kind, [])
            bucket.extend(r for r in rules or [] if r not in bucket)
    if perms:
        settings["permissions"] = perms
    merged.settings = settings
    return merged


__all__ = [
    "NEUTRAL_EVENTS",
    "Hook",
    "Layer",
    "NeutralConfigError",
    "load_layer",
    "merge_layers",
]
