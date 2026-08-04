"""The local liveness probe, and a RECORD of how it answered.

WHY THIS MODULE EXISTS. ``sac agents list`` has reported a demonstrably
running agent as ``stopped`` three times now. Twice it was fixed
(``liveness-live-agents-read-stopped``; ``sac-fix-live-agents-read-stopped``,
2026-07-08). The third report (scitex-dev, 2026-08-04) could not be
diagnosed AT ALL: the host rebooted between their measurement and the
investigation, the population moved, and the verdict itself carried no
trace of how it had been reached. A ``stopped`` row said "stopped" and
nothing else — not which runtime adapter answered, and for an ``unknown``
row, not which exception produced it (the old ``except Exception: return
None`` discarded the exception object entirely).

That is a MISSING FACILITY, not poor investigation. A defect that keeps
recurring and keeps having to be re-derived from scratch is telling you
the subsystem cannot answer questions about itself. So this module makes
the probe RECORD what it did, and the recording rides in
``sac agents list --json`` where it needs no debug flag and no
reproduction: a third occurrence will read ``stopped via
ClaudeSessionRuntime`` on a ``tui`` agent, which IS the bug, stated.

THE LABEL MUST COME FROM INSIDE THE PROBE. The cheap alternative — have
the row builder call ``_get_runtime(cfg).__class__.__name__`` on its own —
is worse than no label at all. If the probe were ever hardcoded to one
adapter again (the exact historical bug), the row would report the
CORRECTLY selected adapter while the probe used the wrong one: a label
that lies precisely in the case it exists to catch. So the adapter name
is a return value of the probe call itself and cannot disagree with it.
"""

from __future__ import annotations

from dataclasses import dataclass

__all__ = ["LocalProbe", "probe_local_detail"]


@dataclass(frozen=True)
class LocalProbe:
    """What the local liveness probe did, not merely what it concluded.

    ``running`` keeps the original tri-state and its meaning is unchanged:
    ``True``/``False`` are answers, ``None`` is an ABSTENTION (the probe
    could not run) and must never be read as "stopped".

    ``runtime`` is the adapter class that actually answered — ``None`` only
    when runtime selection itself failed, i.e. when nothing answered.
    ``error`` is populated exactly when ``running is None``, and says which
    step failed.
    """

    running: bool | None
    runtime: str | None
    error: str | None


def probe_local_detail(cfg) -> LocalProbe:
    """Probe an agent's liveness via its DECLARED runtime adapter.

    Must select the SAME runtime ``agent_status`` uses
    (:func:`_lifecycle._runtime_select._get_runtime`, which branches on
    ``spec.runtime``) — NOT a hardcoded ``ClaudeSessionRuntime``. That
    hardcode was the root cause of "live agent reads stopped": the DEFAULT
    runtime is ``tui``, whose liveness is the ``tui-<name>`` tmux session's
    pane process. Probing a live TUI agent through ``ClaudeSessionRuntime``
    reached ``ApptainerContainerRuntime.is_running`` → ``os.kill(read_pid,
    0)``, but a TUI agent NEVER writes an ``apptainer_pid`` file, so
    ``_read_pid`` returned ``None`` → ``is_running`` returned ``False`` →
    status "stopped" for a provably-running agent.

    Never raises. The two failure modes are recorded SEPARATELY because
    they mean different things: selection failing means we never chose an
    instrument, while ``is_running`` raising means a NAMED instrument was
    asked and could not answer. Collapsing both to a bare ``None`` is what
    made the third recurrence undiagnosable.
    """
    # stx-allow: fallback (reason: runtime may be missing or the spec may be
    # unloadable for an agent that never ran; that is an ABSTENTION, not a
    # crash and not a "stopped" — and the reason is kept, not discarded.)
    try:
        from ..._lifecycle._runtime_select import _get_runtime

        runtime = _get_runtime(cfg)
    except Exception as exc:  # stx-allow: fallback (reason: see inline comment)
        return LocalProbe(running=None, runtime=None, error=f"runtime select: {exc}")
    label = type(runtime).__name__
    # stx-allow: fallback (reason: a named instrument was asked and could not
    # answer; record WHICH one, because that is the whole diagnostic value.)
    try:
        return LocalProbe(
            running=bool(runtime.is_running(cfg)), runtime=label, error=None
        )
    except Exception as exc:  # stx-allow: fallback (reason: see inline comment)
        return LocalProbe(
            running=None, runtime=label, error=f"{label}.is_running: {exc}"
        )
