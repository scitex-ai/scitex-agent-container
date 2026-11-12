"""``sac agents archive-claude-bloat`` — audit-driven F-CS8 remediation.

Closes the banner-to-action loop for the F-CS8 (workdir-claude bloat →
silent SDK MCP-spawn failure) class of incidents.

Background. ``runtimes/claude_session._warn_if_heavy_workdir_claude``
already runs :func:`scitex_agent_container._workdir.audit_workdir_claude`
on every start and surfaces a LOUD banner listing ``bloat_sources``
(per-subdir entries above the per-bucket file-count threshold) plus a
copy-paste-able ``mv ...`` recipe. The operator gap is the copy-paste:
they have to read the banner, eyeball the rel_path, and run the move
by hand. In production that loop has stalled the recovery of multiple
fleet members (one agent workdir at 41,873 files / 884 MB on
2026-06-03, see ``runtimes/claude_session.py:163-169``).

This command closes that loop. For each ``bloat_sources`` entry the
audit returns, the sub-directory is MOVED (NEVER ``rm``ed) to
``<workdir>/.claude/.archived-<utc>/<original-relpath>/``. A single-
line summary is printed per move::

    archived: <from> -> <to> (N files, M MB)

Successful moves retain their trees at the recorded archive path. Review
that retained data before manual restoration; the mover does not provide
a rollback transaction.

This command is intentionally narrower than the existing
``prune-claude`` (which carries dry-run logic for ``.pending/`` records
and git-aware worktree merge checks). ``archive-claude-bloat`` is the
broader archive operation. It requires explicit approval; argument-only
preview does not resolve the agent or inspect its workdir.
"""

from __future__ import annotations

import json
import shutil
import time
from pathlib import Path

import click

from .._workdir import audit_workdir_claude
from ._helpers import _json_flag

# ---------------------------------------------------------------------------
# Workdir resolution
# ---------------------------------------------------------------------------


def _resolve_agent_workdir(name: str) -> str | None:
    """Look up an agent's workdir via the registry → spec.yaml chain.

    Returns the expanded workdir string, or ``None`` if the agent is
    unknown, the spec is missing, or the spec is malformed. The CLI
    surfaces a clear error rather than tracebacks; this helper stays
    boolean so the command body can choose the exit code.
    """
    # stx-allow: fallback (reason: the agent registry may be empty in
    # CI / fresh installs; the CLI surfaces a clear error instead of
    # tracebacks)
    try:
        from .._state.registry import Registry
        from ..config import load_config
    except ImportError:  # stx-allow: fallback (reason: see inline comment)
        return None
    reg = Registry()
    entry = reg.get(name)
    if not entry:
        return None
    cfg_path = entry.get("config")
    if not cfg_path:
        return None
    # stx-allow: fallback (reason: malformed/missing spec.yaml; CLI
    # reports the error rather than abort)
    try:
        cfg = load_config(cfg_path)
    except Exception:  # stx-allow: fallback (reason: see inline comment)
        return None
    return getattr(cfg, "expanded_workdir", None) or getattr(cfg, "workdir", None)


# ---------------------------------------------------------------------------
# Archive primitive
# ---------------------------------------------------------------------------


def _archive_root(workdir: Path) -> Path:
    """Return ``<workdir>/.claude/.archived-<utc>/`` for a SINGLE invocation.

    The timestamp is computed once per call so every move in this run
    lands under the same archive bucket. UTC + ISO-8601-basic so the
    name sorts lexicographically and is filesystem-safe everywhere.
    """
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    return workdir / ".claude" / f".archived-{stamp}"


