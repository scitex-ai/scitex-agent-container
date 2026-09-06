"""Resolved-engine observation for ``sac agents health``.

Published NEXT TO ``healthy`` (never folded into the exit code), same as
:mod:`._health_liveness` and :mod:`._health_overlay_masking`.

WHY THIS EXISTS. Nothing reported which backend an agent actually runs
on. ``check`` validates apptainer, binds and host and prints "Ready to
deploy"; ``status`` reports ports, session and inbox. Neither says
"engine". On 2026-09-05 ``business`` was restarted with an explicit
``--engine claude`` while its own spec declares ``qwen38-27b`` with
``default: true`` and carries an operator ruling saying Qwen ONLY. It
ran 27 hours that way and the only way to establish it was reading
``/proc/<pid>/environ`` by hand.

TWO RULES THIS MODULE IS BUILT AROUND.

1. ``declared`` HAS EXACTLY ONE SOURCE: ``AgentConfig.engine_key``,
   which the loader writes from the same ``resolve_default_for_spec``
   the START path uses. It is NEVER re-derived from ``config.engines``
   -- that mapping is the MERGED namespace (fleet library UNION
   spec-local), and handing it to ``resolve_default`` without
   ``local_keys`` tells the resolver every FLEET engine was written by
   THIS spec. Measured 2026-09-06: with a one-entry library, a spec
   declaring no engines at all reported ``declared`` = that library's
   lone key and ``verdict=mismatch``. 129 of 130 deployed specs declare
   no engine, so that path manufactures 129 false alarms; with a
   two-entry library it raises instead and degrades every one of them
   to UNKNOWN blaming the process environment.

2. ABSENCE IS ONLY CLAIMABLE FROM A VANTAGE THAT COULD HAVE SEEN IT.
   The reader walks ``/proc``, and a process whose ``environ`` we may
   not read, or a whole namespace we are not in, is NOT evidence that
   nothing is running. Measured the same day: from inside a container
   (18 pids visible) the reader returned a DEFINITE
   "no-running-process-carries-an-engine" for ``business`` while 13 of
   its processes were running on the host; and on that host as uid 1000,
   235 of 283 environs were unreadable. So the census travels with every
   answer -- scanned, matched, unreadable, vantage -- and a bare
   "nothing found" is reported as *cannot tell*, never as *not there*.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Mapping

__all__ = ["EngineScan", "engine_payload", "print_engine"]

VERDICT_MATCH = "match"
VERDICT_MISMATCH = "mismatch"
VERDICT_UNKNOWN = "unknown"

REASON_NO_RUNNING_PROCESS = "no-running-process-carries-an-engine"
REASON_ENVIRONS_UNREADABLE = "process-environments-not-readable-by-this-user"
REASON_BLIND_VANTAGE = "this-vantage-cannot-see-the-agent-processes"
REASON_ENGINES_DISAGREE = "live-processes-disagree-on-the-engine"
REASON_SPEC_DECLARES_NO_ENGINE = "spec-declares-no-engine"
REASON_READ_ERROR = "process-scan-failed"

AGENT_ID_ENV = "CLAUDE_AGENT_ID"

VANTAGE_HOST = "host"
VANTAGE_CONTAINER = "container"

_VERDICT_COLOUR = {
    VERDICT_MATCH: "green",
    VERDICT_MISMATCH: "red",
    VERDICT_UNKNOWN: "yellow",
}


@dataclass(frozen=True)
class EngineScan:
    """What the /proc walk actually saw. Travels with EVERY answer.

    Published even on success: consulting the unreadable count only when
    nothing was found makes a partial scan invisible the moment one
    process happens to be readable, and the disagreement check below
    then compares a subset while looking like it compared everything.
    """

    pids_scanned: int = 0
    pids_matched: int = 0
    pids_unreadable: int = 0
    vantage: str = VANTAGE_HOST

    @property
    def could_have_seen_everything(self) -> bool:
        """Absence is only meaningful when this is True."""
        return self.pids_unreadable == 0 and self.vantage != VANTAGE_CONTAINER

    def to_dict(self) -> dict:
        return {
            "pids_scanned": self.pids_scanned,
            "pids_matched": self.pids_matched,
            "pids_unreadable": self.pids_unreadable,
            "vantage": self.vantage,
            "complete": self.could_have_seen_everything,
        }


def _vantage() -> str:
    """``container`` when this process runs inside an apptainer SIF.

    Uses sac's existing marker set rather than a private heuristic.
    """
    import os

    from .._maintenance._scratch_migrate_liveness import CONTAINER_MARKER_ENV

    for marker in CONTAINER_MARKER_ENV:
        if os.environ.get(marker):
            return VANTAGE_CONTAINER
    return VANTAGE_HOST


def _read_running_engine(
    name: str, *, proc_root: Any = None
) -> tuple[str | None, EngineScan, str | None]:
    """``(engine, scan, reason)`` for ``name``'s RUNNING processes.

    ``engine`` is ``None`` whenever we could not read exactly one, and
    ``reason`` then says which way it failed. Never guesses, and never
    reports absence from a vantage that could not have seen presence.
    """
    from pathlib import Path

    from ..runtimes._apptainer_provider import ENGINE_KEY_ENV

    fake_proc = proc_root is not None
    root = Path(proc_root) if fake_proc else Path("/proc")
    vantage = VANTAGE_HOST if fake_proc else _vantage()

    agent_entry = f"{AGENT_ID_ENV}={name}"
    engine_prefix = f"{ENGINE_KEY_ENV}="

    engines: set[str] = set()
    scanned = matched = unreadable = 0
    try:
        entries = sorted(root.iterdir())
    except OSError as exc:
        return (
            None,
            EngineScan(vantage=vantage),
            f"{REASON_READ_ERROR} ({type(exc).__name__}: {exc})",
        )

    for entry in entries:
        if not entry.name.isdigit():
            continue
        scanned += 1
        try:
            block = (entry / "environ").read_bytes().decode("utf-8", errors="replace")
        except PermissionError:
            unreadable += 1
            continue
        except OSError:
            # Exited between listing and reading. Not ours, not an error.
            continue
        variables = block.split("\0")
        if agent_entry not in variables:
            continue
        matched += 1
        for variable in variables:
            if variable.startswith(engine_prefix):
                engines.add(variable[len(engine_prefix) :].strip())

    scan = EngineScan(
        pids_scanned=scanned,
        pids_matched=matched,
        pids_unreadable=unreadable,
        vantage=vantage,
    )

    if len(engines) > 1:
        # Two live processes of one agent disagreeing about the backend
        # is a real condition; averaging it away would hide it.
        return None, scan, f"{REASON_ENGINES_DISAGREE}: {sorted(engines)}"
    if engines:
        return engines.pop(), scan, None
    if scan.vantage == VANTAGE_CONTAINER:
        return None, scan, REASON_BLIND_VANTAGE
    if unreadable:
        return None, scan, f"{REASON_ENVIRONS_UNREADABLE}: {unreadable} unreadable"
    return None, scan, REASON_NO_RUNNING_PROCESS


def _declared_engine(config: Any) -> str | None:
    """The engine the PRECEDENCE selects for this spec, or ``None``.

    ONE source, and it is the one the START path uses.
    ``AgentConfig.engine_key`` is written at load time from
    ``_engine_precedence.resolve_default_for_spec``, so reading it makes
    ``declared`` equal BY CONSTRUCTION to the engine a start would pick.
    Empty means the precedence selected NOTHING, which is UNKNOWN.

    A raw spec mapping (tests, callers holding parsed YAML) asks the SAME
    precedence, so an ``engine:`` pin, the legacy block and the fleet
    default are all honoured -- the previous version's private
    ``parse_engines`` + ``resolve_default`` pair silently ignored the pin.
    """
    key = str(getattr(config, "engine_key", "") or "").strip()
    if key:
        return key

    if isinstance(config, Mapping):
        from ..config._engine_library import resolve_engine_namespace
        from ..config._engine_precedence import resolve_default_for_spec

        selected = resolve_default_for_spec(config, resolve_engine_namespace(config))
        return getattr(selected, "key", None)
    return None


def engine_payload(
    name: str,
    config: Any,
    *,
    launch_engine_reader: Callable[[str], tuple[str | None, EngineScan, str | None]]
    | None = None,
) -> dict:
    """JSON-ready engine verdict; any gather failure degrades to UNKNOWN."""
    reader = launch_engine_reader or _read_running_engine
    try:
        running, scan, reason = reader(name)
    except Exception as exc:  # stx-allow: fallback (reason: an unreadable process environment is UNKNOWN with its reason — never a fabricated MATCH, and never a crashed health command)
        running, scan, reason = (
            None,
            EngineScan(),
            f"{REASON_READ_ERROR} ({type(exc).__name__}: {exc})",
        )

    try:
        declared = _declared_engine(config)
    except Exception as exc:  # stx-allow: fallback (reason: same rule — a spec whose engine namespace will not resolve is UNKNOWN, not a match)
        declared = None
        reason = reason or f"{REASON_READ_ERROR} ({type(exc).__name__}: {exc})"

    if running is None:
        verdict = VERDICT_UNKNOWN
    elif declared is None:
        verdict, reason = VERDICT_UNKNOWN, reason or REASON_SPEC_DECLARES_NO_ENGINE
    elif declared == running:
        verdict = VERDICT_MATCH
    else:
        verdict = VERDICT_MISMATCH

    return {
        "agent": name,
        "declared": declared,
        "running": running,
        "verdict": verdict,
        "reason": reason,
        "scan": scan.to_dict(),
    }


def print_engine(console: Any, payload: dict) -> None:
    """One verdict line; on MISMATCH, name both engines and the fix."""
    verdict = str(payload.get("verdict", VERDICT_UNKNOWN))
    colour = _VERDICT_COLOUR.get(verdict, "yellow")
    declared = payload.get("declared") or "?"
    running = payload.get("running") or "?"
    scan = payload.get("scan") or {}
    if verdict == VERDICT_MATCH:
        console.print(f"[{colour}]engine: {running} (matches spec)[/{colour}]")
        return
    if verdict == VERDICT_MISMATCH:
        console.print(
            f"[{colour}]engine: MISMATCH — spec selects {declared!r}, "
            f"the running process was launched on {running!r}[/{colour}]"
        )
        console.print(
            "[dim]  Fix: restart the agent so the declared engine is applied; "
            "a start that silently ran a different backend than the one "
            "declared is worse than a start that did not happen.[/dim]"
        )
        return
    console.print(
        f"[{colour}]engine: unknown — {payload.get('reason') or '?'}[/{colour}]"
    )
    if not scan.get("complete", True):
        console.print(
            f"[dim]  scan was PARTIAL: {scan.get('pids_matched')} matched of "
            f"{scan.get('pids_scanned')} scanned, {scan.get('pids_unreadable')} "
            f"unreadable, vantage={scan.get('vantage')}. Absence here is not "
            f"evidence of absence — run this on the agent's host as its owner.[/dim]"
        )
