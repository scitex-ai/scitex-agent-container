#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# File: src/scitex_agent_container/_jobs/_constitution_refresh.py

"""Push the current constitution into every agent overlay — merged is LIVE.

THE PROBLEM THIS EXISTS FOR (measured 2026-08-15, cct + sac)
============================================================
The copy a running agent reads is a snapshot written once, at agent start.
Nothing refreshes it. A rule merged to develop reaches an agent only when
that agent restarts — so on 2026-08-15 the operator set the THREE DAYS RULE
and it reached 2 of 21 agents. Three versions were in circulation, the
oldest nine days old and missing two policy REVERSALS, so those agents
would have enforced a withdrawn ban.

THE PATH THAT WINS, AND THE TRAP
================================
AUTHORITATIVE::

    <overlays>/<agent>/upper/home/agent/.claude/commands/constitution.md

NOT::

    runtime/<agent>/home/.claude/commands/constitution.md

Both exist and both look plausible. sac once updated the second one,
measured "21 updated, 0 stale", and had verified a file no agent reads. The
only vantage that can answer "what does a running agent read" is INSIDE the
container — which is why the self-check here reads every copy back through
the overlay path after writing it, and counts the overlay as refreshed only
when the read-back digest matches the source. "The copy returned 0" and
"the file now matches" are different claims; only the second one counts.

WHY NOT A RESTART WAVE
======================
A host-side write into the overlay upper is visible to the LIVE container
immediately — verified by writing and then reading it back from inside a
running session. So no restart is needed, and the stale-in-memory-token
hazard that killed 33 agents on 2026-07-16 is never paid.

That measurement was of an IN-PLACE write (``cp -f``), and the port keeps
it: the file is truncated and rewritten under its existing inode, not
swapped in by ``os.replace``. A rename gives the path a new inode, and the
live container's overlayfs dentry cache is exactly the layer that was never
measured to follow one.

WHY THIS IS SOURCE AND NOT A SCRIPT
===================================
This module is the port of ``~/.local/bin/sac-constitution-refresh.sh``, a
hand-written host script scheduled by a hand-written unit pair
(``sac-constitution-refresh.service`` / ``.timer``) on compute-04. Operator
ruling 2026-09-02: hand-written scripts must live on the source side. As a
declared JobSpec (:mod:`._specs_constitution`) it is version-controlled,
tested, and installed by the same ``ecosystem up`` that installs every
other sac timer; the migration table (:mod:`._migrate._renames`) retires
the hand-written unit when the declared one is armed.

EXIT CODES (declared, not overloaded)
=====================================
* 0 — every overlay current (or, with ``--dry-run``, nothing was written)
* 1 — at least one overlay could not be refreshed (a real failure)
* 2 — the SOURCE is missing, unreadable, or smaller than
  :data:`MIN_SOURCE_BYTES` (cannot even start). A suspiciously small source
  is more likely a truncated write than a real edit; refusing beats fanning
  a broken file out to every agent.

An overlays root that does not exist iterates as ZERO overlays and exits 0,
exactly as the script's ``for ov in "$OVERLAYS"/*/`` did. Deliberate, for a
fleet-wide timer: a JobSpec has no host axis, so this job is a candidate on
every host, and a host that runs no agents must not record the unit
``failed`` — that puts the host ``degraded``, and ``host-sync-check``'s
``--exit-zero`` records what a degraded host costs. The rendered report
says ``no overlays found`` so the empty pass is visible rather than silent.
"""

from __future__ import annotations

import argparse
import hashlib
import os
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

import click

#: Env override for the constitution to distribute.
SOURCE_ENV = "SAC_CONSTITUTION_SRC"
#: Env override for the directory holding one overlay per agent.
OVERLAY_ROOT_ENV = "SAC_OVERLAY_ROOT"
#: A source below this many bytes is refused as a probable truncation.
MIN_SOURCE_BYTES = 10_000
#: The agent's home inside one overlay. An overlay without it is not an
#: agent overlay (a stray directory, a half-built one) and is SKIPPED.
AGENT_HOME = Path("upper") / "home" / "agent"
#: Where the running agent reads its commands, relative to its home.
COMMANDS_DIR = Path(".claude") / "commands"
#: The file name on both sides.
CONSTITUTION_NAME = "constitution.md"

