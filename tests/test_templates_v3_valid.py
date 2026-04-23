"""Every shipped ``config/templates/*.yaml`` must load clean under v3.

Fast (no container/daemon) guard against regressions like the 2026-04-23
v1/v2 breakage: stale ``apiVersion``, stray ``metadata.name``, or
deprecated ``health.method: screen-alive`` would silently rot until a
user tried to instantiate the template. This test parametrizes over every
file in ``config/templates/`` and asserts ``load_config`` succeeds.
"""

from __future__ import annotations

from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
TEMPLATES_DIR = REPO_ROOT / "config" / "templates"


def _template_ids():
    return sorted(p.name for p in TEMPLATES_DIR.glob("*.yaml"))


@pytest.mark.parametrize("template_name", _template_ids())
def test_template_is_v3_valid(tmp_path, template_name):
    """Each shipped template must round-trip through the v3 loader cleanly."""
    from scitex_agent_container.config import load_config

    src = TEMPLATES_DIR / template_name
    # dir-as-SSoT: place the YAML at ``<name>/<name>.yaml`` so the loader
    # derives the agent name from the parent dir.
    agent_name = src.stem
    agent_dir = tmp_path / agent_name
    agent_dir.mkdir()
    target = agent_dir / f"{agent_name}.yaml"
    target.write_text(src.read_text())

    # Should not raise.
    cfg = load_config(target)
    assert cfg.name == agent_name
    # Sanity: v3 default for the new opt-in flag.
    assert cfg.container.mount_host_claude in (True, False)
    # Never resurrect the retired health method.
    assert cfg.health.method in ("multiplexer-alive",)
