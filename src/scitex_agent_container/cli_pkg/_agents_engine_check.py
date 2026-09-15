"""Engine protocol checks that do not start a harness or container."""

from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path
from typing import Any

import click

from ..config import apply_engine, load_config, select_engine
from ..config._engine_honour import engine_verdict
from ..config._engine_tool_probe import build_probe_plan, probe_engine_tools
from ..config._harness_lookup import canonical_harness
from ..runtimes._apptainer_provider import resolve_provider_api_key


def _resolve_probe(spec_file: Path, engine: str | None, harness: str | None):
    config = load_config(spec_file)
    selected_harness = canonical_harness(harness or config.harness)
    if selected_harness is None:
        raise ValueError(f"unknown harness {harness or config.harness!r}")
    selected_key = engine or str(config.engine_key or "").strip() or None
    selected = select_engine(config.engines, selected_key)
    if selected is None:
        raise ValueError(
            "engine-check requires a named engine from the spec or fleet engine library"
        )
    verdict = engine_verdict(selected, harness=selected_harness)
    if not verdict.honourable:
        raise ValueError(
            f"engine {selected.key!r} is not honourable: {verdict.reason}. "
            f"Fix: {verdict.fix}"
        )
    apply_engine(config, selected)
    plan = build_probe_plan(config, harness=selected_harness)
    key = resolve_provider_api_key(config)
    return plan, key


@click.command("engine-check")
@click.argument(
    "spec_file", type=click.Path(exists=True, dir_okay=False, path_type=Path)
)
@click.option(
    "--harness",
    help="Probe with this harness wire protocol instead of the spec default.",
)
@click.option("--engine", help="Select this named engine for the probe.")
@click.option(
    "--count", type=click.IntRange(min=1, max=20), default=3, show_default=True
)
@click.option("--timeout", "timeout_s", type=click.FloatRange(min=1), default=30.0)
@click.option("--json", "as_json", is_flag=True, help="Emit machine-readable JSON.")
def engine_check(
    spec_file: Path,
    harness: str | None,
    engine: str | None,
    count: int,
    timeout_s: float,
    as_json: bool,
) -> None:
    """Check exact tool calls against the engine selected for SPEC_FILE."""
    try:
        plan, key = _resolve_probe(spec_file, engine, harness)
        results = [
            probe_engine_tools(plan, key=key, timeout_s=timeout_s) for _ in range(count)
        ]
        payload: dict[str, Any] = {
            "engine": plan.engine.key,
            "model": plan.engine.model_id,
            "harness": plan.harness,
            "protocol": plan.endpoint.protocol,
            "passed": sum(result.ok for result in results),
            "attempted": count,
            "results": [asdict(result) for result in results],
        }
        if as_json:
            click.echo(json.dumps(payload, indent=2))
        else:
            click.echo(
                f"{payload['engine']} ({payload['model']}) via "
                f"{payload['protocol']}: {payload['passed']}/{count} exact tool calls"
            )
            for index, result in enumerate(results, 1):
                outcome = "PASS" if result.ok else "FAIL"
                click.echo(f"  {index}: {outcome} {result.diagnostic}")
        if not all(result.ok for result in results):
            raise click.ClickException("engine tool-call conformance failed")
    except (OSError, RuntimeError, ValueError, json.JSONDecodeError) as exc:
        raise click.ClickException(str(exc)) from exc


def register(group: click.Group) -> None:
    """Register the engine conformance probe under ``sac agents``."""
    group.add_command(engine_check)


__all__ = ["engine_check", "register"]
