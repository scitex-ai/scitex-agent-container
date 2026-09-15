"""The start CLI exposes no source-currency bypass."""

from __future__ import annotations

import click

from scitex_agent_container._lifecycle._layers_preflight import ALLOW_ENV
from scitex_agent_container.cli_pkg.lifecycle._start_gate_options import (
    _set_env_when_given,
    spec_gate_options,
)


def _params(func) -> "dict[str, click.Parameter]":
    command = click.command()(func)
    return {p.name or p.opts[0]: p for p in command.params}


def test_only_the_layer_override_is_attached() -> None:
    decorated = spec_gate_options(lambda **kw: None)
    flags = [opt for parameter in _params(decorated).values() for opt in parameter.opts]
    assert flags == ["--allow-undeclared-layers"]


def test_layer_override_sets_its_environment(env_save_restore) -> None:
    env_save_restore.delete(ALLOW_ENV)
    _set_env_when_given(ALLOW_ENV)(None, None, True)
    import os

    assert os.environ[ALLOW_ENV] == "1"


def test_no_source_currency_override_is_exposed() -> None:
    decorated = spec_gate_options(lambda **kw: None)
    flags = [opt for parameter in _params(decorated).values() for opt in parameter.opts]
    assert "--allow-stale-spec" not in flags
