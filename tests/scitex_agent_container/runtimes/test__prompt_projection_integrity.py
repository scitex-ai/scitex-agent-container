from __future__ import annotations

import json
from pathlib import Path

import pytest

from scitex_agent_container.config._types import AgentConfig
from scitex_agent_container.runtimes._prompt_projection_integrity import (
    PromptProjectionDriftError,
    capture_prompt_sources,
    resolve_hermes_instruction_projection,
    verify_prompt_projection_manifest,
    write_prompt_projection_manifest,
)
from scitex_agent_container.runtimes._to_home import deploy_to_home


def _config(tmp_path: Path) -> tuple[AgentConfig, Path, Path]:
    agents = tmp_path / "agents"
    shared = agents / "_shared" / "to_home"
    own = agents / "demo" / "to_home"
    shared.mkdir(parents=True)
    own.mkdir(parents=True)
    config = AgentConfig(name="demo")
    config.config_path = str(agents / "demo" / "spec.yaml")
    config.to_home = "./to_home"
    config.to_home_layers = ["user-shared", "per-agent"]
    config.startup_prompts = ["Continue the assigned work."]
    return config, shared, own


def test_manifest_records_source_and_runtime_hashes(tmp_path, env_save_restore):
    # Arrange
    config, shared, own = _config(tmp_path)
    env_save_restore.set("SAC_USER_TO_HOME_BASELINE", str(shared))
    (shared / "AGENTS.md").write_text("shared\n")
    skill = own / ".agents" / "skills" / "demo"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("skill\n")
    home = tmp_path / "home"
    (home / ".agents" / "skills" / "demo").mkdir(parents=True)
    (home / "AGENTS.md").write_text("shared\n")
    (home / ".agents" / "skills" / "demo" / "SKILL.md").write_text("skill\n")

    # Act
    before = capture_prompt_sources(config)
    manifest_path = write_prompt_projection_manifest(config, home, before)

    # Assert
    payload = json.loads(manifest_path.read_text())
    observed = {
        "schema": payload["schema"],
        "startup_hash_present": bool(payload["startup_prompts"][0]["sha256"]),
        "source_layers": {item["layer"] for item in payload["sources"]},
        "verified": verify_prompt_projection_manifest(config, home) == payload,
    }
    assert observed == {
        "schema": "scitex-agent-container/prompt-projections/v1",
        "startup_hash_present": True,
        "source_layers": {"user-shared", "per-agent"},
        "verified": True,
    }


def test_source_change_after_capture_fails_loud(tmp_path, env_save_restore):
    # Arrange
    config, shared, _ = _config(tmp_path)
    env_save_restore.set("SAC_USER_TO_HOME_BASELINE", str(shared))
    source = shared / "AGENTS.md"
    source.write_text("before\n")
    before = capture_prompt_sources(config)
    source.write_text("after\n")

    # Act
    def action():
        return write_prompt_projection_manifest(config, tmp_path / "home", before)

    # Assert
    with pytest.raises(PromptProjectionDriftError, match="changed during"):
        action()


def test_runtime_projection_mutation_fails_verification(tmp_path, env_save_restore):
    # Arrange
    config, shared, _ = _config(tmp_path)
    env_save_restore.set("SAC_USER_TO_HOME_BASELINE", str(shared))
    (shared / "AGENTS.md").write_text("source\n")
    home = tmp_path / "home"
    home.mkdir()
    projected = home / "AGENTS.md"
    projected.write_text("projected\n")
    write_prompt_projection_manifest(config, home, capture_prompt_sources(config))
    projected.write_text("mutated\n")

    # Act
    def action():
        return verify_prompt_projection_manifest(config, home)

    # Assert
    with pytest.raises(PromptProjectionDriftError, match="runtime projection"):
        action()


def test_declared_layers_control_prompt_sources(tmp_path, env_save_restore):
    # Arrange
    config, shared, own = _config(tmp_path)
    env_save_restore.set("SAC_USER_TO_HOME_BASELINE", str(shared))
    config.to_home_layers = ["user-shared"]
    (shared / "AGENTS.md").write_text("shared\n")
    (own / "AGENTS.md").write_text("must not contribute\n")

    # Act
    records = capture_prompt_sources(config)

    # Assert
    assert [(item["layer"], item["relative_path"]) for item in records] == [
        ("user-shared", "AGENTS.md")
    ]


def test_hermes_consumes_verified_neutral_projection(tmp_path, env_save_restore):
    # Arrange
    config, shared, _ = _config(tmp_path)
    config.harness = "hermes"
    env_save_restore.set("SAC_USER_TO_HOME_BASELINE", str(shared))
    (shared / "AGENTS.md").write_text("authoritative instructions\n")
    home = tmp_path / "home"
    home.mkdir()
    (home / "AGENTS.md").write_text("authoritative instructions\n")
    write_prompt_projection_manifest(config, home, capture_prompt_sources(config))

    # Act
    text, identity = resolve_hermes_instruction_projection(config, home)

    # Assert
    assert (text, identity["projection"], identity["transport"]) == (
        "authoritative instructions\n",
        "AGENTS.md",
        "agent.system_prompt",
    )


def test_hermes_refuses_implicit_claude_translation(tmp_path, env_save_restore):
    # Arrange
    config, shared, _ = _config(tmp_path)
    config.harness = "hermes"
    env_save_restore.set("SAC_USER_TO_HOME_BASELINE", str(shared))
    claude_dir = shared / ".claude"
    claude_dir.mkdir()
    (claude_dir / "CLAUDE.md").write_text("legacy instructions\n")
    home = tmp_path / "home"
    (home / ".claude").mkdir(parents=True)
    (home / ".claude" / "CLAUDE.md").write_text("legacy instructions\n")
    write_prompt_projection_manifest(config, home, capture_prompt_sources(config))

    # Act
    def action():
        return resolve_hermes_instruction_projection(config, home)

    # Assert
    with pytest.raises(
        PromptProjectionDriftError,
        match="will not silently translate",
    ):
        action()


def test_materializer_publishes_exact_hermes_projection(tmp_path, env_save_restore):
    # Arrange
    config, shared, _ = _config(tmp_path)
    config.harness = "hermes"
    config.to_home_layers = ["user-shared"]
    env_save_restore.set("SAC_USER_TO_HOME_BASELINE", str(shared))
    (shared / "AGENTS.md").write_text("current instructions\n")
    home = tmp_path / "home"
    home.mkdir()
    (home / "AGENTS.md").write_text("stale operator tail\n")

    # Act
    deploy_to_home(config, str(home))
    text, _identity = resolve_hermes_instruction_projection(config, home)

    # Assert
    assert text == "current instructions\n"


def test_walk_never_escapes_root_through_symlink(tmp_path):
    """A symlink pointing outside the tree must not be descended into."""
    from scitex_agent_container.runtimes._prompt_projection_integrity import (
        _walk_prompt_files,
    )

    outside = tmp_path / "outside"
    (outside / "skills").mkdir(parents=True)
    (outside / "skills" / "note.md").write_text("x")
    root = tmp_path / "home"
    root.mkdir()
    (root / "AGENTS.md").write_text("hi")
    (root / "proj").symlink_to(tmp_path, target_is_directory=True)
    found = [rel.as_posix() for rel, _ in _walk_prompt_files(root)]
    assert found == ["AGENTS.md"]
