"""Every shipped ``config/templates/*.yaml`` and ``config/examples/*.yaml``
must load clean under v3.

Templates live in two places:
  * ``config/templates/`` — minimal pattern templates (one per pattern:
    local, docker, apptainer, ssh, ssh-slurm, mcp). These are the
    starting points users copy from.
  * ``config/examples/`` — concrete real-world configs (newbie-docker,
    researcher-opus). These document specific operator decisions.

Both must round-trip through ``load_config`` cleanly. The SLURM and
MCP templates additionally exercise runtime-specific rendering paths
so YAML-key drift from ``SlurmSpec`` / ``McpServer`` dataclasses fails
loudly here, not at the user's first ``sac start``.
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
    assert cfg.health.method == "multiplexer-alive"
    assert cfg.container.mount_host_claude in (True, False)


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
    assert cfg.health.method == "multiplexer-alive"


# ---------------------------------------------------------------------------
# Pattern coverage — each minimal template demonstrates a distinct pattern
# ---------------------------------------------------------------------------


def test_minimal_templates_cover_expected_patterns():
    """Catch accidental deletion / pattern drift in templates/."""
    expected = {
        "local.yaml",
        "docker.yaml",
        "apptainer.yaml",
        "ssh.yaml",
        "ssh-slurm.yaml",
        "mcp.yaml",
        "claude-session.yaml",
    }
    actual = {p.name for p in TEMPLATES_DIR.glob("*.yaml")}
    assert actual == expected, f"template set drifted: {actual ^ expected}"


# ---------------------------------------------------------------------------
# Runtime-specific assertions — guard against YAML/dataclass drift
# ---------------------------------------------------------------------------


def test_local_template_is_bare_metal(tmp_path):
    from scitex_agent_container.config import load_config

    target, _ = _instantiate(TEMPLATES_DIR / "local.yaml", tmp_path)
    cfg = load_config(target)
    assert cfg.runtime == "claude-code"
    assert cfg.container.runtime == "none"
    assert cfg.remote.is_remote is False


def test_docker_template_uses_docker_runtime(tmp_path):
    from scitex_agent_container.config import load_config

    target, _ = _instantiate(TEMPLATES_DIR / "docker.yaml", tmp_path)
    cfg = load_config(target)
    assert cfg.container.runtime == "docker"
    assert cfg.container.mount_host_claude is False  # safe default


def test_apptainer_template_uses_apptainer_runtime(tmp_path):
    from scitex_agent_container.config import load_config

    target, _ = _instantiate(TEMPLATES_DIR / "apptainer.yaml", tmp_path)
    cfg = load_config(target)
    assert cfg.container.runtime == "apptainer"
    assert cfg.container.image.endswith(".sif")


def test_ssh_template_has_remote_block(tmp_path):
    from scitex_agent_container.config import load_config

    target, _ = _instantiate(TEMPLATES_DIR / "ssh.yaml", tmp_path)
    cfg = load_config(target)
    assert cfg.remote.is_remote is True
    assert cfg.remote.host == "example.host"
    assert cfg.remote.login_shell is True


def test_ssh_slurm_template_renders_sbatch_script(tmp_path):
    """SLURM template must round-trip into a valid sbatch script —
    catches YAML-key drift from SlurmSpec / SlurmHooks."""
    from scitex_agent_container.config import load_config
    from scitex_agent_container.runtimes.slurm import (
        REQUIRED_SHEBANG,
        REQUIRED_STRICT_MODE,
        REQUIRED_USR1_TRAP_MARKER,
        render_sbatch_script,
    )

    target, agent_name = _instantiate(TEMPLATES_DIR / "ssh-slurm.yaml", tmp_path)
    cfg = load_config(target)

    assert cfg.runtime == "slurm"
    assert cfg.slurm.auto_resubmit is True
    assert cfg.slurm.signal == "B:USR1@3600"

    script = render_sbatch_script(cfg)
    assert script.startswith(REQUIRED_SHEBANG)
    assert REQUIRED_STRICT_MODE in script
    assert REQUIRED_USR1_TRAP_MARKER in script
    assert f"#SBATCH --job-name={agent_name}" in script


def test_mcp_template_has_server_entry(tmp_path):
    from scitex_agent_container.config import load_config

    target, _ = _instantiate(TEMPLATES_DIR / "mcp.yaml", tmp_path)
    cfg = load_config(target)
    assert "example-server" in cfg.mcp_servers
    server = cfg.mcp_servers["example-server"]
    # mcp_servers is parsed as dict[str, dict] — check the raw shape.
    assert server.get("type") == "stdio"
    assert server.get("command") == "bun"
