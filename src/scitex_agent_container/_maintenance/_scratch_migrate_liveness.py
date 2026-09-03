"""Is this agent running — and may the answer be believed from HERE?

The liveness half of the ADR-0024 migration (:mod:`._scratch_migrate`), split
out of it so each file holds one responsibility. The sweep ``rmtree``s an
agent's ``/uvwork``, so "stopped" is the single most consequential word in the
plan and it gets its own module.

Two questions, deliberately separate:

* :func:`agent_liveness` asks the agent's DECLARED runtime adapter — the same
  one ``sac agents status`` uses (:func:`.._lifecycle._runtime_select._get_runtime`),
  never a second opinion, so the verb and the status command can never
  disagree about who is running.
* :func:`liveness_vantage` asks whether this process is even in a position to
  read that answer. It is the guard a live dry-run proved necessary; the
  measurement is in its docstring.

``None`` is a first-class answer here. The plan treats it as a refusal, never
as "stopped", so every path that cannot produce a trustworthy verdict lands
there rather than inventing one.
"""

from __future__ import annotations

import os

#: Set by apptainer/singularity inside every container it runs.
CONTAINER_MARKER_ENV = ("APPTAINER_CONTAINER", "SINGULARITY_CONTAINER")


def liveness_vantage(env: dict | None = None) -> str:
    """``""`` when host pids are resolvable from here; else WHY they are not.

    MEASURED 2026-09-03, from inside the ``scitex-agent-container`` agent —
    the agent that was running the probe:

        runtime/scitex-agent-container/apptainer_pid   3190806
        max pid visible in this container's /proc         74275
        os.kill(3190806, 0)                    ProcessLookupError

    The adapter's ``is_running`` is ``_read_pid`` + ``os.kill(pid, 0)``. In a
    container with its own PID namespace that pid belongs to nobody, so the
    probe answered **False** — "stopped" — for an agent whose own turn was
    executing the call. A sweep that believed it would ``rmtree`` the
    ``/uvwork`` of a live container while its overlay is mounted.

    The pid file is not wrong and the adapter is not wrong; the VANTAGE is.
    So this is not a better heuristic — it is an abstention. Every agent's
    liveness comes back UNKNOWN, and UNKNOWN is a refusal (never "stopped"),
    with the fix in the message: run the verb on the host. Sizes still print,
    because the overlays are read through a bind mount and that reading IS
    faithful; only the pid probe is not.

    ``env`` defaults to ``os.environ``; pass a dict in tests.
    """
    values = os.environ if env is None else env
    for key in CONTAINER_MARKER_ENV:
        image = (values.get(key) or "").strip()
        if image:
            return (
                f"this process runs INSIDE a container ({key}={image}), which "
                f"has its own PID namespace, so an agent's recorded host pid "
                f"resolves to nobody here and every agent reads as 'stopped'. "
                f"Run `sac agents scratch-migrate` on the HOST instead."
            )
    return ""


def agent_liveness(config) -> tuple[bool | None, str]:
    """``(running, detail)`` via the agent's DECLARED runtime adapter.

    ``None`` means the instrument could not answer, and ``detail`` says which
    instrument and why — the plan treats that as a refusal, never as
    "stopped". Same selection as ``sac agents status``.

    Abstains up front when :func:`liveness_vantage` says the probe cannot see
    host processes from here: an answer the vantage cannot support is worse
    than no answer, because this one authorises a delete.
    """
    blind = liveness_vantage()
    if blind:
        return None, blind
    # stx-allow: fallback (reason: an adapter that cannot be selected or that
    # raises is an ABSTENTION recorded with its cause, not a crash of the
    # whole sweep and not a "stopped" verdict)
    try:
        from .._lifecycle._runtime_select import _get_runtime

        runtime = _get_runtime(config)
    except Exception as exc:  # stx-allow: fallback (reason: see above)
        return None, f"runtime select: {exc}"
    label = type(runtime).__name__
    try:
        return bool(runtime.is_running(config)), label
    except Exception as exc:  # stx-allow: fallback (reason: see above)
        return None, f"{label}.is_running: {exc}"


__all__ = ["CONTAINER_MARKER_ENV", "agent_liveness", "liveness_vantage"]
