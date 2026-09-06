"""Resolved-engine observation for ``sac agents health``.

Published NEXT TO ``healthy`` (never folded into the exit code), same as
:mod:`._health_liveness` and :mod:`._health_overlay_masking`.

WHY THIS EXISTS. Until now nothing reported which backend an agent is
actually running on. ``check`` validates apptainer, binds and host and
prints "Ready to deploy"; ``status`` reports ports, session and inbox.
Neither says "engine". On 2026-09-06 that blind spot let ``business``
run 27 hours on Claude while its own spec carried an operator ruling
saying Qwen ONLY, and the only way to establish that was reading
``/proc/<pid>/environ`` on the host.

THE RUNNING ENGINE IS READ FROM THE RUNNING PROCESS, NOT THE SPEC.
That is the entire point. A status that re-derived the engine from the
spec would agree with the spec by construction and could never report
the disagreement this module exists to surface.

NOT from the tui-env snapshot, which was the obvious-looking source and
is the WRONG one: ``env >`` runs in the outer shell BEFORE
``exec apptainer --env SAC_ENGINE=<key>`` injects the engine, so the
snapshot never contains it. Measured on ``business`` 2026-09-06 — a
23 KB snapshot with zero ``SAC_ENGINE`` lines. The agent's own process
environment is where the value actually lives, and reading the process
is also what makes this an observation of REALITY rather than of
configuration.

Processes are matched on ``CLAUDE_AGENT_ID``, not on a pid from the
registry and not on a process name: one host runs several agents whose
processes are all called ``claude``, and the registry pid is known to be
shared. ``CLAUDE_AGENT_ID`` is carried in the same environment block as
``SAC_ENGINE``, so the match and the reading cannot drift apart.

THREE-VALUED, and ``unknown`` is not ``match``. A missing or unreadable
snapshot means we could not tell, which authorises nothing; folding it
into "match" would reproduce the failure in a new place.
"""

from __future__ import annotations

from typing import Any, Callable, Mapping

__all__ = ["engine_payload", "print_engine"]

VERDICT_MATCH = "match"
VERDICT_MISMATCH = "mismatch"
VERDICT_UNKNOWN = "unknown"

REASON_NO_RUNNING_PROCESS = "no-running-process-carries-an-engine"
REASON_ENGINES_DISAGREE = "live-processes-disagree-on-the-engine"
REASON_ENVIRONS_UNREADABLE = "process-environments-not-readable-by-this-user"
AGENT_ID_ENV = "CLAUDE_AGENT_ID"
REASON_SPEC_DECLARES_NO_ENGINE = "spec-declares-no-engine"
REASON_READ_ERROR = "process-environment-unreadable"

_VERDICT_COLOUR = {
    VERDICT_MATCH: "green",
    VERDICT_MISMATCH: "red",
    VERDICT_UNKNOWN: "yellow",
}


def _read_running_engine(
    name: str, *, proc_root: Any = None
) -> tuple[str | None, str | None, str | None]:
    """``(engine, observed_at, reason)`` from ``name``'s RUNNING process.

    ``engine`` is ``None`` whenever we could not read one, and ``reason``
    then says which of the ways it failed. Never guesses.
    """
    import datetime
    from pathlib import Path

    from ..runtimes._apptainer_provider import ENGINE_KEY_ENV

    root = Path(proc_root) if proc_root is not None else Path("/proc")
    agent_prefix = f"{AGENT_ID_ENV}="
    engine_prefix = f"{ENGINE_KEY_ENV}="

    engines: set[str] = set()
    earliest: float | None = None
    # Counted, never swallowed. A process whose environ we may not read is
    # NOT evidence of absence, and reporting it as "nothing is running" is
    # the same conflation this whole module exists to end — measured
    # 2026-09-06, where a host-side run reported no engine for five agents
    # that were all running, because the caller was not their owner.
    unreadable = 0
    try:
        entries = sorted(root.iterdir())
    except OSError as exc:
        return None, None, f"{REASON_READ_ERROR} ({type(exc).__name__}: {exc})"

    for entry in entries:
        if not entry.name.isdigit():
            continue
        try:
            block = (entry / "environ").read_bytes().decode("utf-8", errors="replace")
        except PermissionError:
            unreadable += 1
            continue
        except OSError:
            # A process that exited between listing and reading is not an
            # error: it simply is not one of ours to report on.
            continue
        variables = block.split("\0")
        if f"{agent_prefix}{name}" not in variables:
            continue
        for variable in variables:
            if variable.startswith(engine_prefix):
                engines.add(variable[len(engine_prefix) :].strip())
        try:
            started = (entry / "environ").stat().st_mtime
        except OSError:
            started = None
        if started is not None and (earliest is None or started < earliest):
            earliest = started

    observed_at = (
        datetime.datetime.fromtimestamp(earliest, tz=datetime.timezone.utc).isoformat()
        if earliest is not None
        else None
    )
    if not engines:
        if unreadable:
            return (
                None,
                observed_at,
                f"{REASON_ENVIRONS_UNREADABLE}: {unreadable} unreadable",
            )
        return None, observed_at, REASON_NO_RUNNING_PROCESS
    if len(engines) > 1:
        # Two live processes of one agent disagreeing about the backend is
        # a real condition, and averaging it away would hide it.
        return (
            None,
            observed_at,
            f"{REASON_ENGINES_DISAGREE}: {sorted(engines)}",
        )
    return engines.pop(), observed_at, None


def _declared_engine(config: Any) -> str | None:
    """The engine key the SPEC selects, or ``None`` if it declares none.

    Reads ``AgentConfig`` the way the rest of the package does — an
    explicit ``engine_key`` pin first, then the default among
    ``config.engines`` (already the fleet library UNION the spec's own
    block by the time load_config is done). The plain-mapping branch
    below keeps a raw spec dict usable by callers and tests.
    """
    from ..config._engine_types import parse_engines, resolve_default

    pinned = str(getattr(config, "engine_key", "") or "").strip()
    if pinned:
        return pinned

    engines = getattr(config, "engines", None)
    if isinstance(engines, Mapping) and engines:
        return getattr(resolve_default(engines), "key", None)

    if isinstance(config, Mapping):
        parsed = parse_engines(config)
        if parsed:
            return getattr(resolve_default(parsed), "key", None)
    return None


def engine_payload(
    name: str,
    config: Any,
    *,
    launch_engine_reader: Callable[[str], tuple[str | None, str | None, str | None]]
    | None = None,
) -> dict:
    """JSON-ready engine verdict; any gather failure degrades to UNKNOWN.

    ``launch_engine_reader`` is the test seam, matching the injected-reader
    idiom used elsewhere in this package.
    """
    reader = launch_engine_reader or _read_running_engine
    try:
        running, observed_at, reason = reader(name)
    except Exception as exc:  # stx-allow: fallback (reason: an unreadable process environment is UNKNOWN with its reason — never a fabricated MATCH, and never a crashed health command)
        running, observed_at, reason = (
            None,
            None,
            f"{REASON_READ_ERROR} ({type(exc).__name__}: {exc})",
        )

    try:
        declared = _declared_engine(config)
    except Exception as exc:  # stx-allow: fallback (reason: same rule — a spec we cannot parse is UNKNOWN, not a match)
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
        "observed_at": observed_at,
    }


def print_engine(console: Any, payload: dict) -> None:
    """One verdict line; on MISMATCH, name both engines and the fix."""
    verdict = str(payload.get("verdict", VERDICT_UNKNOWN))
    colour = _VERDICT_COLOUR.get(verdict, "yellow")
    declared = payload.get("declared") or "?"
    running = payload.get("running") or "?"
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
