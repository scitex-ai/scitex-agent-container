"""Launch-time gate: a spec must DECLARE the ``to_home`` layers it inherits.

Why this is a launch preflight and not a resolver concern
---------------------------------------------------------
The "declares no ``to_home_layers``" finding used to be a ``logger.warning``
inside :func:`..runtimes._to_home_resolve.settings_layer_dirs`. That function
is a PURE resolver — it answers "which directories cascade into this agent" —
and a start calls it **twice**: once for the workspace home
(``claude_session.py`` -> ``deploy_to_home``) and once for the apptainer overlay
upper (``deploy_to_home_overlay`` -> ``deploy_to_home``). The TUI runtime does
the same. So one agent's start printed the same paragraph two times, which is
how a message teaches people to scroll past it.

The finding is not a property of a path lookup. It is a property of the SPEC,
decided ONCE, before any deploy work happens. Putting it here fixes the
duplication structurally (one call site, in ``agent_start``) and puts the
refusal where a refusal belongs: before the runtime is built or touched.

The refusal, and its escape hatch
---------------------------------
Operator ruling 2026-08-10: a bad spec REFUSES TO START by default, with an
explicit named override rather than a blanket ``--force``. So:

* undeclared layers -> :class:`UndeclaredToHomeLayers` raised before launch;
* ``--allow-undeclared-layers`` / ``SAC_ALLOW_UNDECLARED_LAYERS=1`` starts
  anyway, and says so at ERROR level naming the condition AND the agent. An
  override that is silent is just a slower version of the warning nobody read.

The override NAMES THE CONDITION on purpose. A generic ``--force`` skips every
check at once, leaves no record of which one was bypassed, and gets reused by
the next person for an unrelated failure — ``git --no-verify`` is the
cautionary example, and this fleet bans that idiom by hook.

Sequencing: :data:`ENFORCE_BY_DEFAULT`
--------------------------------------
Measured 2026-08-10 on the live registry: 101 of 102 fleet specs are
UNDECLARED. Flipping the refusal on before those specs are migrated would stop
the entire fleet from booting, so enforcement is currently OPT-IN via
``SAC_ENFORCE_TO_HOME_LAYERS=1``.

:data:`ENFORCE_BY_DEFAULT` is the one-line switch. Flip it to ``True`` once
``sac agents migrate-layers --apply`` has landed in the dotfiles repo (the
specs are tracked files in a SHARED repo, so that is a PR there, not a sweep
here). The env var and this constant then become redundant and should be
deleted together; ``--allow-undeclared-layers`` is the part that survives.
"""

from __future__ import annotations

import logging

from ..runtimes._to_home_errors import UndeclaredToHomeLayers

logger = logging.getLogger(__name__)

#: Flip to ``True`` to make an undeclared spec refuse to start by DEFAULT.
#: Gated on the fleet's 102 specs being migrated first — see the module
#: docstring. ``SAC_ENFORCE_TO_HOME_LAYERS=1`` enables it per-invocation today.
ENFORCE_BY_DEFAULT = False

#: The named override. One flag per condition, never a blanket ``--force``.
ALLOW_FLAG = "--allow-undeclared-layers"
ALLOW_ENV = "SAC_ALLOW_UNDECLARED_LAYERS"
ENFORCE_ENV = "SAC_ENFORCE_TO_HOME_LAYERS"

_TRUTHY = ("1", "true", "yes", "on")


def _env_flag(name: str, default: bool) -> bool:
    """Read a sac env flag (either prefix), falling back to ``default``.

    ``name`` is passed WITHOUT the ``SAC_`` prefix to the sac env helper, which
    accepts both ``SAC_<NAME>`` and ``SCITEX_AGENT_CONTAINER_<NAME>``.
    """
    from .._env import getenv as _sac_env

    raw = (_sac_env(name.removeprefix("SAC_"), "") or "").strip().lower()
    if not raw:
        return default
    return raw in _TRUTHY


def _resolve_allow_undeclared(allow: "bool | None") -> bool:
    """Effective override state: explicit arg wins, else the env, else off."""
    if allow is not None:
        return allow
    return _env_flag(ALLOW_ENV, False)


