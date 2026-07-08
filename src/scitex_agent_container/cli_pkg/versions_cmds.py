"""``sac versions`` — scitex-* versions across the layers sac owns.

scitex-dev's ``ecosystem check-versions`` reports scitex-* versions across
6 layers (PyPI, GitHub, both hosts' venvs, CI, editable) but cannot see the
2 layers only sac owns: the shared base-image venv and the per-agent overlay
venvs. This verb exposes those as a flat JSON list so scitex-dev's drift
aggregator can fold all 8 together.

Contract — a flat list of ``{agent, layer, image, package, version, source}``:
  * ``layer``  ∈ {"base-image", "agent-overlay"}
  * base-image rows: ``agent="*"``, one set per base SIF; ``image`` = the SIF
    name (e.g. "sac-base", "sac-scitex").
  * agent-overlay rows: ``agent=<name>``, ONLY the scitex-* packages the
    agent's overlay adds/overrides vs its base (usually empty); ``image`` =
    the base SIF that agent runs on.
  * ``source`` ∈ {"manifest", "live"}.

sac emits RAW base + overlay rows; scitex-dev's aggregator derives the
"effective" (overlay-else-base) view.
"""

from __future__ import annotations

import json as _json

import click

from .._drift.versions import LAYER_BASE, collect_versions

# Column order for the optional human-readable table.
_COLUMNS = ("agent", "layer", "image", "package", "version", "source")


def _render_table(rows: list[dict]) -> str:
    """Render rows as a fixed-width table (nice-to-have; --json is canonical)."""
    if not rows:
        return "(no scitex-* versions discovered)"
    widths = {
        col: max(len(col), *(len(str(r.get(col, ""))) for r in rows))
        for col in _COLUMNS
    }
    header = "  ".join(col.upper().ljust(widths[col]) for col in _COLUMNS)
    sep = "  ".join("-" * widths[col] for col in _COLUMNS)
    lines = [header, sep]
    for r in rows:
        lines.append(
            "  ".join(str(r.get(col, "")).ljust(widths[col]) for col in _COLUMNS)
        )
    return "\n".join(lines)


@click.command("versions")
@click.option(
    "--json/--table",
    "as_json",
    default=True,
    help="Emit the flat JSON list (default, the LOCKED contract) or a "
    "human-readable table.",
)
@click.option(
    "--live",
    is_flag=True,
    default=False,
    help="Force ground-truth reads (exec `pip list` per base SIF, scan each "
    "overlay venv) instead of the near-zero baked-manifest path. This is "
    "what produces real output on images not yet rebuilt with the manifest.",
)
@click.option(
    "--containers-dir",
    default=None,
    help="Override the base-image containers dir "
    "(default: ~/.scitex/agent-container/containers). Mainly for testing.",
)
@click.option(
    "--base-only",
    is_flag=True,
    default=False,
    help="Emit only the base-image layer (skip per-agent overlay enumeration).",
)
def versions(as_json, live, containers_dir, base_only):
    """Report scitex-* package versions for sac's base + overlay layers.

    \b
    Examples:
      $ sac versions --json                 # baked-manifest path (routine)
      $ sac versions --json --live          # ground-truth exec pip-list
      $ sac versions --table                # human-readable
    """
    rows = collect_versions(
        live=live,
        containers_dir=containers_dir,
        agent_configs=[] if base_only else None,
    )
    if as_json:
        click.echo(_json.dumps(rows, indent=2))
    else:
        click.echo(_render_table(rows))


__all__ = ["versions", "LAYER_BASE"]
