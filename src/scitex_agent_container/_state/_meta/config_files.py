"""Workspace config file readers — CLAUDE.md and .mcp.json.

Extracted from ``agent_meta.py`` to keep that module under the 512-line
hook ceiling. ``agent_meta`` re-exports every helper so existing
``agent_meta._config_candidates`` / ``_read_claude_md`` /
``_redact_mcp_tree`` / ``_read_mcp_json`` / ``_parse_mcp_servers``
access keeps working.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .secrets import _redact_secrets


def _config_candidates(workdir: str, filename: str) -> list[Path]:
    """Return a prioritised list of candidate locations for ``filename``.

    Historically only ``<workdir>/<filename>`` was probed, which meant
    agents whose workspace wasn't provisioned with that file pushed an
    empty ``claude_md`` / ``mcp_json`` to the hub. Walk a wider set of
    plausible locations so every agent gets populated content:

    1. ``<workdir>/<filename>``
    2. ``<workdir>/.claude/<filename>``   (nested config style)
    3. Legacy sibling ``<workdir-parent>/mamba-<name>/<filename>``
    4. Nearest enclosing git-root ``<filename>``
    5. ``~/.claude/<filename>``           (user-global fallback)
    6. ``~/<filename>``
    """
    home = Path.home()
    cands: list[Path] = []
    if workdir:
        p = Path(workdir)
        cands += [p / filename, p / ".claude" / filename]
        if p.parent.name == "workspaces":
            cands.append(p.parent / f"mamba-{p.name}" / filename)
        # stx-allow: fallback (reason: git root walk can fail on pathological
        # filesystems — missing git root just skips that candidate)
        try:
            git_root = p
            while git_root != git_root.parent and not (git_root / ".git").exists():
                git_root = git_root.parent
            if (git_root / ".git").exists():
                cands.append(git_root / filename)
        except Exception:  # stx-allow: fallback (reason: catch-all safety net — see inline comment for context)
            pass
    cands += [home / ".claude" / filename, home / filename]
    # Dedup preserving order.
    seen: set[str] = set()
    uniq: list[Path] = []
    for c in cands:
        k = str(c)
        if k in seen:
            continue
        seen.add(k)
        uniq.append(c)
    return uniq


def _read_claude_md(workdir: str, max_chars: int = 20_000) -> str:
    for p in _config_candidates(workdir, "CLAUDE.md"):
        # stx-allow: fallback (reason: permission error on one candidate
        # must not prevent trying the next — best-effort file read)
        try:
            if not p.is_file():
                continue
            return p.read_text(errors="replace")[:max_chars]
        except Exception:  # stx-allow: fallback (reason: catch-all safety net — see inline comment for context)
            continue
    return ""


def _redact_mcp_tree(obj):
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            if isinstance(v, str) and any(
                t in k.upper() for t in ("TOKEN", "SECRET", "KEY", "PASSWORD")
            ):
                out[k] = "***REDACTED***"
            else:
                out[k] = _redact_mcp_tree(v)
        return out
    if isinstance(obj, list):
        return [_redact_mcp_tree(x) for x in obj]
    return obj


def _read_mcp_json(workdir: str, max_chars: int = 10_000) -> str:
    for p in _config_candidates(workdir, ".mcp.json"):
        # stx-allow: fallback (reason: permission error on one candidate
        # must not prevent trying the next — best-effort file read)
        try:
            if not p.is_file():
                continue
            raw = p.read_text(errors="replace")
        except Exception:  # stx-allow: fallback (reason: catch-all safety net — see inline comment for context)
            continue
        # stx-allow: fallback (reason: corrupt JSON falls back to raw-with-
        # redaction rather than raising — collect_rich is best-effort)
        try:
            doc = json.loads(raw)
            pretty = json.dumps(_redact_mcp_tree(doc), indent=2)
            return pretty[:max_chars]
        except Exception:  # stx-allow: fallback (reason: catch-all safety net — see inline comment for context)
            return _redact_secrets(raw[:max_chars])
    return ""


def _parse_mcp_servers(workdir: str) -> list[dict[str, Any]]:
    """Return a structured summary of MCP servers configured for this agent.

    Parses ``<workdir>/.mcp.json`` into a flat list of
    ``{name, transport, url_host, command}`` entries so the dashboard
    can render a setup-audit table alongside installed plugins. URL
    hosts (not full URLs) and commands (not args) are surfaced because
    that is enough to verify the server is pointing at the right
    endpoint without exposing query-string secrets.

    Returns [] if the file is missing or malformed — callers never get
    ``None``.
    """
    try:
        p = Path(workdir) / ".mcp.json"
        if not p.is_file():
            return []
        doc = json.loads(p.read_text(errors="replace"))
    except Exception:
        return []
    if not isinstance(doc, dict):
        return []
    servers = doc.get("mcpServers")
    if not isinstance(servers, dict):
        return []
    out: list[dict[str, Any]] = []
    for sname, sconf in servers.items():
        if not isinstance(sconf, dict):
            continue
        transport = sconf.get("type") or sconf.get("transport")
        url_host: str | None = None
        url_val = sconf.get("url")
        if isinstance(url_val, str):
            try:
                from urllib.parse import urlparse

                url_host = urlparse(url_val).hostname or None
            except Exception:
                url_host = None
        command = sconf.get("command")
        if not isinstance(command, str):
            command = None
        out.append(
            {
                "name": sname,
                "transport": transport if isinstance(transport, str) else None,
                "url_host": url_host,
                "command": command,
            }
        )
    return out
