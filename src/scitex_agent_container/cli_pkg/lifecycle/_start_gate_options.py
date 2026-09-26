"""The ``sac agents start`` SPEC-GATE options, and how they reach the gates.

Two launch-time gates refuse a start (operator ruling 2026-08-10:
「スペックがおかしかったら起動不可っていうのをデフォルトに」):

* spec authority cannot be proven exact; and
* the spec declares no ``to_home_layers``, so what gets merged into the agent
  is invisible from the spec alone.

The authority gate has no bypass. A detached source is accepted only through
the immutable ``sac-authority/<source>-<commit>`` policy. The layers gate keeps
its separate, narrowly scoped migration override.

**How an override reaches the gate: the env var, set from a click callback.**
The layers override uses an environment variable because parallel start
re-execs one subprocess per agent. Authority policy stays in the lifecycle
implementation, shared by CLI and programmatic callers.
"""

from __future__ import annotations

import os

import click

from ..._lifecycle._launch_verify import DEFAULT_VERIFY_WINDOW_S, VERIFY_WINDOW_ENV
from ..._lifecycle._layers_preflight import ALLOW_ENV as ALLOW_LAYERS_ENV


def _true_or_unset(ctx, param, value):  # noqa: ARG001 - click callback signature
    """``--strict-drift`` yields ``True`` when passed and ``None`` when not.

    ``None`` preserves the historical call shape for an absent flag. The
    lifecycle policy is mandatory either way; this compatibility flag cannot
    make it weaker.
    """
    return True if value else None


def _set_env_when_given(env_var: str):
    """Build a click callback that sets ``env_var=1`` when the flag is passed.

    Only ever SETS the variable. A flag that is absent must leave an
    environment the operator exported themselves alone.
    """

    def _callback(ctx, param, value):  # noqa: ARG001 - click callback signature
        if value:
            os.environ[env_var] = "1"
        return value

    return _callback


def _set_verify_window_env(ctx, param, value):  # noqa: ARG001 - click callback signature
    """``--verify-window N`` -> ``SAC_START_VERIFY_WINDOW_S=N``.

    Same env transport as the gate overrides above, for the same reason:
    a multi-target start re-execs one subprocess per agent
    (``_start_parallel``) and only the environment survives that hop.
    Only SET when the flag is passed — an absent flag must leave an
    operator-exported value alone.
    """
    if value is not None:
        os.environ[VERIFY_WINDOW_ENV] = str(value)
    return value


#: ``--verify-window`` for ``sac agents start`` — the bounded wait for
#: launch evidence (v4 step 1). Lives in this module (rather than a line
#: in ``_start.py``) because it reuses the env-transport pattern above
#: and the click entry file sits at its per-file line budget.
verify_window_option = click.option(
    "--verify-window",
    type=float,
    default=None,
    expose_value=False,
    callback=_set_verify_window_env,
    help=(
        "Seconds to wait after launching for evidence the agent actually "
        "came up (fresh runner heartbeat, or the live TUI session) before "
        f"the start verdict. Default {DEFAULT_VERIFY_WINDOW_S:g}s; 0 "
        f"disables verification. Env: {VERIFY_WINDOW_ENV}."
    ),
)


def spec_gate_options(func):
    """Apply the two spec-gate flags to a click command, in help order."""
    options = (
        click.option(
            "--strict-drift",
            "strict_drift",
            is_flag=True,
            default=False,
            callback=_true_or_unset,
            help="Explicitly request the mandatory fail-closed spec-authority "
            "gate. Retained for compatibility; authority has no bypass.",
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


__all__ = ["spec_gate_options", "verify_window_option"]
