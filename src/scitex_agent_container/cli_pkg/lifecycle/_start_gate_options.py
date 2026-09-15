"""The ``sac agents start`` spec-gate options.

Source currency is an unconditional lifecycle invariant and therefore has no
CLI option. This module only carries the independent home-layer declaration
gate and launch-verification window.

"""

from __future__ import annotations

import os

import click

from ..._lifecycle._launch_verify import DEFAULT_VERIFY_WINDOW_S, VERIFY_WINDOW_ENV
from ..._lifecycle._layers_preflight import ALLOW_ENV as ALLOW_LAYERS_ENV


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
    """Apply the home-layer gate override to a click command."""
    options = (
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
