"""START-TIME engine selection — pick one, or REFUSE naming what failed.

This is where ``spec.engines`` becomes a launch. The loader has already
folded the DEFAULT engine onto the config (purely, with no host
questions asked); this module runs on the START path only, and it is
the only place that:

  1. honours ``--engine <key>`` for THIS start (operator answer Q2:
     start time only — there is no per-turn hatch and nothing rebinds
     mid-session), and
  2. asks the HOST whether the selected engine can be honoured, and
     REFUSES the start when it cannot (operator answer Q3).

WHY HERE AND NOT IN THE LOADER. ``sac agents list`` loads every spec on
the machine. A loader that resolved ``$API_KEY`` per spec, or dialled
each declared endpoint, would answer a question nobody asked once per
spec and would contaminate ``--json`` with warnings. Same ruling that
put ``warn_if_legacy_harness_key`` and
``warn_if_legacy_apptainer_runtime`` on the start path.

NO SILENT FALLBACK, in three specific ways the operator named:

  * an unknown ``--engine`` key does NOT degrade to the default — it
    raises, listing the declared keys;
  * an unhonourable engine does NOT degrade to another engine — it
    raises, naming the engine key, what was unhonourable, and the fix;
  * a reachability verdict of "could not tell" is NOT read as
    honourable — it emits a LOUD warning naming the engine and what
    could not be determined, and the start proceeds only because
    refusing on an undetermined network would ground the fleet the
    first time a link flaps (the hazard recorded on
    ``hub-cards-dsn-unreachable-should-refuse-to-boot-20260815``). The
    warning is the difference between "proceeding despite not knowing"
    and "pretending to know".

REACHABILITY IS OPT-IN, and this is stated in the ``--engine`` help,
in :mod:`config._engine_honour`, and in the ADR: static resolution runs
on EVERY start and is the whole refusal surface by default; the live
probe runs only under ``--probe-engine`` / ``SAC_ENGINE_PROBE=1``, on a
short bounded timeout, and only an ACTIVE connection refusal (a
definite answer) is allowed to refuse a start.
"""

from __future__ import annotations

import os
from typing import Any

from ..config._engine_honour import (
    ENGINE_PROBE_ENV,
    PROBE_TIMEOUT_S,
    EngineVerdict,
    engine_verdict,
)
from ..config._engine_types import (
    ENGINES_KEY,
    EngineSpec,
    apply_engine,
    select_engine,
)

__all__ = [
    "EngineNotHonourableError",
    "check_engine_before_stop",
    "engine_probe_requested",
    "refusal_message",
    "select_engine_at_start",
]


class EngineNotHonourableError(RuntimeError):
    """The selected engine cannot be honoured, so the start REFUSES."""


def _logger():
    """scitex-logging logger, imported lazily.

    Same pattern (and the same reason) as
    ``config._harness_types._harness_logger``: the fleet-consistent
    coloured stderr line, imported inside the function so importing the
    lifecycle package does not pay scitex-logging's first-import
    auto-configuration.
    """
    import scitex_logging

    return scitex_logging.getLogger(__name__)


def engine_probe_requested(explicit: bool | None = None) -> bool:
    """Whether THIS start should run the live reachability probe.

    ``explicit`` is the CLI flag (``--probe-engine`` / None when not
    passed). With no flag, the ops-only ``SAC_ENGINE_PROBE`` env var
    decides, and its default is OFF — a start must not depend on a
    possibly-remote endpoint answering unless someone asked for that.
    """
    if explicit is not None:
        return bool(explicit)
    return os.environ.get(ENGINE_PROBE_ENV, "").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    )


def refusal_message(
    agent_name: str, verdict: EngineVerdict, *, explicit: bool
) -> str:
    """The refusal an unhonourable engine produces at start.

    Names, in order: the agent, the engine KEY, HOW that engine was
    selected (an explicit ``--engine`` or the spec's default — so the
    operator knows which line to edit), WHAT could not be honoured, and
    the FIX. Ends by stating the no-fallback rule explicitly, because
    the absence of a fallback is the surprising part for anyone used to
    tools that quietly pick something that works.
    """
    how = (
        f"--engine {verdict.engine}"
        if explicit
        else f"the spec's default engine (spec.{ENGINES_KEY}.{verdict.engine})"
    )
    probed = " (live endpoint probe)" if verdict.probed else " (static resolution)"
    return (
        f"REFUSING to start agent {agent_name!r} on engine "
        f"{verdict.engine!r}, selected by {how}: {verdict.reason}"
        f"{probed}. Fix: {verdict.fix}. sac does NOT fall back to another "
        "engine — not to the default, and not to the plain Anthropic "
        "backend — because a start that silently ran a different backend "
        "than the one declared is worse than a start that did not happen."
    )