def _resolve_enforce(enforce: "bool | None") -> bool:
    """Effective enforcement state: explicit arg wins, else env, else default."""
    if enforce is not None:
        return enforce
    return _env_flag(ENFORCE_ENV, ENFORCE_BY_DEFAULT)


def _inherited_layer_names(config) -> str:
    """The layers this undeclared spec is silently inheriting, for the message.

    Best-effort by design: the point of the message is the MISSING declaration,
    and a resolver hiccup must not replace a precise complaint with a stack
    trace about something else.
    """
    # stx-allow: fallback (reason: this only enriches an error message; a
    # resolution failure here must not mask the declaration complaint itself)
    try:
        from ..runtimes._to_home_resolve import settings_layer_dirs

        names = [name for name, path in settings_layer_dirs(config) if path is not None]
    except Exception as exc:
        return f"<unresolvable: {type(exc).__name__}: {exc}>"
    return ", ".join(names) or "none"


def undeclared_layers_lines(config, *, bypassed: bool = False) -> "list[str]":
    """The banner for an undeclared spec — the refusal, or the bypass notice.

    Named ``ERROR`` in both forms. A bypass is not a milder event than the
    refusal it replaced: it is the same defect, deliberately admitted.
    """
    name = getattr(config, "name", "<unnamed>") or "<unnamed>"
    spec = getattr(config, "config_path", "") or "<unknown spec path>"
    inherited = _inherited_layer_names(config)
    bar = "!" * 72
    verdict = (
        f"sac-layers BYPASSED for agent '{name}': starting anyway because "
        f"{ALLOW_FLAG} / {ALLOW_ENV} was given."
        if bypassed
        else f"sac-layers ERROR for agent '{name}': refusing to start."
    )
    return [
        bar,
        verdict,
        "  the spec declares no 'to_home_layers', so what gets merged into",
        "  this agent is invisible from the spec alone.",
        f"  spec:      {spec}",
        f"  inherits:  {inherited}",
        "  fix:       sac agents migrate-layers --apply   "
        "(writes the one line it already resolves)",
        f"  override:  {ALLOW_FLAG}   /   {ALLOW_ENV}=1",
        bar,
    ]


def check_to_home_layers_at_launch(
    config,
    *,
    allow_undeclared: "bool | None" = None,
    enforce: "bool | None" = None,
) -> bool:
    """Refuse (or loudly permit) a spec that declares no ``to_home_layers``.

    Returns ``True`` when the spec is declared (or enforcement is off and the
    spec is undeclared — reported, not raised). Raises
    :class:`UndeclaredToHomeLayers` when the spec is undeclared, enforcement is
    on, and no override was given.

    Called EXACTLY ONCE per :func:`.._start.agent_start`, which is the whole
    point: the resolver it replaced ran twice per start.
    """
    if getattr(config, "to_home_layers", None) is not None:
        return True

    allow = _resolve_allow_undeclared(allow_undeclared)
    if not _resolve_enforce(enforce):
        # Not yet enforcing: one line, once, naming the agent and the fix.
        # Deliberately NOT the full banner — until the flip this is a to-do
        # list for the migration, not an event anyone must act on right now.
        logger.warning(
            "to_home: agent %r declares no 'to_home_layers' and is inheriting "
            "the implicit cascade (%s). Fix: sac agents migrate-layers --apply. "
            "This becomes a start REFUSAL once the fleet's specs are migrated.",
            getattr(config, "name", "<unnamed>"),
            _inherited_layer_names(config),
        )
        return True

    lines = undeclared_layers_lines(config, bypassed=allow)
    for line in lines:
        logger.error("%s", line)
    if allow:
        return True
    raise UndeclaredToHomeLayers("\n".join(lines))


__all__ = [
    "ALLOW_ENV",
    "ALLOW_FLAG",
    "ENFORCE_BY_DEFAULT",
    "ENFORCE_ENV",
    "UndeclaredToHomeLayers",
    "check_to_home_layers_at_launch",
    "undeclared_layers_lines",
]