def archive_bloat_sources(workdir: str | Path) -> list[dict]:
    """Move every audit-detected bloat-source subdir to the archive bucket.

    Returns a list of move records, one per source actually moved.
    Each record is a dict with keys ``from``, ``to``, ``files``,
    ``bytes`` so the caller can render any format it likes. Pure I/O
    surface — the caller decides how to surface success / failure.

    Move semantics: :func:`shutil.move` is used so cross-device moves
    work via copy+remove. Source directories that disappeared between
    audit and apply (race window) are silently skipped — the audit
    result is a snapshot, not a lock.
    """
    wd = Path(workdir)
    audit = audit_workdir_claude(wd)
    if not audit.bloat_sources:
        return []
    archive_root = _archive_root(wd)
    archive_root.mkdir(parents=True, exist_ok=True)
    moved: list[dict] = []
    for source in audit.bloat_sources:
        src = wd / ".claude" / source.rel_path
        if not src.exists():
            # stx-allow: fallback (reason: audit is a snapshot; a
            # racing prune or operator-side mv between audit and
            # apply means the source is already gone — record
            # nothing and move on)
            continue
        dest = archive_root / source.rel_path
        dest.parent.mkdir(parents=True, exist_ok=True)
        # stx-allow: fallback (reason: shutil.move can raise on
        # cross-filesystem permission edges; we record the failure
        # by skipping the entry rather than aborting the whole
        # archive run — operator still gets the partial result)
        try:
            shutil.move(str(src), str(dest))
        except (OSError, shutil.Error):  # stx-allow: fallback (reason: see inline)
            continue
        moved.append(
            {
                "from": str(src),
                "to": str(dest),
                "files": source.files,
                "bytes": source.bytes,
            }
        )
    return moved


# ---------------------------------------------------------------------------
# CLI command
# ---------------------------------------------------------------------------


def _format_move(record: dict) -> str:
    """Render one move as the single-line summary the banner promises.

    Format is locked: ``archived: <from> -> <to> (N files, M MB)``.
    Bytes are reported as MB with one decimal to mirror the banner's
    own units; the audit's per-subdir count + bytes are stable inputs.
    """
    mb = record["bytes"] / (1024 * 1024)
    return (
        f"archived: {record['from']} -> {record['to']} "
        f"({record['files']:,} files, {mb:.1f} MB)"
    )


@click.command(name="archive-claude-bloat")
@click.argument("name", required=True)
@click.option(
    "--dry-run",
    is_flag=True,
    help="Describe arguments without agent lookup, scanning or moving files.",
)
@click.option(
    "--yes",
    "-y",
    is_flag=True,
    help="Approve audit-driven moves into the archive bucket.",
)
@click.option("--json", "as_json", is_flag=True, help="Emit recorded moves as JSON.")
@click.pass_context
def archive_claude_bloat(
    ctx: click.Context, name: str, dry_run: bool, yes: bool, as_json: bool
) -> None:
    """Archive F-CS8 bloat sources from an agent's ``<workdir>/.claude/``.

    Requires --yes/-y. --dry-run reports arguments only, without registry
    lookup or scanning; it does not list sources or validate the agent.
    Approval conflicts with --dry-run.

    Resolves the agent's workdir, runs the same ``audit_workdir_claude``
    that the start-time banner uses, and MOVES every ``bloat_sources``
    entry to ``<workdir>/.claude/.archived-<UTC>/<rel_path>/``. Move-
    don't-delete: nothing is removed; the operator can reverse any move
    with one ``mv`` if they decide the audit was over-eager.

    Prints one summary line per move::

        archived: <from> -> <to> (N files, M MB)

    The legacy mover records only successful moves and skips vanished or
    failing sources. An empty result does not prove that the tree is clean.

    Exits 0 when the run completed (with or without moves), 2 when the
    agent name is unknown / unregistrable.

    \b
    Examples:
      $ sac agents archive-claude-bloat proj-heavy-agent --dry-run
      $ sac agents archive-claude-bloat proj-grant --yes
    """
    if yes and dry_run:
        raise click.UsageError("--dry-run cannot be combined with --yes/-y")
    if dry_run:
        click.echo(
            json.dumps(
                {
                    "action": "archive-claude-bloat",
                    "agent": name,
                    "dry_run": True,
                    "scope": "arguments_only",
                    "verified": False,
                },
                indent=2,
            )
        )
        return
    if not yes:
        raise click.UsageError(
            "Archiving requires --yes/-y; use --dry-run to preview arguments."
        )
    workdir = _resolve_agent_workdir(name)
    if not workdir:
        click.echo(f"unknown agent or missing workdir: {name}", err=True)
        raise SystemExit(2)
    moved = archive_bloat_sources(workdir)
    if _json_flag(ctx, as_json):
        click.echo(
            json.dumps(
                {"agent": name, "workdir": str(workdir), "moved": moved}, indent=2
            )
        )
        return
    if not moved:
        click.echo(
            "nothing to archive: no moves recorded (no bloat or sources skipped during apply)."
        )
        return
    for record in moved:
        click.echo(_format_move(record))


__all__ = [
    "archive_bloat_sources",
    "archive_claude_bloat",
]
