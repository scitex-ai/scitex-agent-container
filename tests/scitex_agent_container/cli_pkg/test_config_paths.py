"""Neutral CLI path precedence uses real environments and owned files only."""

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from scitex_agent_container.cli_pkg.config_group import config_group


def layer(root, label):
    root.mkdir(parents=True)
    (root / "AGENTS.md").write_text(label + "\n")
    (root / "settings.yaml").write_text("model: " + label + "\n")
    return root


@pytest.fixture
def paths(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    runner = CliRunner(env={"HOME": str(home), "SAC_AGENTS_DIR": None,
                            "CLAUDE_CONFIG_DIR": None})
    with runner.isolated_filesystem(temp_dir=tmp_path):
        yield runner, home


def invoke(runner, args, **kwargs):
    result = runner.invoke(config_group, args, **kwargs)
    assert result.exit_code == 0, result.output
    return result.output


@pytest.mark.parametrize("selection", ["default", "environment", "explicit"])
def test_user_source_precedence_and_no_project_merge(paths, selection):
    runner, home = paths
    default = layer(home / ".scitex/agent-container/.agents", "user-default")
    override = layer(home / "override", "user-environment")
    explicit = layer(home / "explicit", "user-explicit")
    Path(".git").mkdir()
    project = layer(Path(".scitex/agent-container/.agents"), "project-decoy")
    (project / "PROJECT.md").write_text("must not merge\n")
    env = {"SAC_AGENTS_DIR": "~/override" if selection != "default" else None}
    flags = ["--source", str(explicit)] if selection == "explicit" else []
    selected = {"default": default, "environment": override,
                "explicit": explicit}[selection]
    summary = json.loads(invoke(runner, ["show", *flags, "--json"], env=env))
    assert Path(summary["root"]) == selected
    assert "PROJECT.md" not in summary["docs"]
    target = home / "native"
    invoke(runner, ["deploy", *flags, "--target", str(target), "--yes"], env=env)
    assert (target / "CLAUDE.md").read_text() == (selected / "AGENTS.md").read_text()
    settings = json.loads((target / "settings.json").read_text())
    assert settings["model"] == "user-" + selection
    assert not (Path.cwd() / ".claude").exists()


@pytest.mark.parametrize("selection", ["default", "environment", "explicit"])
def test_user_target_precedence_and_check_consumption(paths, selection):
    runner, home = paths
    source = layer(home / "source", "selected-model")
    env = {"CLAUDE_CONFIG_DIR": "~/native-env" if selection != "default" else None}
    default, override, explicit = [home / n for n in [".claude", "native-env", "native-arg"]]
    target = {"default": default, "environment": override,
              "explicit": explicit}[selection]
    flags = ["--source", str(source)]
    if selection == "explicit":
        flags += ["--target", str(explicit)]
    invoke(runner, ["deploy", *flags, "--yes"], env=env)
    assert (target / "CLAUDE.md").read_text() == "selected-model\n"
    for other in {default, override, explicit} - {target}:
        assert not other.exists()
    invoke(runner, ["check", *flags], env=env)
    (target / "settings.json").write_text('{"model": "drifted"}')
    result = runner.invoke(config_group, ["check", *flags], env=env)
    assert result.exit_code == 1
    assert str(target) in result.output


@pytest.mark.parametrize("selection", ["default", "environment", "explicit"])
def test_import_native_source_precedence(paths, selection):
    runner, home = paths
    default, override, explicit = [home / n for n in [".claude", "native-env", "native-arg"]]
    for root, label in [(default, "default"), (override, "environment"), (explicit, "explicit")]:
        root.mkdir()
        (root / "CLAUDE.md").write_text(label + "\n")
    env = {"CLAUDE_CONFIG_DIR": "~/native-env" if selection != "default" else None}
    flags = ["--claude-dir", str(explicit)] if selection == "explicit" else []
    dest = home / "imported"
    invoke(runner, ["import", *flags, "--to", str(dest), "--yes"], env=env)
    assert (dest / "AGENTS.md").read_text() == selection + "\n"
    for root, label in [(default, "default"), (override, "environment"), (explicit, "explicit")]:
        assert (root / "CLAUDE.md").read_text() == label + "\n"


@pytest.mark.parametrize("git_marker", ["directory", "worktree-file"])
def test_project_scope_uses_nearest_root_and_ignores_user_overrides(paths, git_marker):
    runner, home = paths
    root = Path.cwd()
    if git_marker == "directory":
        (root / ".git").mkdir()
    else:
        (root / ".git").write_text("gitdir: /not-consumed-by-path-reader\n")
    source = layer(root / ".scitex/agent-container/.agents", "project-model")
    user = layer(home / "user-decoy", "user-model")
    nested = root / "nested"
    nested.mkdir()
    env = {"SAC_AGENTS_DIR": str(user), "CLAUDE_CONFIG_DIR": str(home / "native-decoy")}
    # CliRunner changes the actual cwd; no path reader or command is mocked.
    with runner.isolated_filesystem(temp_dir=nested):
        summary = json.loads(invoke(runner, ["show", "--scope", "project", "--json"], env=env))
        assert Path(summary["root"]) == source
        invoke(runner, ["deploy", "--scope", "project", "--yes"], env=env)
        invoke(runner, ["check", "--scope", "project"], env=env)
    assert (root / ".claude/CLAUDE.md").read_text() == "project-model\n"
    assert not (home / "native-decoy").exists()
    assert (user / "AGENTS.md").read_text() == "user-model\n"
