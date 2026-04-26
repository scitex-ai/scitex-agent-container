"""Deploy src_CLAUDE.md, src_mcp.json, and src_env from agent definition directory."""

from __future__ import annotations

import json
import logging
import os
import re
from pathlib import Path

from ..config import AgentConfig
from .claude_md import build_skills_lines

logger = logging.getLogger(__name__)

# Preserve protocol for workspace CLAUDE.md:
#   <!-- Start of scitex-agent-container generated section (<ts>) -->
#   ... managed by scitex-agent-container, regenerated on every deploy ...
#   <!-- End of scitex-agent-container generated section -->
#   ... user-editable tail, preserved across restarts ...
#
# deploy_src_claude_md re-reads any tail past END_MARKER from the existing
# workspace CLAUDE.md and re-appends it after writing the freshly generated
# section. .mcp.json is NOT subject to this protocol — it is a full overwrite
# because MCP server config is structural, not user-edited.
END_MARKER = "<!-- End of scitex-agent-container generated section -->"
START_MARKER_PREFIX = "<!-- Start of scitex-agent-container generated section"


class WorkspaceCLAUDEMarkerError(RuntimeError):
    """Raised when an existing workspace CLAUDE.md has malformed markers.

    The deploy is hard-aborted on this error rather than silently overwriting
    or guessing — preserving user content past the End marker is a safety
    contract and any ambiguity in marker placement could destroy work.
    """


def _validate_marker_invariants(text: str, source_name: str) -> None:
    """Hard-fail if Start/End markers are missing or malformed.

    Per ywatanabe spec (msg 5250-5260, 2026-04-12):
    - Exactly one Start marker (matched by START_MARKER_PREFIX) must appear.
    - Exactly one End marker (END_MARKER) must appear.
    - The Start marker must come before the End marker in byte order.

    Any violation raises WorkspaceCLAUDEMarkerError. Callers should NOT
    try to recover — abort the deploy and require a human to fix the file.
    """
    start_count = text.count(START_MARKER_PREFIX)
    end_count = text.count(END_MARKER)
    if start_count != 1 or end_count != 1:
        raise WorkspaceCLAUDEMarkerError(
            f"{source_name}: expected exactly 1 Start marker and 1 End "
            f"marker, found Start={start_count} End={end_count}. "
            "Refusing to deploy to avoid data loss. Restore the markers "
            "manually before retrying."
        )
    start_idx = text.find(START_MARKER_PREFIX)
    end_idx = text.find(END_MARKER)
    if start_idx > end_idx:
        raise WorkspaceCLAUDEMarkerError(
            f"{source_name}: Start marker appears AFTER End marker. "
            "This indicates a corrupted file. Refusing to deploy."
        )


def _extract_user_tail(workspace_path: Path, end_marker: str = END_MARKER) -> str:
    """Return the substring of workspace_path after the last end_marker.

    Returns empty string if the file does not exist or the marker is absent.
    The returned string preserves whatever whitespace/content followed the
    marker verbatim (leading newline included).
    """
    if not workspace_path.exists():
        return ""
    try:
        existing = workspace_path.read_text()
    except OSError:  # stx-allow: fallback (reason: file system operation failure)
        return ""
    idx = existing.rfind(end_marker)
    if idx == -1:
        return ""
    return existing[idx + len(end_marker) :]


def _definition_dir(config: AgentConfig) -> Path | None:
    """Return the directory containing the agent YAML, or None."""
    if not config.config_path:
        return None
    return Path(config.config_path).parent


def _interpolate_env(text: str) -> str:
    """Resolve ${VAR} references from os.environ."""
    return re.sub(
        r"\$\{(\w+)\}",
        lambda m: os.environ.get(m.group(1), m.group(0)),
        text,
    )


def _interpolate_metadata(text: str, config: AgentConfig) -> str:
    """Resolve ${metadata.name} and ${metadata.labels.*} references."""

    def _replace(m: re.Match) -> str:
        key = m.group(1)
        if key == "metadata.name":
            return config.name
        if key.startswith("metadata.labels."):
            label = key[len("metadata.labels.") :]
            return config.labels.get(label) or m.group(0)
        return m.group(0)

    return re.sub(r"\$\{([^}]+)\}", _replace, text)


