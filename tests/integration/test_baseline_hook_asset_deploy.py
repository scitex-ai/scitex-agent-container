"""The packaged hook assets must REACH agents, and still work when they land.

Guards the defect these tests were written for: ``_baseline_assets/`` held the
canonical hook implementations, the deployed copies came from a hand-maintained
dotfiles mirror, and nothing connected the two — so merging a hook fix armed
nothing and no check said so.

Every assertion here is written to be FALSE before the fix. The ``stale_home``
fixture starts from a deployed copy that DIFFERS from the packaged asset (the
real measured state on 2026-08-12: a 601-byte-stale
``enforce_telegram_use_lists.sh``, three stale ``hpc_login_hooks`` files, and
three families never copied at all) and REFUSES to run if that precondition is
not actually false. A test that merely asserted "the hook is present" would
pass on a machine where it was already present and prove nothing.

PA-306 no-mocks: real files, real bytes, real subprocess execution of the
deployed scripts.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from scitex_agent_container.runtimes._baseline_hook_assets import (
    HOOKS_RELPATH,
    baseline_assets_dir,
    deploy_baseline_hook_assets,
    iter_packaged_hook_assets,
)

# A hook the fleet actually runs on the operator's only channel, and one of the
# hooks measured stale. Used as the representative asset throughout.
PROBE_HOOK = "enforce_telegram_no_bare_issue.sh"
TELEGRAM_TOOL = "mcp__claude-code-telegrammer__reply"

STALE_BYTES = b"#!/bin/bash\n# a stale copy that no longer matches the repo asset\nexit 0\n"


def packaged(name: str) -> Path:
    """The packaged asset called ``name`` (skips if sac stopped shipping it)."""
    for asset in iter_packaged_hook_assets():
        if asset.name == name:
            return asset
    pytest.skip(f"sac no longer ships {name}")


def hooks_dir(home: Path) -> Path:
    return home / HOOKS_RELPATH


def run_hook(hook: Path, text: str) -> int:
    """Execute ``hook`` with a real Claude-Code PreToolUse payload; return rc."""
    payload = json.dumps(
        {"tool_name": TELEGRAM_TOOL, "tool_input": {"chat_id": "1", "text": text}}
    )
    proc = subprocess.run(
        [str(hook)], input=payload.encode(), capture_output=True, timeout=30
    )
    return proc.returncode


@pytest.fixture
def stale_home(tmp_path: Path) -> Path:
    """An agent ``$HOME`` whose deployed hook is STALE — the false precondition.

    Fails the test outright if the planted copy is NOT different from the
    packaged asset: the whole point of this suite is to start from a state
    where the deployed bytes are wrong, and a fixture that quietly stopped
    doing that would make every test below vacuous.
    """
    home = tmp_path / "home"
    d = hooks_dir(home)
    d.mkdir(parents=True)
    stale = d / PROBE_HOOK
    stale.write_bytes(STALE_BYTES)
    os.chmod(stale, 0o755)
    if stale.read_bytes() == packaged(PROBE_HOOK).read_bytes():
        pytest.fail("precondition not false: planted 'stale' copy matches the asset")
    return home


@pytest.fixture
def converged_home(stale_home: Path) -> Path:
    """A stale home after one real deploy."""
    deploy_baseline_hook_assets(stale_home)
    return stale_home


class TestStaleCopyConverges:
    """The load-bearing behaviour: a DIFFERING deployed copy becomes correct."""

    def test_stale_deployed_copy_now_matches_the_packaged_asset(self, stale_home):
        # Arrange
        dst = hooks_dir(stale_home) / PROBE_HOOK
        # Act
        deploy_baseline_hook_assets(stale_home)
        # Assert
        assert dst.read_bytes() == packaged(PROBE_HOOK).read_bytes()

    def test_replacement_is_reported_as_deployed(self, stale_home):
        # Arrange
        expected = PROBE_HOOK
        # Act
        result = deploy_baseline_hook_assets(stale_home)
        # Assert
        assert expected in result["deployed"]

    def test_replacement_reports_no_failures(self, stale_home):
        # Arrange
        home = stale_home
        # Act
        result = deploy_baseline_hook_assets(home)
        # Assert
        assert result["failed"] == []

    def test_replaced_copy_is_displaced_not_deleted(self, converged_home):
        # Arrange
        attic = hooks_dir(converged_home) / ".old"
        # Act
        displaced = list(attic.glob(f"*/{PROBE_HOOK}"))
        # Assert
        assert [p.read_bytes() for p in displaced] == [STALE_BYTES]

    def test_hook_absent_entirely_is_created(self, tmp_path):
        # Arrange — nothing deployed at all (three families were in this state).
        home = tmp_path / "empty-home"
        # Act
        deploy_baseline_hook_assets(home)
        # Assert
        assert (hooks_dir(home) / PROBE_HOOK).read_bytes() == packaged(
            PROBE_HOOK
        ).read_bytes()

    def test_second_deploy_reports_unchanged(self, converged_home):
        # Arrange
        home = converged_home
        # Act
        result = deploy_baseline_hook_assets(home)
        # Assert
        assert PROBE_HOOK in result["unchanged"]

    def test_second_deploy_does_not_rewrite_the_file(self, converged_home):
        # Arrange
        dst = hooks_dir(converged_home) / PROBE_HOOK
        before = dst.stat().st_mtime_ns
        # Act
        deploy_baseline_hook_assets(converged_home)
        # Assert
        assert dst.stat().st_mtime_ns == before

    def test_second_deploy_displaces_nothing_further(self, converged_home):
        # Arrange
        attic = hooks_dir(converged_home) / ".old"
        # Act
        deploy_baseline_hook_assets(converged_home)
        # Assert
        assert len(list(attic.glob(f"*/{PROBE_HOOK}"))) == 1

    def test_repeatedly_restored_stale_copy_does_not_grow_the_attic(self, stale_home):
        # Arrange — reproduces the real steady state: the to_home walk re-copies
        # the operator's stale dotfiles version on EVERY start, so this module
        # replaces the same bytes again every start.
        dst = hooks_dir(stale_home) / PROBE_HOOK
        attic = hooks_dir(stale_home) / ".old"
        # Act — five "starts", each preceded by the walk restoring the stale copy.
        for _ in range(5):
            dst.write_bytes(STALE_BYTES)
            deploy_baseline_hook_assets(stale_home)
        # Assert — identical content is archived once, not once per start.
        assert len(list(attic.glob(f"*/{PROBE_HOOK}"))) == 1

    def test_a_genuinely_different_prior_version_is_still_kept(self, stale_home):
        # Arrange
        dst = hooks_dir(stale_home) / PROBE_HOOK
        attic = hooks_dir(stale_home) / ".old"
        deploy_baseline_hook_assets(stale_home)
        # Act — a DIFFERENT hand-edit appears, then is replaced in turn.
        dst.write_bytes(b"#!/bin/bash\n# a different local edit\nexit 0\n")
        deploy_baseline_hook_assets(stale_home)
        # Assert — dedupe must not cost a distinct version.
        assert len(list(attic.glob(f"*/{PROBE_HOOK}"))) == 2


class TestDeployedHookActuallyRuns:
    """Landing the bytes is only half the claim — the hook must still fire."""

    def test_deployed_hook_blocks_a_bare_issue_number(self, converged_home):
        # Arrange
        dst = hooks_dir(converged_home) / PROBE_HOOK
        # Act
        rc = run_hook(dst, "#162")
        # Assert — rc 2 is this hook's BLOCK verdict.
        assert rc == 2

    def test_deployed_hook_allows_a_compliant_message(self, converged_home):
        # Arrange
        dst = hooks_dir(converged_home) / PROBE_HOOK
        # Act
        rc = run_hook(dst, "fix #162 caption overlap")
        # Assert — the guard must not be a blanket denier.
        assert rc == 0

    def test_deployed_hook_is_executable(self, converged_home):
        # Arrange
        dst = hooks_dir(converged_home) / PROBE_HOOK
        # Act
        executable = os.access(dst, os.X_OK)
        # Assert — Claude Code execs these directly; a lost +x is a dead hook.
        assert executable is True

    def test_deployed_hook_passes_its_own_self_test(self, converged_home):
        # Arrange
        dst = hooks_dir(converged_home) / PROBE_HOOK
        # Act
        proc = subprocess.run([str(dst), "--self-test"], capture_output=True, timeout=60)
        # Assert
        assert proc.returncode == 0, proc.stdout.decode("utf-8", "replace")


class TestPackagedAssetQuality:
    def test_every_shipped_shell_asset_parses(self):
        # Arrange
        shell_assets = [a for a in iter_packaged_hook_assets() if a.suffix == ".sh"]
        # Act
        broken = [
            a.name
            for a in shell_assets
            if subprocess.run(["bash", "-n", str(a)], capture_output=True).returncode
        ]
        # Assert — sac must never ship a hook that cannot run.
        assert broken == []

    def test_asset_tree_ships_with_the_package(self):
        # Arrange
        root = baseline_assets_dir()
        # Act
        present = root.is_dir()
        # Assert — the whole mechanism rests on this tree being installed.
        assert present is True

    def test_package_ships_at_least_one_deployable_hook(self):
        # Arrange
        expected_nonempty = True
        # Act
        assets = iter_packaged_hook_assets()
        # Assert
        assert bool(assets) is expected_nonempty
