"""The ternary liveness verdict, rendered for ``sac agents health``.

Extracted from :mod:`.status_cmds` (512-line per-file cap).

WHY THIS EXISTS NEXT TO ``healthy``, RATHER THAN REPLACING IT
-------------------------------------------------------------
``sac agents health`` answers with a BOOL (``healthy``), and a bool cannot say
"I could not tell". It reports one of two poles no matter how little was
actually observed — so a reader cannot distinguish *"we watched a message reach
this agent"* from *"every probe we ran failed and we guessed"*. Those are the
same output today, and acting on them as if they were the same is the bug.

So we publish the VERDICT and ITS EVIDENCE alongside the bool, and leave the
bool alone. Two reasons it is additive rather than a replacement:

1. ``healthy`` gates this command's EXIT CODE, and automation keys on that exit
   code. Re-deriving it from a new signal would change what every one of those
   callers does, silently, in one step.
2. It is the same restraint :mod:`.._listen._reachability` already practises —
   the observation is published NEXT TO the declaration, never overwriting it —
   and that restraint is precisely what keeps a watchdog from wiring itself to a
   signal that would kill healthy agents.
"""

from __future__ import annotations

from typing import Any

__all__ = ["liveness_payload", "print_inbox", "print_liveness"]

# wedged = present but NOT working — magenta, distinct from unknown's yellow so
# a "known-stuck, needs a restart" reads apart from a "we could not tell". It is
# NOT red: red is DEAD (destroyable), and a wedged agent must never be destroyed.
_VERDICT_COLOUR = {
    "alive": "green",
    "dead": "red",
    "unknown": "yellow",
    "wedged": "magenta",
}


def liveness_payload(name: str, config: Any) -> dict:
    """Resolve ``name``'s ternary verdict + evidence as a JSON-ready dict.

    Tolerant by construction: any failure to gather degrades to an UNKNOWN
    verdict carrying the reason — never to a fabricated DEAD (whose remedy is
    destructive), and never to an exception that takes the health command down.
    """
    from .._lifecycle._runtime_select import _get_runtime
    from .._lifecycle._verdict import (
        INSTRUMENT_NO_OBSERVATION,
        SOURCE_RESOLVER,
        UNKNOWN,
        LivenessVerdict,
        Signal,
    )
    from .._lifecycle._verdict_resolve import resolve_verdict

    try:
        return resolve_verdict(name, config, _get_runtime(config)).to_dict()
    except Exception as exc:  # stx-allow: fallback (reason: an un-gatherable verdict is UNKNOWN with its reason — never a fabricated DEAD, and never a crashed health command)
        return LivenessVerdict(
            agent=name,
            verdict=UNKNOWN,
            signals=(
                Signal(
                    SOURCE_RESOLVER,
                    UNKNOWN,
                    f"could not gather liveness evidence ({type(exc).__name__}: {exc})",
                    INSTRUMENT_NO_OBSERVATION,
                ),
            ),
        ).to_dict()


def print_liveness(console: Any, liveness: dict) -> None:
    """Print the verdict AND why, plus what it does (not) authorise."""
    verdict = str(liveness.get("verdict", "unknown"))
    colour = _VERDICT_COLOUR.get(verdict, "yellow")
    summary = liveness.get("summary", "?")
    console.print(f"[{colour}]liveness: {summary}[/{colour}]")
    veto = liveness.get("destroy_veto_reason")
    if veto:
        console.print(
            f"[dim]  destructive action NOT authorised on this evidence: {veto}[/dim]"
        )


def print_inbox(console: Any, name: str, subscribers: int, reachable: str) -> None:
    """Render the inbox-reachability observation (moved verbatim from
    ``status_cmds.health`` — the 512-line per-file cap)."""
    from .._listen._reachability import UNKNOWN, UNREACHABLE

    if reachable == UNREACHABLE:
        console.print(
            f"[yellow]inbox: NOT REACHABLE — 0 live subscribers. "
            f"a2a_send to '{name}' will reach nobody (messages are queued and "
            f"replayed when its channel adapter reconnects). The process is "
            f"up; its inbox adapter is not attached. Do NOT force-restart on "
            f"this alone.[/yellow]"
        )
    elif reachable == UNKNOWN:
        console.print(
            "[dim]inbox: unknown (could not reach sac listen to observe "
            "subscribers)[/dim]"
        )
    else:
        console.print(f"[green]inbox: reachable ({subscribers} subscriber(s))[/green]")
