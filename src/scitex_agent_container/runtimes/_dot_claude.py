"""Materialize ``<spec.dot_claude>/`` into the agent's workdir.

Replaces the legacy ``src_CLAUDE.md`` / ``src_mcp.json`` / ``src_env`` /
``src_state.md`` sibling-file convention with one directory:

    agents/<name>/
    ├── spec.yaml                  (spec.dot_claude: ./dot_claude)
    └── dot_claude/
        ├── CLAUDE.md              → <workdir>/CLAUDE.md       (marker-protected)
        ├── .mcp.json              → <workdir>/.mcp.json       (per-server merge)
        ├── .env                   → <workdir>/.env            (full overwrite, 0600)
        ├── state.md               → <workdir>/state.md        (full overwrite)
        ├── commands/              → <workdir>/.claude/commands/   (mirror)
        ├── skills/                → <workdir>/.claude/skills/     (mirror)
        ├── hooks/                 → <workdir>/.claude/hooks/      (mirror)
        └── <other>/               → <workdir>/.claude/<rel>       (mirror)

The four well-known leaf files (CLAUDE.md, .mcp.json, .env, state.md)
keep the legacy semantics:

  - **CLAUDE.md** preserves the user-editable tail past END_MARKER and
    refuses to deploy if existing markers are malformed (no silent data
    loss).
  - **.mcp.json** uses per-server replace: servers declared in the
    source overwrite same-named entries in the workspace copy;
    workspace-only servers (e.g. user-added local tools) are preserved.
  - **.env** is a full overwrite, written with mode 0600.
  - **state.md** is a full overwrite (no marker protocol).

Anything else under ``dot_claude/`` is mirrored verbatim into
``<workdir>/.claude/<rel>`` so commands/, skills/, hooks/, agents/,
settings/ etc. propagate without sac needing to know about each.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
from datetime import datetime
from pathlib import Path
from typing import Iterable

from ..config import AgentConfig
from .claude_md import build_skills_lines

logger = logging.getLogger(__name__)

END_MARKER = "<!-- End of scitex-agent-container generated section -->"
START_MARKER_PREFIX = "<!-- Start of scitex-agent-container generated section"

# Leaf files under dot_claude/ that map to workdir root (not workdir/.claude/).
_WORKDIR_ROOT_FILES = {"CLAUDE.md", ".mcp.json", ".env", "state.md"}


class WorkspaceCLAUDEMarkerError(RuntimeError):
    """Existing workspace CLAUDE.md has malformed Start/End markers.

    The deploy is hard-aborted on this error rather than silently
    overwriting or guessing — preserving user content past the End
    marker is a safety contract and any ambiguity in marker placement
    could destroy work.
    """


# --- helpers ---------------------------------------------------------------


def _spec_dir(config: AgentConfig) -> Path | None:
    if not config.config_path:
        return None
    return Path(config.config_path).parent


def resolve_dot_claude_dir(config: AgentConfig) -> Path | None:
    """Resolve ``spec.dot_claude`` to an absolute directory.

    Resolution order:
      1. If ``spec.dot_claude`` is an absolute path: use it.
      2. If ``spec.dot_claude`` is relative: resolve against the
         directory containing ``spec.yaml``.
      3. If ``spec.dot_claude`` is empty: auto-discover ``./dot_claude``
         next to ``spec.yaml`` if it exists.

    Returns ``None`` if no directory can be resolved (legacy specs
    without a dot_claude/ dir simply skip the deploy).
    """
    spec_dir = _spec_dir(config)
    raw = (config.dot_claude or "").strip()
    if not raw:
        if spec_dir is not None and (spec_dir / "dot_claude").is_dir():
            return spec_dir / "dot_claude"
        return None
    p = Path(raw).expanduser()
    if not p.is_absolute():
        if spec_dir is None:
            return None
        p = spec_dir / p
    return p if p.is_dir() else None


def _validate_marker_invariants(text: str, source_name: str) -> None:
    """Hard-fail if Start/End markers are missing or malformed."""
    start_count = text.count(START_MARKER_PREFIX)
    end_count = text.count(END_MARKER)
    if start_count != 1 or end_count != 1:
        raise WorkspaceCLAUDEMarkerError(
            f"{source_name}: expected exactly 1 Start marker and 1 End "
            f"marker, found Start={start_count} End={end_count}. "
            "Refusing to deploy to avoid data loss. Restore the markers "
            "manually before retrying."
        )
    if text.find(START_MARKER_PREFIX) > text.find(END_MARKER):
        raise WorkspaceCLAUDEMarkerError(
            f"{source_name}: Start marker appears AFTER End marker. "
            "This indicates a corrupted file. Refusing to deploy."
        )


def _extract_user_tail(workspace_path: Path) -> str:
    if not workspace_path.exists():
        return ""
    try:
        existing = workspace_path.read_text()
    except OSError:  # stx-allow: fallback (reason: file system operation failure)
        return ""
    idx = existing.rfind(END_MARKER)
    if idx == -1:
        return ""
    return existing[idx + len(END_MARKER) :]


def _interpolate_env(text: str) -> str:
    return re.sub(
        r"\$\{(\w+)\}",
        lambda m: os.environ.get(m.group(1), m.group(0)),
        text,
    )


def _interpolate_metadata(text: str, config: AgentConfig) -> str:
    def _replace(m: re.Match) -> str:
        key = m.group(1)
        if key == "metadata.name":
            return config.name
        if key.startswith("metadata.labels."):
            label = key[len("metadata.labels.") :]
            return config.labels.get(label) or m.group(0)
        return m.group(0)

    return re.sub(r"\$\{([^}]+)\}", _replace, text)


# --- per-leaf deploys (CLAUDE.md, .mcp.json, .env, state.md) ---------------


def _deploy_claude_md(config: AgentConfig, root: Path, workdir: str) -> None:
    src = root / "CLAUDE.md"
    if not src.exists():
        return

    section_content = src.read_text().strip()
    if not section_content:
        return

    section_content = _interpolate_metadata(section_content, config)

    dest = Path(workdir) / "CLAUDE.md"

    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    start_tag = (
        f"<!-- Start of scitex-agent-container generated section ({timestamp}) -->"
    )

    section_body = re.sub(
        r"<!--.*?scitex-agent-container.*?-->\n?", "", section_content
    ).strip()
    guide_comment = (
        "<!-- ================================================================\n"
        "     CUSTOM CONTENT — edit freely below this line.\n"
        "     The block above (Start marker → End marker, inclusive) is\n"
        "     AUTO-GENERATED by scitex-agent-container and will be OVERWRITTEN\n"
        "     on every agent restart. Do NOT edit or delete the markers or\n"
        "     anything between them — all changes there will be lost.\n"
        "     ================================================================ -->"
    )
    skills_lines = build_skills_lines(config)
    skills_block = ("\n" + "\n".join(skills_lines)).rstrip() if skills_lines else ""
    new_content = (
        f"{start_tag}\n{section_body}\n{skills_block}\n{END_MARKER}\n{guide_comment}\n"
        if skills_block
        else f"{start_tag}\n{section_body}\n{END_MARKER}\n{guide_comment}\n"
    )

    existing_text = dest.read_text() if dest.exists() else ""
    if existing_text.strip():
        _validate_marker_invariants(existing_text, str(dest))

    user_tail = _extract_user_tail(dest)
    if user_tail:
        user_tail = re.sub(
            r"\n?<!--\s*={3,}.*?={3,}\s*-->\n?",
            "\n",
            user_tail,
            count=1,
            flags=re.DOTALL,
        )

    if END_MARKER not in new_content:
        logger.warning(
            "dot_claude/CLAUDE.md for %s has no End marker after wrapping; "
            "writing source only, user tail not preserved",
            config.name,
        )
        updated = new_content
    elif user_tail:
        updated = new_content.rstrip("\n") + user_tail
        if not updated.endswith("\n"):
            updated += "\n"
    else:
        updated = new_content

    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(updated)
    logger.info(
        "Deployed dot_claude/CLAUDE.md for %s to %s (preserved user tail: %s)",
        config.name,
        dest,
        "yes" if user_tail else "no",
    )


def _cleanup_claude_md(config: AgentConfig, workdir: str) -> None:
    dest = Path(workdir) / "CLAUDE.md"
    if not dest.exists():
        return

    existing = dest.read_text()

    managed_block = (
        r"\n*<!-- Start of scitex-agent-container generated section.*?-->.*?"
        r"<!-- End of scitex-agent-container generated section -->\n?"
    )
    guide_comment_current = (
        r"<!--\s*=+\s*\n.*?CUSTOM CONTENT.*?edit freely.*?=+\s*-->\n?"
    )
    guide_comment_legacy = r"<!--\s*↓\s*Your custom content.*?-->\n?"

    pattern = f"{managed_block}(?:{guide_comment_current}|{guide_comment_legacy})?"
    updated = re.sub(pattern, "", existing, flags=re.DOTALL)

    if not updated.strip():
        dest.unlink()
        logger.info("Cleaned up CLAUDE.md for %s (file removed)", config.name)
        return
    if updated != existing:
        dest.write_text(updated)
        logger.info("Cleaned up CLAUDE.md for %s at %s", config.name, dest)


def _deploy_mcp_json(config: AgentConfig, root: Path, workdir: str) -> None:
    src = root / ".mcp.json"
    if not src.exists():
        return

    text = src.read_text().strip()
    if not text:
        return

    text = _interpolate_metadata(text, config)
    text = _interpolate_env(text)

    try:
        data = json.loads(text)
    except (
        json.JSONDecodeError
    ) as exc:  # stx-allow: fallback (reason: malformed JSON tolerated)
        logger.warning("Invalid JSON in %s: %s", src, exc)
        return

    dest = Path(workdir) / ".mcp.json"
    existing: dict = {}
    if dest.exists():
        try:
            existing = json.loads(dest.read_text())
        except (
            json.JSONDecodeError,
            OSError,
        ):  # stx-allow: fallback (reason: malformed JSON tolerated)
            pass
    if not isinstance(existing, dict):
        existing = {}

    for server in data.get("mcpServers", {}).values():
        if "args" in server and isinstance(server["args"], list):
            server["args"] = [
                str(Path(a).expanduser()) if a.startswith("~") else a
                for a in server["args"]
            ]

    src_servers = data.get("mcpServers", {})
    existing.setdefault("mcpServers", {}).update(src_servers)

    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(json.dumps(existing, indent=2) + "\n")
    logger.info(
        "Deployed dot_claude/.mcp.json for %s to %s (servers: %s)",
        config.name,
        dest,
        ", ".join(src_servers.keys()),
    )


def _cleanup_mcp_json(config: AgentConfig, root: Path, workdir: str) -> None:
    src = root / ".mcp.json"
    if not src.exists():
        return
    try:
        src_data = json.loads(src.read_text())
    except (
        json.JSONDecodeError,
        OSError,
    ):  # stx-allow: fallback (reason: malformed JSON tolerated)
        return
    keys_to_remove = list(src_data.get("mcpServers", {}).keys())
    if not keys_to_remove:
        return
    dest = Path(workdir) / ".mcp.json"
    if not dest.exists():
        return
    try:
        data = json.loads(dest.read_text())
    except (
        json.JSONDecodeError,
        OSError,
    ):  # stx-allow: fallback (reason: malformed JSON tolerated)
        return
    servers = data.get("mcpServers", {})
    for key in keys_to_remove:
        servers.pop(key, None)
    if not servers:
        dest.unlink(missing_ok=True)
        logger.info("Removed empty .mcp.json at %s", dest)
    else:
        data["mcpServers"] = servers
        dest.write_text(json.dumps(data, indent=2) + "\n")
        logger.info("Cleaned up .mcp.json at %s", dest)


def _deploy_env(config: AgentConfig, root: Path, workdir: str) -> None:
    src = root / ".env"
    if not src.exists():
        return
    text = src.read_text()
    if not text.strip():
        return
    text = _interpolate_metadata(text, config)
    text = _interpolate_env(text)
    dest = Path(workdir) / ".env"
    dest.parent.mkdir(parents=True, exist_ok=True)
    if not text.endswith("\n"):
        text += "\n"
    dest.write_text(text)
    try:
        os.chmod(dest, 0o600)
    except (
        OSError
    ) as exc:  # stx-allow: fallback (reason: file system operation failure)
        logger.warning("Failed to chmod 0600 on %s: %s", dest, exc)
    logger.info("Deployed dot_claude/.env for %s to %s", config.name, dest)


def _cleanup_env(config: AgentConfig, root: Path, workdir: str) -> None:
    src = root / ".env"
    if not src.exists():
        return
    dest = Path(workdir) / ".env"
    if dest.exists():
        dest.unlink()
        logger.info("Cleaned up .env for %s at %s", config.name, dest)


def _deploy_state_md(config: AgentConfig, root: Path, workdir: str) -> None:
    src = root / "state.md"
    if not src.exists():
        return
    text = src.read_text()
    if not text.strip():
        return
    text = _interpolate_metadata(text, config)
    text = _interpolate_env(text)
    dest = Path(workdir) / "state.md"
    dest.parent.mkdir(parents=True, exist_ok=True)
    if not text.endswith("\n"):
        text += "\n"
    dest.write_text(text)
    logger.info("Deployed dot_claude/state.md for %s to %s", config.name, dest)


def _cleanup_state_md(config: AgentConfig, workdir: str) -> None:
    dest = Path(workdir) / "state.md"
    if dest.exists():
        try:
            dest.unlink()
            logger.info("Removed state.md for %s at %s", config.name, dest)
        except (
            OSError
        ) as exc:  # stx-allow: fallback (reason: file system operation failure)
            logger.warning("Failed to remove %s: %s", dest, exc)


# --- mirror the rest of dot_claude/ under workdir/.claude/ -----------------


def _iter_extras(root: Path) -> Iterable[Path]:
    """Yield every direct child of ``root`` that's neither a leaf-file
    handled above nor a dotfile we don't recognize."""
    if not root.is_dir():
        return
    for child in root.iterdir():
        if child.name in _WORKDIR_ROOT_FILES:
            continue
        # Skip backup/swap files but allow legitimate dotfiles like
        # ``.codex/`` or ``.cursor/`` to mirror through.
        if child.name.startswith(".") and child.name in {".DS_Store", ".tmp"}:
            continue
        yield child


