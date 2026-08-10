"""The ``sac agents start`` SPEC-GATE options, and how they reach the gates.

Two launch-time gates refuse a start by default (operator ruling 2026-08-10:
「スペックがおかしかったら起動不可っていうのをデフォルトに」):

* the spec source is STALE — BEHIND / DIVERGED from its remote, so the spec
  about to launch may be an old one; and
* the spec declares no ``to_home_layers``, so what gets merged into the agent
  is invisible from the spec alone.

Each gate has exactly ONE named override. Deliberately not a blanket
``--force`` / ``--ignore-warnings``: a generic override skips every check at
once, leaves no record of WHICH check was bypassed, and gets reused by the next
person for an unrelated failure. ``git --no-verify`` is the cautionary example,
and this fleet already bans that idiom by hook.

**How an override reaches the gate: the env var, set from a click callback.**
Both overrides are ``expose_value=False`` — click never passes them to the
command function; their callbacks set the gate's env var instead. That is not
a shortcut around plumbing, it is the only transport that survives the
PARALLEL path: a multi-target ``sac agents start`` re-execs itself as one
SUBPROCESS PER AGENT (``_start_parallel``), and a subprocess inherits the
environment, not the parent's local variables. Threading two more keywords
through ``start -> maybe_run_parallel -> run_single_targets -> agent_start``
would also have to append two more flags to that child argv to work at all.

The gates themselves read those env vars as their documented override
(``_lifecycle._start_preflight._resolve_strict_drift`` and
``_lifecycle._layers_preflight``), so the CLI adds no second source of truth.

PRECEDENCE, stated because both can be passed at once: an explicit
``--strict-drift`` wins over ``--allow-stale-spec``, because
``_resolve_strict_drift`` gives an explicit argument priority over the env.
The stricter of two contradictory instructions is the safe winner, and the
``--allow-stale-spec`` help says so rather than leaving it to be discovered.
"""

from __future__ import annotations

import os

import click

from ..._drift._local import ALLOW_STALE_ENV
from ..._lifecycle._layers_preflight import ALLOW_ENV as ALLOW_LAYERS_ENV


def _set_env_when_given(env_var: str):
    """Build a click callback that sets ``env_var=1`` when the flag is passed.

    Only ever SETS the variable. A flag that is absent must leave an
    environment the operator exported themselves alone — silently unsetting it
    would make ``SAC_ALLOW_STALE_SPEC=1 sac agents start x`` behave differently
    from the same export two lines earlier in a shell script.
    """

    def _callback(ctx, param, value):  # noqa: ARG001 - click callback signature
        if value:
            os.environ[env_var] = "1"
        return value

    return _callback


def spec_gate_options(func):
    """Apply the three spec-gate flags to a click command, in help order."""
    options = (
        click.option(
            "--strict-drift",
            "strict_drift",
            is_flag=True,
            default=False,
            help="Hard-block on a STALE spec-source git repo. This is now the "
            "DEFAULT; the flag remains so a caller can state it explicitly.",
        ),
        click.option(
            "--allow-stale-spec",
            is_flag=True,
            default=False,
            expose_value=False,
            callback=_set_env_when_given(ALLOW_STALE_ENV),
            help="Start even though the spec source is BEHIND/DIVERGED from "
            f"its remote ({ALLOW_STALE_ENV}=1). Ignored if --strict-drift is "
            "also passed.",
        ),
        click.option(
            "--allow-undeclared-layers",
            is_flag=True,
            default=False,
            expose_value=False,
            callback=_set_env_when_given(ALLOW_LAYERS_ENV),
            help="Start even though the spec declares no 'to_home_layers' "
            f"({ALLOW_LAYERS_ENV}=1).",
        ),
    )
    for option in reversed(options):
        func = option(func)
    return func


__all__ = ["spec_gate_options"]