def deploy_src_claude_md(config: AgentConfig, workdir: str) -> None:
    """Write src_CLAUDE.md into {workdir}/CLAUDE.md, preserving the user tail.

    Protocol: everything up to and including END_MARKER is regenerated from
    the agent template on every deploy. Anything in the existing workspace
    CLAUDE.md that appears *after* END_MARKER is preserved verbatim and
    re-appended. This is how per-host notes, custom skills, and agent
    scratch content survive container restarts.

    If the existing workspace file has no END_MARKER (legacy/contaminated),
    the entire existing content is discarded and the source is written as-is.
    If the freshly generated section itself has no END_MARKER (template not
    yet updated to the new convention), a warning is logged and the source
    is written with no tail preservation.
    """
    defdir = _definition_dir(config)
    if defdir is None:
        return

    src = defdir / "src_CLAUDE.md"
    if not src.exists():
        return

    section_content = src.read_text().strip()
    if not section_content:
        return

    # Interpolate metadata references
    section_content = _interpolate_metadata(section_content, config)

    dest = Path(workdir) / "CLAUDE.md"

    from datetime import datetime

    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    start_tag = (
        f"<!-- Start of scitex-agent-container generated section ({timestamp}) -->"
    )

    # Strip any pre-existing scitex-agent-container tags from the source body
    # so we can re-wrap with a fresh timestamped start tag + canonical end tag.
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
    # Append skills section (Required + Available, per spec.skills) inside
    # the managed block, after the user's src_CLAUDE content but before the
    # End marker. In at-import mode, this materializes as ``@<absolute path>``
    # lines that Claude Code follows at session start.
    skills_lines = build_skills_lines(config)
    skills_block = ("\n" + "\n".join(skills_lines)).rstrip() if skills_lines else ""
    new_content = (
        f"{start_tag}\n{section_body}\n{skills_block}\n{END_MARKER}\n{guide_comment}\n"
        if skills_block
        else f"{start_tag}\n{section_body}\n{END_MARKER}\n{guide_comment}\n"
    )

    # Validate: if a workspace CLAUDE.md already exists, its markers must
    # be well-formed (exactly 1 Start, 1 End, Start-before-End). Refuse the
    # deploy on any violation rather than silently overwriting (data-loss
    # safety contract per ywatanabe spec msg 5250-5260).
    existing_text = dest.read_text() if dest.exists() else ""
    if existing_text.strip():
        _validate_marker_invariants(existing_text, str(dest))

    # Preserve anything the agent/user wrote past END_MARKER in the existing file.
    user_tail = _extract_user_tail(dest, END_MARKER)
    # Strip any previous guide comment so it does not duplicate on re-deploy.
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
            "src_CLAUDE.md for %s has no End marker after wrapping; "
            "writing source only, user tail not preserved",
            config.name,
        )
        updated = new_content
    elif user_tail:
        # user_tail already includes any leading newline that followed the marker
        updated = new_content.rstrip("\n") + user_tail
        if not updated.endswith("\n"):
            updated += "\n"
    else:
        updated = new_content

    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(updated)
    logger.info(
        "Deployed src_CLAUDE.md for %s to %s (preserved user tail: %s)",
        config.name,
        dest,
        "yes" if user_tail else "no",
    )