EXIT_CURRENT = 0
EXIT_FAILED = 1
EXIT_SOURCE = 2


def default_source(env: Mapping[str, str] | None = None) -> Path:
    """The constitution to distribute: ``$SAC_CONSTITUTION_SRC`` or the
    HOST user's ``~/.claude/commands/constitution.md``.

    ``~`` is the user running the timer, derived, never spelled — the
    script hardcoded one login and would have distributed nothing on any
    other host.
    """
    env = os.environ if env is None else env
    raw = env.get(SOURCE_ENV)
    if raw:
        return Path(raw).expanduser()
    return Path.home() / ".claude" / "commands" / CONSTITUTION_NAME


def default_overlays_root(env: Mapping[str, str] | None = None) -> Path:
    """The overlays root: ``$SAC_OVERLAY_ROOT`` or sac's own containers dir."""
    env = os.environ if env is None else env
    raw = env.get(OVERLAY_ROOT_ENV)
    if raw:
        return Path(raw).expanduser()
    return Path.home() / ".scitex" / "agent-container" / "containers" / "overlays"


def target_in(overlay: Path) -> Path:
    """The one path a running agent reads, inside ``overlay``."""
    return overlay / AGENT_HOME / COMMANDS_DIR / CONSTITUTION_NAME


@dataclass(frozen=True)
class RefreshResult:
    """One pass, counted by CONTENT.

    ``updated`` and ``new`` count overlays whose copy was written AND read
    back with the source's digest; a write that succeeded but reads back
    wrong is ``failed``. ``ok`` is already-current, ``skipped`` is not an
    agent overlay at all. ``refusal`` is set only when the SOURCE was
    unusable and nothing was attempted.
    """

    source: Path
    overlays_root: Path
    dry_run: bool
    source_md5: str
    source_bytes: int
    refusal: str | None = None
    updated: int = 0
    new: int = 0
    ok: int = 0
    failed: int = 0
    skipped: int = 0
    failures: tuple[str, ...] = ()

    @property
    def exit_code(self) -> int:
        """The declared tri-state, see the module docstring."""
        if self.refusal is not None:
            return EXIT_SOURCE
        return EXIT_FAILED if self.failed else EXIT_CURRENT

    @property
    def summary(self) -> str:
        """The one line a cron log is grepped for — the script's, plus ``skipped``."""
        return (
            f"  already current={self.ok}  updated={self.updated}  "
            f"created={self.new}  failed={self.failed}  skipped={self.skipped}"
        )


def _md5(data: bytes) -> str:
    return hashlib.md5(data).hexdigest()


def _md5_of(path: Path) -> str | None:
    """Digest of ``path``, or ``None`` when it cannot be read.

    An unreadable existing copy is NOT current — it gets rewritten, and if
    that fails too the overlay is counted ``failed`` with the reason.
    """
    try:
        return _md5(path.read_bytes())
    except OSError:  # stx-allow: fallback (reason: an unreadable copy is by definition not current; the write path reports the real error if it persists)
        return None


def _overlays(root: Path) -> list[Path]:
    """Every directory directly under ``root``, sorted; none when ``root``
    is absent — the documented zero-overlay pass."""
    try:
        return sorted(p for p in root.iterdir() if p.is_dir())
    except OSError:  # stx-allow: fallback (reason: an absent overlays root is a host with no agents, which must not fail a fleet-wide timer; the report says so)
        return []


def _write_and_verify(target: Path, data: bytes, digest: str) -> str | None:
    """Write ``data`` in place, read it back through the overlay path, and
    return ``None`` on a verified match or the reason it is not one."""
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        return f"cannot create {target.parent}: {exc.strerror or exc}"
    try:
        # In place, under the existing inode, like `cp -f` — see the module
        # docstring for why a tempfile + rename is NOT the safer choice here.
        with open(target, "wb") as handle:
            handle.write(data)
    except OSError as exc:
        return f"cannot write {target}: {exc.strerror or exc}"
    back = _md5_of(target)
    if back is None:
        return f"cannot read back {target}"
    if back != digest:
        return f"read-back md5 {back[:8]} != source {digest[:8]}: {target}"
    return None