def _deploy_extras(config: AgentConfig, root: Path, workdir: str) -> None:
    """Mirror dot_claude/<everything-else> into <workdir>/.claude/<rel>."""
    dot_workspace = Path(workdir) / ".claude"
    dot_workspace.mkdir(parents=True, exist_ok=True)
    for child in _iter_extras(root):
        dst = dot_workspace / child.name
        if child.is_dir():
            if dst.exists():
                shutil.rmtree(dst)
            shutil.copytree(child, dst)
        else:
            shutil.copy2(child, dst)
    logger.info(
        "Mirrored dot_claude extras for %s into %s",
        config.name,
        dot_workspace,
    )


def _cleanup_extras(config: AgentConfig, root: Path, workdir: str) -> None:
    dot_workspace = Path(workdir) / ".claude"
    if not dot_workspace.is_dir():
        return
    for child in _iter_extras(root):
        candidate = dot_workspace / child.name
        if candidate.is_dir():
            shutil.rmtree(candidate, ignore_errors=True)
        elif candidate.exists():
            try:
                candidate.unlink()
            except (
                OSError
            ):  # stx-allow: fallback (reason: file system operation failure)
                pass


# --- public umbrella -------------------------------------------------------


def deploy_dot_claude(config: AgentConfig, workdir: str) -> None:
    """Materialize ``<spec.dot_claude>/`` into ``<workdir>``.

    No-op when ``resolve_dot_claude_dir`` returns ``None`` (agents
    without a dot_claude/ dir just don't get materialization).
    """
    root = resolve_dot_claude_dir(config)
    if root is None:
        return
    _deploy_claude_md(config, root, workdir)
    _deploy_mcp_json(config, root, workdir)
    _deploy_env(config, root, workdir)
    _deploy_state_md(config, root, workdir)
    _deploy_extras(config, root, workdir)


def cleanup_dot_claude(config: AgentConfig, workdir: str) -> None:
    """Reverse of :func:`deploy_dot_claude` — invoked at agent stop."""
    root = resolve_dot_claude_dir(config)
    _cleanup_claude_md(config, workdir)
    _cleanup_state_md(config, workdir)
    if root is None:
        return
    _cleanup_mcp_json(config, root, workdir)
    _cleanup_env(config, root, workdir)
    _cleanup_extras(config, root, workdir)