def select_engine_at_start(
    config: Any,
    requested: str | None = None,
    *,
    probe: bool | None = None,
    timeout_s: float = PROBE_TIMEOUT_S,
    log: bool = True,
) -> EngineSpec | None:
    """Resolve THIS start's engine onto ``config``, or refuse.

    Returns the selected :class:`EngineSpec`, or ``None`` for a legacy
    single-backend spec with no ``--engine`` asked for (the unchanged
    path — no engines block, nothing to select, nothing to refuse).

    Raises :class:`config._engine_types.UnknownEngineError` when
    ``requested`` names an engine the spec does not declare, and
    :class:`EngineNotHonourableError` when the selected engine cannot be
    honoured. Emits a LOUD warning — never a silent pass — when the
    optional probe could not reach a verdict.
    """
    engines = dict(getattr(config, "engines", {}) or {})
    explicit = bool((requested or "").strip())
    if not engines and not explicit:
        return None

    # Raises UnknownEngineError listing the declared keys. Deliberately
    # NOT caught: degrading to the default here is the exact silent
    # fallback the operator ruled out.
    engine = select_engine(engines, requested)
    if engine is None:
        return None

    # Re-fold even when the loader already applied this same entry: the
    # config may have been mutated between load and start, and applying
    # an engine is idempotent.
    apply_engine(config, engine)

    verdict = engine_verdict(
        engine, probe=engine_probe_requested(probe), timeout_s=timeout_s
    )
    agent_name = getattr(config, "name", "<unknown>")
    if verdict.refuses:
        message = refusal_message(agent_name, verdict, explicit=explicit)
        if log:
            _logger().error(message)
        raise EngineNotHonourableError(message)
    if verdict.undetermined:
        # COULD NOT TELL is its own state and must read as its own state.
        # Proceeding is the right call (a flapping link must not ground
        # the fleet), but proceeding QUIETLY would turn "I do not know"
        # into "it is fine" — which is the claim this warning exists to
        # refuse to make.
        warning = (
            f"engine {engine.key!r} for agent {agent_name!r}: reachability "
            f"COULD NOT BE DETERMINED — {verdict.reason}. Starting anyway "
            "(an undetermined network is not evidence the endpoint is "
            "down), but this start is NOT a verified-honourable start. "
            f"{verdict.fix}."
        )
        if log:
            _logger().warning(warning)
    return engine


def check_engine_before_stop(
    config_path: str,
    requested: str | None = None,
    *,
    probe: bool | None = None,
    timeout_s: float = PROBE_TIMEOUT_S,
    log: bool = True,
) -> None:
    """Refuse a RESTART before its stop leg when the engine is unhonourable.

    WHY A SECOND CALL SITE. On ``sac agents start`` the engine refusal
    already runs before anything is torn down: it sits in
    :func:`_start_prelaunch.run_prelaunch`, ahead of the ``--force``
    stop. ``agent_restart`` is the other shape — it stops FIRST and then
    calls ``agent_start`` — so the same refusal, reached through that
    path, would fire on an agent that is ALREADY DOWN and leave it down.
    A wrong-backend start is worse than no start (answer Q3); a stopped
    agent that never comes back is worse than both, and a bare ``--engine
    qwen38-27bb`` typo would have bought exactly that.

    This is the same one-way-trip hazard the successor-credential
    pre-flight was added for (incident
    ``incident-agent-self-restart-one-way-20260712``), so this check
    stands beside it, in the same window, for the same reason: everything
    that can refuse a restart must refuse while the OLD process is still
    up and re-startable.

    Loads ``config_path`` and runs the ordinary
    :func:`select_engine_at_start` against a THROWAWAY config — the start
    leg re-loads and re-selects, so nothing is carried across and the two
    cannot disagree. A spec with no ``engines:`` block and no ``--engine``
    returns immediately, so the legacy corpus pays one config load and
    nothing else.

    Raises the same errors ``select_engine_at_start`` raises —
    :class:`config._engine_types.UnknownEngineError` and
    :class:`EngineNotHonourableError` — and returns ``None`` when the
    engine can be honoured.
    """
    # Runs with or without ``--engine``: a spec whose DEFAULT engine has
    # an unset token takes the agent down just as surely as a typo'd key,
    # and both are decidable here, before the stop.
    from ..config import load_config

    config = load_config(config_path)
    select_engine_at_start(
        config, requested, probe=probe, timeout_s=timeout_s, log=log
    )