def refresh(source: Path, overlays_root: Path, *, dry_run: bool) -> RefreshResult:
    """Distribute ``source`` into every agent overlay under ``overlays_root``.

    Pure over the filesystem it is given: no subprocess, no env, no home.
    ``dry_run`` counts what WOULD change and writes nothing.
    """
    try:
        data = source.read_bytes()
    except OSError as exc:
        return RefreshResult(
            source,
            overlays_root,
            dry_run,
            source_md5="",
            source_bytes=0,
            refusal=f"constitution source unreadable: {source} ({exc.strerror or exc})",
        )
    digest = _md5(data)
    if len(data) < MIN_SOURCE_BYTES:
        return RefreshResult(
            source,
            overlays_root,
            dry_run,
            source_md5=digest,
            source_bytes=len(data),
            refusal=(
                f"source is only {len(data)}B — refusing to distribute a "
                f"probable truncation (floor {MIN_SOURCE_BYTES}B): {source}"
            ),
        )

    counts = {"updated": 0, "new": 0, "ok": 0, "failed": 0, "skipped": 0}
    failures: list[str] = []
    for overlay in _overlays(overlays_root):
        if not (overlay / AGENT_HOME).is_dir():
            counts["skipped"] += 1
            continue
        target = target_in(overlay)
        existed = target.is_file()
        if existed and _md5_of(target) == digest:
            counts["ok"] += 1
            continue
        bucket = "updated" if existed else "new"
        if dry_run:
            counts[bucket] += 1
            continue
        problem = _write_and_verify(target, data, digest)
        if problem is None:
            counts[bucket] += 1
        else:
            counts["failed"] += 1
            failures.append(f"{overlay.name}: {problem}")

    return RefreshResult(
        source,
        overlays_root,
        dry_run,
        source_md5=digest,
        source_bytes=len(data),
        failures=tuple(failures),
        **counts,
    )


def render(result: RefreshResult) -> str:
    """The report: a header naming what was distributed, one line per
    failure, and the summary line."""
    header = (
        f"sac agents refresh-constitution: src={result.source} "
        f"md5={result.source_md5[:8] or '-'} {result.source_bytes}B"
        + ("  (DRY RUN)" if result.dry_run else "")
    )
    if result.refusal is not None:
        return f"{header}\nFATAL: {result.refusal}"
    lines = [header]
    lines.extend(f"  FAILED: {failure}" for failure in result.failures)
    if result.updated + result.new + result.ok + result.failed + result.skipped == 0:
        lines.append(f"  no overlays found under {result.overlays_root}")
    lines.append(result.summary)
    return "\n".join(lines)


def run(
    source: Path,
    overlays_root: Path,
    *,
    dry_run: bool,
    echo: Callable[[str], None] = click.echo,
) -> int:
    """Refresh, print the report, return the exit code. The CLI verb and
    :func:`main` both end here so the two cannot drift."""
    result = refresh(source, overlays_root, dry_run=dry_run)
    echo(render(result))
    return result.exit_code


def main(argv: Sequence[str] | None = None) -> int:
    """Thin entry: ``[--dry-run] [--source PATH] [--overlays-root PATH]``."""
    parser = argparse.ArgumentParser(
        prog="sac agents refresh-constitution",
        description="Push the current constitution into every agent overlay.",
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="report what WOULD change; write nothing"
    )
    parser.add_argument(
        "--source",
        type=Path,
        default=None,
        help=f"constitution to distribute [default: ${SOURCE_ENV} or ~/.claude/commands/constitution.md]",
    )
    parser.add_argument(
        "--overlays-root",
        type=Path,
        default=None,
        help=f"one overlay per agent [default: ${OVERLAY_ROOT_ENV} or ~/.scitex/agent-container/containers/overlays]",
    )
    ns = parser.parse_args(None if argv is None else list(argv))
    return run(
        ns.source or default_source(),
        ns.overlays_root or default_overlays_root(),
        dry_run=ns.dry_run,
    )


__all__ = [
    "AGENT_HOME",
    "COMMANDS_DIR",
    "CONSTITUTION_NAME",
    "EXIT_CURRENT",
    "EXIT_FAILED",
    "EXIT_SOURCE",
    "MIN_SOURCE_BYTES",
    "OVERLAY_ROOT_ENV",
    "SOURCE_ENV",
    "RefreshResult",
    "default_overlays_root",
    "default_source",
    "main",
    "refresh",
    "render",
    "run",
    "target_in",
]

# EOF