def cleanup_src_claude_md(config: AgentConfig, workdir: str) -> None:
    """Remove the agent-container section from {workdir}/CLAUDE.md.

    Strips the Start...End managed block AND the guide comment that
    ``deploy_src_claude_md`` appends directly after the End marker. The
    guide comment pattern matches the current ``====`` framed
    "CUSTOM CONTENT — edit freely below this line" block as well as any
    legacy ``↓ Your custom content`` variant, because both forms are in
    the wild on already-deployed workspaces.

    If after stripping the remaining file is whitespace-only, the file
    is removed so the next ``deploy_src_claude_md`` sees "no existing
    file" rather than a non-empty file with zero markers (which the
    validator would hard-reject via ``WorkspaceCLAUDEMarkerError``,
    breaking every ``stop`` → ``start`` restart cycle).
    """
    dest = Path(workdir) / "CLAUDE.md"
    if not dest.exists():
        return

    existing = dest.read_text()

    # 1. Strip the managed block (Start marker → End marker).
    managed_block = (
        r"\n*<!-- Start of scitex-agent-container generated section.*?-->.*?"
        r"<!-- End of scitex-agent-container generated section -->\n?"
    )
    # 2. Strip the guide comment that deploy_src_claude_md emits right
    #    after the End marker. Matches both the current ``====`` form and
    #    the legacy ``↓ Your custom content`` form.
    guide_comment_current = (
        r"<!--\s*=+\s*\n"
        r".*?CUSTOM CONTENT.*?edit freely.*?"
        r"=+\s*-->\n?"
    )
    guide_comment_legacy = r"<!--\s*↓\s*Your custom content.*?-->\n?"

    pattern = f"{managed_block}(?:{guide_comment_current}|{guide_comment_legacy})?"
    updated = re.sub(pattern, "", existing, flags=re.DOTALL)

    if not updated.strip():
        dest.unlink()
        logger.info(
            "Cleaned up CLAUDE.md for %s at %s (file removed: only "
            "managed section + guide comment present)",
            config.name,
            dest,
        )
        return

    if updated != existing:
        dest.write_text(updated)
        logger.info("Cleaned up CLAUDE.md for %s at %s", config.name, dest)


def deploy_src_mcp_json(config: AgentConfig, workdir: str) -> None:
    """Copy ``src_mcp.json`` to ``{workdir}/.mcp.json`` on EVERY invocation.

    Contract (see todo#453):

    * **Unconditional refresh** — the workspace ``.mcp.json`` is always
      rewritten from the canonical ``src_mcp.json`` when this function
      is called. There is NO ``if dest.exists()`` fast-path. This is the
      invariant that makes canonical config edits (e.g. adding a channel
      to ``SCITEX_OROCHI_CHANNELS``) propagate on the next agent start
      rather than lingering as stale workspace state for hours.
    * **Per-server replace**, not deep-merge — every server entry
      declared in ``src_mcp.json`` fully overwrites the same-named entry
      in the workspace copy. This means env keys removed from the
      canonical source are also removed from the workspace.
    * **Other servers preserved** — servers present in the workspace
      ``.mcp.json`` but NOT declared by this agent's ``src_mcp.json``
      are left untouched (e.g. user-added local tools).
    * **Idempotent** — calling this repeatedly with an unchanged source
      produces byte-identical output.

    Interpolation:

    * ``${metadata.name}`` / ``${metadata.labels.*}`` → resolved from
      the AgentConfig.
    * ``${ENV_VAR}`` → resolved from ``os.environ`` at write time.
    * ``~`` prefix in ``args`` entries → expanded to ``$HOME``.

    If ``src_mcp.json`` does not exist next to the agent YAML, does
    nothing (legacy-v1 / no-MCP agents).
    """
    defdir = _definition_dir(config)
    if defdir is None:
        return

    src = defdir / "src_mcp.json"
    if not src.exists():
        return

    text = src.read_text().strip()
    if not text:
        return

    # Interpolate metadata, then env vars
    text = _interpolate_metadata(text, config)
    text = _interpolate_env(text)

    # Validate JSON
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:  # stx-allow: fallback (reason: malformed JSON tolerated)
        logger.warning("Invalid JSON in %s: %s", src, exc)
        return

    dest = Path(workdir) / ".mcp.json"

    # Preserve any OTHER servers the workspace has that we don't declare.
    # Our own servers are always replaced wholesale below.
    existing: dict = {}
    if dest.exists():
        try:
            existing = json.loads(dest.read_text())
        except (json.JSONDecodeError, OSError):  # stx-allow: fallback (reason: malformed JSON tolerated)
            pass
    if not isinstance(existing, dict):
        existing = {}

    # Expand ~ in args for each server
    for server in data.get("mcpServers", {}).values():
        if "args" in server and isinstance(server["args"], list):
            server["args"] = [
                str(Path(a).expanduser()) if a.startswith("~") else a
                for a in server["args"]
            ]

    # Per-server replace (NOT deep merge): the src entry wholly overrides
    # any workspace entry with the same key. This guarantees that env
    # keys removed from src_mcp.json are also removed from the workspace
    # copy — critical for e.g. retiring a ``SCITEX_OROCHI_CHANNELS``
    # subscription entry cleanly.
    src_servers = data.get("mcpServers", {})
    existing.setdefault("mcpServers", {}).update(src_servers)

    # Drift diagnostics: log when the workspace was older than src —
    # the common case of "PR merged N hours ago, agent still stale"
    # now leaves a breadcrumb in the agent-container log instead of
    # being a silent no-op.
    try:
        if dest.exists():
            src_mtime = src.stat().st_mtime
            dest_mtime = dest.stat().st_mtime
            if src_mtime > dest_mtime:
                drift_minutes = (src_mtime - dest_mtime) / 60
                logger.info(
                    "src_mcp.json for %s is newer than workspace copy by "
                    "%.1f min — refreshing %s",
                    config.name,
                    drift_minutes,
                    dest,
                )
    except OSError:  # stx-allow: fallback (reason: file system operation failure)
        pass

    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(json.dumps(existing, indent=2) + "\n")
    logger.info(
        "Deployed src_mcp.json for %s to %s (servers: %s)",
        config.name,
        dest,
        ", ".join(src_servers.keys()),
    )


