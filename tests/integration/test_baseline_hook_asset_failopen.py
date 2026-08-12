"""A hook deploy must never cost an agent its channel, or its start.

These hooks sit on the send path of ``mcp__claude-code-telegrammer__reply`` —
the operator's only channel to the fleet. An agent left holding a half-written,
unparseable, or absent hook cannot send, and therefore cannot report that it
cannot send. So the deployer is required to fail OPEN: on any error the
previously deployed hook stays in place and the agent still boots.

The last class is the regression guard for the original defect. It drives the
real ``deploy_to_home`` start-path entrypoint end to end, so removing the
deployer call from it turns hook fixes inert again AND turns this test red.

PA-306 no-mocks: the broken-asset and missing-tree cases use REAL directories
with REAL bytes, injected through the deployer's ``assets_dir`` parameter,
rather than patching module internals.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from scitex_agent_container.config._types import AgentConfig
from scitex_agent_container.runtimes._baseline_hook_assets import (
    HOOKS_RELPATH,
    HOOKS_ROOT,
    baseline_assets_dir,
    deploy_baseline_hook_assets,
    fragment_hook_paths,
    iter_packaged_hook_assets,
)
from scitex_agent_container.runtimes._to_home import deploy_to_home

PROBE_HOOK = "enforce_telegram_no_bare_issue.sh"
TELEGRAM_TOOL = "mcp__claude-code-telegrammer__reply"
STALE_BYTES = b"#!/bin/bash\n# stale copy\nexit 0\n"


def packaged(name: str) -> Path:
    for asset in iter_packaged_hook_assets():
        if asset.name == name:
            return asset
    pytest.skip(f"sac no longer ships {name}")


def hooks_dir(home: Path) -> Path:
    return home / HOOKS_RELPATH


def run_hook(hook: Path, text: str) -> int:
    payload = json.dumps(
        {"tool_name": TELEGRAM_TOOL, "tool_input": {"chat_id": "1", "text": text}}
    )
    proc = subprocess.run(
        [str(hook)], input=payload.encode(), capture_output=True, timeout=30
    )
    return proc.returncode


@pytest.fixture
def stale_home(tmp_path: Path) -> Path:
    """An agent ``$HOME`` holding a working-but-stale deployed hook."""
    home = tmp_path / "home"
    d = hooks_dir(home)
    d.mkdir(parents=True)
    stale = d / PROBE_HOOK
    stale.write_bytes(STALE_BYTES)
    os.chmod(stale, 0o755)
    return home


@pytest.fixture
def unwritable_hooks_dir(stale_home: Path):
    """The hooks dir made read-only, restored on teardown."""
    d = hooks_dir(stale_home)
    os.chmod(d, 0o500)
    try:
        yield stale_home
    finally:
        os.chmod(d, 0o755)


@pytest.fixture
def broken_asset_tree(tmp_path: Path) -> Path:
    """A real asset tree whose one shell script genuinely does not parse."""
    family = tmp_path / "broken_assets" / "telegram_hooks"
    family.mkdir(parents=True)
    (family / PROBE_HOOK).write_text("#!/bin/bash\nif [ then fi done )(\n")
    return tmp_path / "broken_assets"


@pytest.fixture
def worktree_family() -> Path:
    """The one family that arms its hooks OUTSIDE ``pre-tool-use``."""
    family = baseline_assets_dir() / "claude_worktree_hooks"
    if not family.is_dir():
        pytest.skip("claude_worktree_hooks no longer shipped")
    return family


@pytest.fixture
def isolated_to_home_env(tmp_path: Path):
    """Pin the to_home cascade away from this host's real baselines.

    A yield fixture over ``os.environ`` (the sanctioned substitute for
    ``monkeypatch``): without it ``deploy_to_home`` would pull the operator's
    live ``~/.scitex/agent-container/agents/_shared/to_home`` into the test.
    """
    keys = ("SAC_USER_TO_HOME_BASELINE", "SAC_TO_HOME_BASELINE")
    saved = {k: os.environ.get(k) for k in keys}
    for k in keys:
        os.environ[k] = str(tmp_path / "nonexistent")
    try:
        yield
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


class TestFailOpenOnUnwritableDestination:
    def test_deploy_returns_instead_of_raising(self, unwritable_hooks_dir):
        # Arrange
        home = unwritable_hooks_dir
        # Act
        result = deploy_baseline_hook_assets(home)
        # Assert — reaching this line at all is the point (a raise fails the
        # test); the failure is RECORDED rather than thrown.
        assert PROBE_HOOK in result["failed"]

    def test_an_unwritable_dir_does_not_stop_other_dirs(self, unwritable_hooks_dir):
        # Arrange — claude_worktree_hooks deploys to its own writable subdir.
        home = unwritable_hooks_dir
        # Act
        result = deploy_baseline_hook_assets(home)
        # Assert — one jammed directory must not cost the whole fleet's hooks.
        assert result["deployed"] != []

    def test_previously_deployed_hook_is_untouched(self, unwritable_hooks_dir):
        # Arrange
        dst = hooks_dir(unwritable_hooks_dir) / PROBE_HOOK
        # Act
        deploy_baseline_hook_assets(unwritable_hooks_dir)
        # Assert — the old hook must survive a failed deploy intact.
        assert dst.read_bytes() == STALE_BYTES

    def test_previously_deployed_hook_is_still_executable(self, unwritable_hooks_dir):
        # Arrange
        dst = hooks_dir(unwritable_hooks_dir) / PROBE_HOOK
        # Act
        deploy_baseline_hook_assets(unwritable_hooks_dir)
        # Assert
        assert os.access(dst, os.X_OK) is True


class TestBrokenAssetIsRefused:
    def test_unparseable_asset_is_reported_as_failed(self, stale_home, broken_asset_tree):
        # Arrange
        home = stale_home
        # Act
        result = deploy_baseline_hook_assets(home, assets_dir=broken_asset_tree)
        # Assert
        assert PROBE_HOOK in result["failed"]

    def test_unparseable_asset_never_lands(self, stale_home, broken_asset_tree):
        # Arrange
        dst = hooks_dir(stale_home) / PROBE_HOOK
        # Act
        deploy_baseline_hook_assets(stale_home, assets_dir=broken_asset_tree)
        # Assert — the working hook keeps running; the bad one is refused.
        assert dst.read_bytes() == STALE_BYTES


class TestMissingAssetTreeIsSurvivable:
    def test_absent_tree_returns_an_empty_result(self, stale_home, tmp_path):
        # Arrange
        gone = tmp_path / "no-such-assets"
        # Act
        result = deploy_baseline_hook_assets(stale_home, assets_dir=gone)
        # Assert — a broken install must not stop an agent booting.
        assert result == {"deployed": [], "unchanged": [], "failed": []}

    def test_absent_tree_leaves_existing_hook_alone(self, stale_home, tmp_path):
        # Arrange
        dst = hooks_dir(stale_home) / PROBE_HOOK
        # Act
        deploy_baseline_hook_assets(stale_home, assets_dir=tmp_path / "no-such-assets")
        # Assert
        assert dst.read_bytes() == STALE_BYTES


class TestNoDebrisLeftBehind:
    def test_no_temp_files_remain_in_the_hooks_dir(self, stale_home):
        # Arrange
        d = hooks_dir(stale_home)
        # Act
        deploy_baseline_hook_assets(stale_home)
        # Assert — a stray temp file could be mistaken for a hook.
        assert [p.name for p in d.glob(".*sac-deploy*")] == []


class TestArmedHooksLandWhereTheyAreArmed:
    """The other half of the gap: registration and deployment must agree.

    A hook can be shipped, deployed, and still dead if it lands in a different
    directory from the one its settings fragment points at. Four families arm
    from ``pre-tool-use``; ``claude_worktree_hooks`` arms from its own subdir,
    so a deployer that flattened everything into ``pre-tool-use`` would arm
    three scripts at paths that do not exist.
    """

    def test_every_armed_command_is_deployed_to_that_exact_path(self, tmp_path):
        # Arrange
        home = tmp_path / "home"
        deploy_baseline_hook_assets(home)
        armed: list[tuple[str, Path]] = []
        for family in sorted(p for p in baseline_assets_dir().iterdir() if p.is_dir()):
            for name, subdir in fragment_hook_paths(family).items():
                if (family / name).exists():
                    armed.append((name, home / HOOKS_ROOT / subdir / name))
        # Act
        missing = [name for name, path in armed if not path.is_file()]
        # Assert
        assert missing == []

    def test_at_least_one_family_arms_outside_pre_tool_use(self):
        # Arrange — pins the fact that made the flat-deploy design wrong, so a
        # future simplification back to "everything in pre-tool-use" fails here.
        families = [p for p in baseline_assets_dir().iterdir() if p.is_dir()]
        # Act
        subdirs = {
            sub for f in families for sub in fragment_hook_paths(f).values()
        }
        # Assert
        assert subdirs - {"pre-tool-use"} != set()

    def test_worktree_scripts_do_not_land_in_pre_tool_use(
        self, tmp_path, worktree_family
    ):
        # Arrange
        home = tmp_path / "home"
        # Act
        deploy_baseline_hook_assets(home)
        # Assert
        stray = [
            n
            for n in fragment_hook_paths(worktree_family)
            if (home / HOOKS_ROOT / "pre-tool-use" / n).exists()
        ]
        assert stray == []


class TestDeployToHomeWiring:
    """The regression guard for the original defect.

    If the ``deploy_baseline_hook_assets`` call is removed from
    ``deploy_to_home``, merged hook fixes silently go inert again — exactly the
    bug this work closed — and these two tests go red.
    """

    def test_start_path_converges_a_stale_hook(
        self, tmp_path, isolated_to_home_env
    ):
        # Arrange
        agent_dir = tmp_path / "agent_def"
        (agent_dir / "to_home").mkdir(parents=True)
        cfg = AgentConfig(name="hook-deploy-probe")
        cfg.config_path = str(agent_dir / "spec.yaml")
        cfg.to_home = ""
        home = tmp_path / "home"
        d = hooks_dir(home)
        d.mkdir(parents=True)
        (d / PROBE_HOOK).write_bytes(STALE_BYTES)
        # Act — the real materialization entrypoint an agent start calls.
        deploy_to_home(cfg, str(home))
        # Assert
        assert (d / PROBE_HOOK).read_bytes() == packaged(PROBE_HOOK).read_bytes()

    def test_hook_delivered_by_the_start_path_actually_blocks(
        self, tmp_path, isolated_to_home_env
    ):
        # Arrange
        agent_dir = tmp_path / "agent_def"
        (agent_dir / "to_home").mkdir(parents=True)
        cfg = AgentConfig(name="hook-deploy-probe")
        cfg.config_path = str(agent_dir / "spec.yaml")
        cfg.to_home = ""
        home = tmp_path / "home"
        hooks_dir(home).mkdir(parents=True)
        # Act
        deploy_to_home(cfg, str(home))
        # Assert — rc 2 is the hook's BLOCK verdict on a bare issue number.
        assert run_hook(hooks_dir(home) / PROBE_HOOK, "#162") == 2
