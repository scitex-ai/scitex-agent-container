"""Every shipped ``config/templates/*.yaml`` and ``config/examples/*.yaml``
must load clean under v3.

Templates live in two places:
  * ``config/templates/`` — minimal pattern templates (one per pattern:
    docker, apptainer, ssh, mcp). These are the starting points users
    copy from. F-CS17 deleted the ``local`` / ``claude-session`` /
    ``ssh-slurm`` patterns: bare-metal and SLURM scheduling are no
    longer supported (sac is a container wrapper; HPC scheduling
    is the operator's concern).
  * ``config/examples/`` — concrete real-world configs (newbie-docker,
    researcher-opus). These document specific operator decisions.

Both must round-trip through ``load_config`` cleanly.
"""

from __future__ import annotations

from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
TEMPLATES_DIR = REPO_ROOT / "config" / "templates"
EXAMPLES_DIR = REPO_ROOT / "config" / "examples"


def _instantiate(src: Path, parent: Path):
    """Copy a flat template/example into a dir-as-SSoT layout."""
    agent_name = src.stem
    agent_dir = parent / agent_name
    agent_dir.mkdir()
    target = agent_dir / f"{agent_name}.yaml"
    target.write_text(src.read_text())
    return target, agent_name


# ---------------------------------------------------------------------------
# Generic load-clean coverage
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "src",
    sorted(TEMPLATES_DIR.glob("*.yaml")),
    ids=lambda p: f"template/{p.name}",
)
def test_template_loads(tmp_path, src):
    from scitex_agent_container.config import load_config

    target, agent_name = _instantiate(src, tmp_path)
    cfg = load_config(target)
    assert cfg.name == agent_name
    assert cfg.health.method == "sdk-alive"


@pytest.mark.parametrize(
    "src",
    sorted(EXAMPLES_DIR.glob("*.yaml")),
    ids=lambda p: f"example/{p.name}",
)
def test_example_loads(tmp_path, src):
    from scitex_agent_container.config import load_config

    target, agent_name = _instantiate(src, tmp_path)
    cfg = load_config(target)
    assert cfg.name == agent_name
    assert cfg.health.method == "sdk-alive"


# ---------------------------------------------------------------------------
# Pattern coverage — each minimal template demonstrates a distinct pattern.
# F-CS17: bare-metal local / claude-session / ssh-slurm patterns deleted.
# ---------------------------------------------------------------------------


def test_minimal_templates_cover_expected_patterns():
    """Catch accidental deletion / pattern drift in templates/."""
    expected = {
        "docker.yaml",
        "apptainer.yaml",
        "ssh.yaml",
        "mcp.yaml",
    }
    actual = {p.name for p in TEMPLATES_DIR.glob("*.yaml")}
    assert actual == expected, f"template set drifted: {actual ^ expected}"


# ---------------------------------------------------------------------------
# Runtime-specific assertions — guard against YAML/dataclass drift
# ---------------------------------------------------------------------------


def test_docker_template_uses_docker_runtime(tmp_path):
    from scitex_agent_container.config import load_config

    target, _ = _instantiate(TEMPLATES_DIR / "docker.yaml", tmp_path)
    cfg = load_config(target)
    assert cfg.runtime == "docker"
    assert cfg.image  # spec.image is non-empty (F-CS16 phase 2a)
    assert "sdk-persistent" in cfg.image
    assert cfg.dockerfile  # auto-build target declared


def test_apptainer_template_uses_apptainer_runtime(tmp_path):
    from scitex_agent_container.config import load_config

    target, _ = _instantiate(TEMPLATES_DIR / "apptainer.yaml", tmp_path)
    cfg = load_config(target)
    assert cfg.runtime == "apptainer"
    # Apptainer images are .sif files; the template ships a sample path.
    assert cfg.image.endswith(".sif")


def test_ssh_template_loads_as_docker_runtime(tmp_path):
    """F-CS17: the ``ssh`` pattern is no longer about sac-side SSH
    dispatch — that's done by ``sac --on <peer>`` (F-CS12). This
    template just shows what an agent yaml on a remote host looks
    like; the engine is plain docker."""
    from scitex_agent_container.config import load_config

    target, _ = _instantiate(TEMPLATES_DIR / "ssh.yaml", tmp_path)
    cfg = load_config(target)
    assert cfg.runtime == "docker"


def test_mcp_template_has_server_entry(tmp_path):
    from scitex_agent_container.config import load_config

    target, _ = _instantiate(TEMPLATES_DIR / "mcp.yaml", tmp_path)
    cfg = load_config(target)
    assert "example-server" in cfg.mcp_servers
    server = cfg.mcp_servers["example-server"]
    # mcp_servers is parsed as dict[str, dict] — check the raw shape.
    assert server.get("type") == "stdio"
    assert server.get("command") == "bun"