def cleanup_src_mcp_json(config: AgentConfig, workdir: str) -> None:
    """Remove servers defined in src_mcp.json from {workdir}/.mcp.json."""
    defdir = _definition_dir(config)
    if defdir is None:
        return

    src = defdir / "src_mcp.json"
    if not src.exists():
        return

    try:
        src_data = json.loads(src.read_text())
    except (json.JSONDecodeError, OSError):  # stx-allow: fallback (reason: malformed JSON tolerated)
        return

    keys_to_remove = list(src_data.get("mcpServers", {}).keys())
    if not keys_to_remove:
        return

    dest = Path(workdir) / ".mcp.json"
    if not dest.exists():
        return

    try:
        data = json.loads(dest.read_text())
    except (json.JSONDecodeError, OSError):  # stx-allow: fallback (reason: malformed JSON tolerated)
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


def deploy_src_env(config: AgentConfig, workdir: str) -> None:
    """Copy ``src_env`` to ``{workdir}/.env`` with mode 0600.

    Symmetric to :func:`deploy_src_mcp_json`. Source format is plain
    dotenv (``KEY=value`` per line, ``#`` comments, blank lines allowed).
    The whole file is interpolated for ``${VAR}`` (from ``os.environ``)
    and ``${metadata.name}`` / ``${metadata.labels.*}`` (from
    AgentConfig) before being written.

    Contract:

    * **Unconditional refresh** — ``.env`` is always rewritten from the
      canonical source. No ``if dest.exists()`` fast-path.
    * **Full overwrite** — unlike ``.mcp.json`` (per-server replace),
      ``.env`` is wholly replaced. Anything the agent or user added to
      the workspace ``.env`` is lost on next deploy. The source of truth
      is ``src_env``.
    * **Mode 0600** — readable only by the agent's UID, since dotenvs
      typically carry secrets.
    * **No shell-eval** — ``$(...)`` and backticks are NOT expanded.
      Only ``${VAR}`` substitution from already-exported env vars is
      supported. To inject a token, the parent shell must export the
      value before sac launches.

    If ``src_env`` does not exist next to the agent YAML, does nothing.
    """
    defdir = _definition_dir(config)
    if defdir is None:
        return

    src = defdir / "src_env"
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
    except OSError as exc:  # stx-allow: fallback (reason: file system operation failure)
        logger.warning("Failed to chmod 0600 on %s: %s", dest, exc)

    logger.info("Deployed src_env for %s to %s", config.name, dest)


def cleanup_src_env(config: AgentConfig, workdir: str) -> None:
    """Remove ``{workdir}/.env`` if it was deployed by us.

    The workspace ``.env`` is fully owned by ``deploy_src_env`` (no
    user-tail protocol like CLAUDE.md, no per-server preservation like
    .mcp.json). On stop, simply unlink it if a ``src_env`` source
    exists. Other projects' workspace .env files (where no ``src_env``
    sits next to the YAML) are left untouched.
    """
    defdir = _definition_dir(config)
    if defdir is None:
        return

    src = defdir / "src_env"
    if not src.exists():
        return

    dest = Path(workdir) / ".env"
    if dest.exists():
        dest.unlink()
        logger.info("Cleaned up .env for %s at %s", config.name, dest)
