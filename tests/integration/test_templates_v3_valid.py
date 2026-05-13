"""Every shipped ``examples/agents/*/spec.yaml`` must load clean under v3.

``examples/agents/`` uses dir-as-SSoT layout: each subdirectory is an
agent example that users copy and customise.
"""

from __future__ import annotations

from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
EXAMPLES_DIR = REPO_ROOT / "examples" / "agents"


@pytest.mark.parametrize(
    "spec",
    sorted(EXAMPLES_DIR.glob("*/spec.yaml")),
    ids=lambda p: p.parent.name,
)
def test_example_loads(spec):
    from scitex_agent_container.config import load_config

    cfg = load_config(str(spec))
    assert cfg.name == spec.parent.name
    assert cfg.runtime == "apptainer"


def test_full_example_has_dot_claude():
    dot_claude = EXAMPLES_DIR / "full-agent" / "dot_claude"
    assert (dot_claude / "CLAUDE.md").exists()
    assert (dot_claude / ".mcp.json").exists()
    assert (dot_claude / ".env").exists()
    assert (dot_claude / "commands").is_dir()
    assert (dot_claude / "skills").is_dir()
    assert (dot_claude / "hooks").is_dir()


def test_minimal_example_has_no_dot_claude():
    assert not (EXAMPLES_DIR / "minimal-agent" / "dot_claude").exists()
