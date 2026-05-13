"""Every shipped ``examples/agent-specs/*.yaml`` must load clean under v3.

``examples/agent-specs/`` contains annotated example configs that users
copy and customise. All must round-trip through ``load_config`` cleanly.
"""

from __future__ import annotations

from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
EXAMPLES_DIR = REPO_ROOT / "examples" / "agent-specs"


def _instantiate(src: Path, parent: Path):
    """Copy a flat example into a dir-as-SSoT layout."""
    agent_name = src.stem
    agent_dir = parent / agent_name
    agent_dir.mkdir()
    target = agent_dir / f"{agent_name}.yaml"
    target.write_text(src.read_text())
    return target, agent_name


@pytest.mark.parametrize(
    "src",
    sorted(EXAMPLES_DIR.glob("*.yaml")),
    ids=lambda p: p.name,
)
def test_example_loads(tmp_path, src):
    from scitex_agent_container.config import load_config

    target, agent_name = _instantiate(src, tmp_path)
    cfg = load_config(target)
    assert cfg.name == agent_name
    assert cfg.runtime == "apptainer"


def test_apptainer_example_uses_apptainer_runtime(tmp_path):
    src = EXAMPLES_DIR / "apptainer.yaml"
    target, _ = _instantiate(src, tmp_path)
    from scitex_agent_container.config import load_config

    cfg = load_config(target)
    assert cfg.runtime == "apptainer"
    assert cfg.image.endswith(".sif")


def test_minimal_example_loads(tmp_path):
    src = EXAMPLES_DIR / "minimal.yaml"
    target, _ = _instantiate(src, tmp_path)
    from scitex_agent_container.config import load_config

    cfg = load_config(target)
    assert cfg.runtime == "apptainer"
    assert cfg.image.endswith(".sif")
