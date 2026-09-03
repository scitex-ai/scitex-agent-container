"""A real tmp fleet for the ADR-0024 scratch-migration suites.

Shared by ``_maintenance/test__scratch_migrate.py`` (planning) and
``_maintenance/test__scratch_migrate_apply.py`` (moving), so the two can
never disagree about what a fleet looks like — a planning test that passes
over a differently-shaped corpus than the apply test walks is worth very
little.

Nothing here is a mock: :func:`write_scratch_agent` writes a real, loadable
v3 spec and a real overlay upper with real files, and the real loader reads
it back. The liveness constants are the module's documented ``liveness``
seam, whose whole shape is ``(running, detail)`` — a test cannot make a real
agent run, and the thing under test is the plan's arithmetic over that
answer, not the probe.
"""

from __future__ import annotations

from pathlib import Path

import yaml

from tests.scitex_agent_container._helpers.explicit_spec import explicit_doc

#: Liveness seams — ``(running, detail)``, exactly the adapter's answer shape.
STOPPED = lambda config: (False, "ApptainerRuntime")  # noqa: E731
RUNNING = lambda config: (True, "ApptainerRuntime")  # noqa: E731
UNKNOWN = lambda config: (None, "ApptainerRuntime.is_running: boom")  # noqa: E731

__all__ = ["RUNNING", "STOPPED", "UNKNOWN", "row_for", "write_scratch_agent"]


def write_scratch_agent(
    agents_dir: Path,
    name: str,
    *,
    uvwork: dict[str, str] | None = None,
    overlay: bool = True,
    overlay_size: str = "",
    overlay_dir: Path | None = None,
) -> Path:
    """A real spec plus (optionally) a real overlay upper with real files.

    ``overlay_dir`` points the spec at a SHARED overlay — the shape eight
    ``handyman-*`` specs and the ``scitex-hub`` pair are really in on the
    fleet. ``overlay_size`` makes it a loopback IMAGE overlay, whose upper
    the host cannot walk. Returns the overlay's ``upper/uvwork`` path — the
    tree the sweep would move.
    """
    agent_dir = agents_dir / name
    agent_dir.mkdir(parents=True)
    overlay_root = agent_dir / "overlay" if overlay_dir is None else overlay_dir
    ap: dict = {"image": "/x.sif", "binds": []}
    if overlay:
        ap["overlay"] = str(overlay_root)
    if overlay_size:
        ap["overlay_size"] = overlay_size
    doc = explicit_doc({"runtime": "tui", "workdir": str(agent_dir), "apptainer": ap})
    (agent_dir / "spec.yaml").write_text(yaml.safe_dump(doc, sort_keys=False))
    source = overlay_root / "upper" / "uvwork"
    if uvwork is not None:
        source.mkdir(parents=True, exist_ok=True)
        for rel, text in uvwork.items():
            path = source / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
    return source


def row_for(plan, agent: str):
    """The one row ``plan`` holds for ``agent``; raises if it holds none."""
    return next(r for r in plan.rows if r.agent == agent)
