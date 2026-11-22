"""Owned path inputs for config_group CLI behavior tests; no mocked readers."""

from pathlib import Path
from types import SimpleNamespace

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


@pytest.fixture(params=["default", "environment", "explicit"])
def source_case(paths, request):
    runner, home = paths
    selected = request.param
    roots = {name: layer(home / name, "user-" + name)
             for name in ["environment", "explicit"]}
    roots["default"] = layer(home / ".scitex/agent-container/.agents", "user-default")
    Path(".git").mkdir()
    project = layer(Path(".scitex/agent-container/.agents"), "project-decoy")
    (project / "PROJECT.md").write_text("must not merge\n")
    env = {"SAC_AGENTS_DIR": "~/environment" if selected != "default" else None}
    flags = ["--source", str(roots["explicit"])] if selected == "explicit" else []
    return SimpleNamespace(runner=runner, env=env, flags=flags, root=roots[selected],
                           label="user-" + selected, target=home / "native")


@pytest.fixture(params=["default", "environment", "explicit"])
def native_case(paths, request):
    runner, home = paths
    selected = request.param
    roots = {"default": home / ".claude", "environment": home / "native-env",
             "explicit": home / "native-arg"}
    env = {"CLAUDE_CONFIG_DIR": "~/native-env" if selected != "default" else None}
    flags = ["--target", str(roots["explicit"])] if selected == "explicit" else []
    return SimpleNamespace(runner=runner, env=env, flags=flags, target=roots[selected],
                           roots=roots, home=home, selection=selected)


@pytest.fixture(params=["directory", "worktree-file"])
def project_case(paths, request):
    runner, home = paths
    root = Path.cwd()
    if request.param == "directory":
        (root / ".git").mkdir()
    else:
        (root / ".git").write_text("gitdir: /not-consumed-by-path-reader\n")
    source = layer(root / ".scitex/agent-container/.agents", "project-model")
    user = layer(home / "user-decoy", "user-model")
    nested = root / "nested"
    nested.mkdir()
    env = {"SAC_AGENTS_DIR": str(user), "CLAUDE_CONFIG_DIR": str(home / "native-decoy")}
    with runner.isolated_filesystem(temp_dir=nested):
        yield SimpleNamespace(runner=runner, env=env, source=source, user=user,
                              target=root / ".claude", decoy=home / "native-decoy")


def command(case, verb, *flags):
    return case.runner.invoke(config_group, [verb, *flags], env=case.env)
