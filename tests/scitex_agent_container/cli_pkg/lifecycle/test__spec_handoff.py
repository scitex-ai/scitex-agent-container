#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""The spec handoff must PROVE delivery, not infer it from an exit code.

Pinned here because the defect that motivated the module is invisible to
every other kind of check: on ``scitex-nas-03`` the transfer exits 0, prints
a normal file list, and writes the spec one directory level away from the
requested path (measured 2026-08-15). The remote ``sac agents start`` then
boots the agent from the stale spec still sitting at the real path, and the
dispatch reports success.

Nothing here is patched. The peers below are real POSIX shells running the
production scripts against real directories; :class:`ReroutingPeer` is a real
shell too, wired the way the NAS is wired — writes land somewhere other than
the path that was asked for. That is the whole seam the module exposes.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from scitex_agent_container.cli_pkg.lifecycle._spec_handoff import (
    EXCLUDED_NAMES,
    local_manifest,
    manifest_script,
    parse_manifest,
    plan_handoff,
    push_spec_dir,
    read_remote_manifest,
)

#: The destination the production code hands to the peer. A real dispatch
#: sends ``$HOME/.scitex/...``; the peer's shell expands it. These tests send
#: ``$SPEC_DIR`` and let the peer below define it — same mechanism, so the
#: shell-expansion half of the contract is exercised rather than assumed.
REMOTE_DIR = "$SPEC_DIR"


class HonestPeer:
    """A peer whose shell writes exactly where it is told."""

    def __init__(self, spec_dir: Path) -> None:
        self.spec_dir = spec_dir

    def _env(self, root: Path) -> dict[str, str]:
        return {**os.environ, "SPEC_DIR": str(root)}

    def __call__(self, script: str, stdin: bytes | None = None):
        return subprocess.run(
            ["sh", "-c", script],
            input=stdin if stdin is not None else b"",
            capture_output=True,
            check=False,
            env=self._env(self.spec_dir),
        )


class ReroutingPeer(HonestPeer):
    """A peer whose TRANSPORT re-roots writes, exactly like scitex-nas-03.

    Its extraction succeeds — rc 0, no diagnostics — into ``writes_to``,
    while anything that reads the spec dir still reads the path that was
    requested. That is the shape that made a stale spec look like a fresh
    deployment.
    """

    def __init__(self, spec_dir: Path, writes_to: Path) -> None:
        super().__init__(spec_dir)
        self.writes_to = writes_to

    def __call__(self, script: str, stdin: bytes | None = None):
        root = self.writes_to if "tar -C" in script else self.spec_dir
        return subprocess.run(
            ["sh", "-c", script],
            input=stdin if stdin is not None else b"",
            capture_output=True,
            check=False,
            env=self._env(root),
        )


class UnreachablePeer:
    """A peer whose shell fails — the loud half of the contract."""

    def __call__(self, script: str, stdin: bytes | None = None):
        return subprocess.CompletedProcess(
            args=[], returncode=11, stdout=b"", stderr=b"mkdir failed: No such file"
        )


@pytest.fixture
def rerouted_delivery_error(tmp_path, spec_src):
    """The message raised when a peer re-roots the write, captured once.

    Lifted into a fixture so each claim about that message is its own test
    with a single assertion.
    """
    peer = ReroutingPeer(tmp_path / "peer" / "asked-for", tmp_path / "peer" / "actual")
    try:
        push_spec_dir(spec_src, REMOTE_DIR, peer, peer="scitex-nas-03")
    except RuntimeError as exc:
        return str(exc)
    raise AssertionError("a re-rooted delivery must not be reported as success")


@pytest.fixture
def spec_src(tmp_path):
    """A lead-side spec dir with a nested file and excluded runtime state."""
    root = tmp_path / "lead" / "scitex-hub"
    (root / "to_home").mkdir(parents=True)
    (root / "runtime").mkdir(parents=True)
    (root / "spec.yaml").write_text("host: scitex-nas-03\n")
    (root / "to_home" / "CLAUDE.md").write_text("house rules\n")
    (root / "runtime" / "state.db").write_text("peer-side state\n")
    return root


# --------------------------------------------------------------------------
# local_manifest — the lead's half
# --------------------------------------------------------------------------


def test_the_manifest_lists_every_shipped_file(spec_src):
    # Arrange
    expected = ["spec.yaml", "to_home/CLAUDE.md"]
    # Act
    manifest = local_manifest(spec_src)
    # Assert
    assert sorted(manifest) == expected


def test_the_manifest_agrees_with_md5sum_byte_for_byte(spec_src):
    # Arrange
    proc = subprocess.run(
        ["md5sum", str(spec_src / "spec.yaml")],
        capture_output=True,
        text=True,
        check=True,
    )
    # Act
    digest = local_manifest(spec_src)["spec.yaml"]
    # Assert
    assert digest == proc.stdout.split()[0]


@pytest.mark.parametrize("excluded", EXCLUDED_NAMES)
def test_runtime_state_and_caches_are_never_shipped(tmp_path, excluded):
    # Arrange
    root = tmp_path / "scitex-hub"
    (root / excluded).mkdir(parents=True)
    (root / "spec.yaml").write_text("a")
    (root / excluded / "junk").write_text("b")
    # Act
    manifest = local_manifest(root)
    # Assert
    assert sorted(manifest) == ["spec.yaml"]


def test_an_excluded_directory_is_pruned_at_any_depth(tmp_path):
    # Arrange
    root = tmp_path / "scitex-hub"
    (root / "to_home" / "__pycache__").mkdir(parents=True)
    (root / "spec.yaml").write_text("a")
    (root / "to_home" / "__pycache__" / "x.pyc").write_text("b")
    # Act
    manifest = local_manifest(root)
    # Assert
    assert sorted(manifest) == ["spec.yaml"]


def test_a_symlink_is_absent_from_the_manifest(spec_src):
    """``find -type f`` on the peer skips symlinks, so this side must too, or
    every spec dir holding one would verify as mis-delivered."""
    # Arrange
    (spec_src / "link.yaml").symlink_to(spec_src / "spec.yaml")
    # Act
    manifest = local_manifest(spec_src)
    # Assert
    assert "link.yaml" not in manifest


# --------------------------------------------------------------------------
# manifest_script / parse_manifest — the peer's half
# --------------------------------------------------------------------------


def test_md5sum_output_parses_into_relative_paths():
    # Arrange
    stdout = "d41d8cd98f00b204e9800998ecf8427e  ./to_home/CLAUDE.md\n"
    # Act
    parsed = parse_manifest(stdout)
    # Assert
    assert parsed == {"to_home/CLAUDE.md": "d41d8cd98f00b204e9800998ecf8427e"}


def test_the_peers_manifest_matches_the_leads_for_the_same_tree(spec_src):
    """The two halves must agree on keys AND digests, or verification would
    fail on a delivery that was in fact perfect."""
    # Arrange
    peer = HonestPeer(spec_src)
    # Act
    reported = read_remote_manifest(REMOTE_DIR, peer)
    # Assert
    assert reported == local_manifest(spec_src)


def test_an_absent_spec_dir_reads_as_an_empty_manifest(tmp_path):
    """A peer that has never seen this agent is a first launch, not a fault."""
    # Arrange
    peer = HonestPeer(tmp_path / "never-created")
    # Act
    reported = read_remote_manifest(REMOTE_DIR, peer)
    # Assert
    assert reported == {}


def test_a_peer_that_cannot_be_read_is_an_error():
    # Arrange
    peer = UnreachablePeer()
    # Act
    # Assert
    with pytest.raises(RuntimeError, match="Could not read the spec manifest"):
        read_remote_manifest(REMOTE_DIR, peer)


def test_the_manifest_script_prunes_excluded_directories(spec_src):
    # Arrange
    script = manifest_script(str(spec_src))
    # Act
    proc = subprocess.run(
        ["sh", "-c", script], capture_output=True, text=True, check=True
    )
    # Assert
    assert "runtime/state.db" not in parse_manifest(proc.stdout)


# --------------------------------------------------------------------------
# plan_handoff
# --------------------------------------------------------------------------


def test_an_empty_peer_is_a_first_launch():
    # Arrange
    local = {"spec.yaml": "aa"}
    # Act
    plan = plan_handoff(local, {})
    # Assert
    assert plan.first_launch is True


def test_an_empty_peer_lists_every_file_as_new():
    # Arrange
    local = {"spec.yaml": "aa"}
    # Act
    plan = plan_handoff(local, {})
    # Assert
    assert plan.new == ("spec.yaml",)


def test_a_differing_shared_file_is_drift():
    # Arrange
    local = {"spec.yaml": "aa"}
    # Act
    plan = plan_handoff(local, {"spec.yaml": "bb"})
    # Assert
    assert plan.drift is True


def test_a_differing_shared_file_is_not_a_first_launch():
    # Arrange
    local = {"spec.yaml": "aa"}
    # Act
    plan = plan_handoff(local, {"spec.yaml": "bb"})
    # Assert
    assert plan.first_launch is False


def test_an_identical_peer_plans_no_change():
    # Arrange
    local = {"spec.yaml": "aa"}
    # Act
    plan = plan_handoff(local, {"spec.yaml": "aa"})
    # Assert
    assert (plan.new, plan.changed, plan.extra) == ((), (), ())


def test_a_peer_only_file_is_reported_as_extra():
    # Arrange
    local = {"spec.yaml": "aa"}
    # Act
    plan = plan_handoff(local, {"spec.yaml": "aa", "start-sidecar.sh": "cc"})
    # Assert
    assert plan.extra == ("start-sidecar.sh",)


def test_a_peer_only_file_is_not_drift():
    """It used to be, because rsync announced the deletion it planned. The
    handoff no longer deletes, so a file only the peer has must not block a
    start — losing scitex-nas-03's start-telegram-sidecar.sh to a mirroring
    delete is the outcome that rule exists to prevent."""
    # Arrange
    local = {"spec.yaml": "aa"}
    # Act
    plan = plan_handoff(local, {"spec.yaml": "aa", "start-sidecar.sh": "cc"})
    # Assert
    assert plan.drift is False


# --------------------------------------------------------------------------
# push_spec_dir — the verification contract
# --------------------------------------------------------------------------


def test_a_delivery_puts_the_spec_where_it_was_asked_for(tmp_path, spec_src):
    # Arrange
    peer = HonestPeer(tmp_path / "peer" / "scitex-hub")
    # Act
    push_spec_dir(spec_src, REMOTE_DIR, peer)
    # Assert
    assert (peer.spec_dir / "spec.yaml").read_text() == "host: scitex-nas-03\n"


def test_a_delivery_carries_nested_files(tmp_path, spec_src):
    # Arrange
    peer = HonestPeer(tmp_path / "peer" / "scitex-hub")
    # Act
    push_spec_dir(spec_src, REMOTE_DIR, peer)
    # Assert
    assert (peer.spec_dir / "to_home" / "CLAUDE.md").exists()


def test_a_delivery_never_ships_peer_side_runtime_state(tmp_path, spec_src):
    # Arrange
    peer = HonestPeer(tmp_path / "peer" / "scitex-hub")
    # Act
    push_spec_dir(spec_src, REMOTE_DIR, peer)
    # Assert
    assert not (peer.spec_dir / "runtime").exists()


def test_a_verified_delivery_returns_the_peers_own_manifest(tmp_path, spec_src):
    # Arrange
    peer = HonestPeer(tmp_path / "peer" / "scitex-hub")
    # Act
    landed = push_spec_dir(spec_src, REMOTE_DIR, peer)
    # Assert
    assert landed == local_manifest(spec_src)


def test_a_transfer_that_exits_zero_but_lands_elsewhere_is_an_error(
    tmp_path, spec_src
):
    """THE regression test. scitex-nas-03's patched rsync exited 0, listed the
    files, and wrote them one level away; the old code then started the agent
    from the stale spec and called the dispatch a success."""
    # Arrange
    peer = ReroutingPeer(tmp_path / "peer" / "asked-for", tmp_path / "peer" / "actual")
    # Act
    # Assert
    with pytest.raises(RuntimeError, match="does NOT"):
        push_spec_dir(spec_src, REMOTE_DIR, peer, peer="scitex-nas-03")


def test_the_mis_delivery_error_names_the_files_that_never_arrived(
    rerouted_delivery_error,
):
    # Arrange
    expected = "spec.yaml"
    # Act
    message = rerouted_delivery_error
    # Assert
    assert expected in message


def test_the_mis_delivery_error_warns_about_the_stale_spec(
    rerouted_delivery_error,
):
    """The consequence is what makes this urgent: the next step boots the
    agent from whatever spec is actually at that path."""
    # Arrange
    expected = "stale spec"
    # Act
    message = rerouted_delivery_error
    # Assert
    assert expected in message


def test_the_mis_delivery_error_names_the_peer(rerouted_delivery_error):
    # Arrange
    expected = "scitex-nas-03"
    # Act
    message = rerouted_delivery_error
    # Assert
    assert expected in message


def test_a_failing_extraction_is_reported_with_the_peers_stderr(spec_src):
    # Arrange
    peer = UnreachablePeer()
    # Act
    # Assert
    with pytest.raises(RuntimeError, match="mkdir failed"):
        push_spec_dir(spec_src, REMOTE_DIR, peer)


# EOF


class PeerExitingWith:
    """A peer whose shell exits with a chosen code.

    The RETURN CODE is the subject under test here, so it is supplied
    directly rather than provoked: reproducing a real rc=127 would mean
    breaking a PATH on a real machine, which tests what the machine did,
    not what our message says about it.
    """

    def __init__(self, returncode: int, stderr: bytes = b"") -> None:
        self.returncode = returncode
        self.stderr = stderr

    def __call__(self, script: str, stdin: bytes | None = None):
        return subprocess.CompletedProcess(
            args=[], returncode=self.returncode, stdout=b"", stderr=self.stderr
        )


def manifest_failure_message(returncode: int, stderr: bytes = b"") -> str:
    """The ``RuntimeError`` text raised for ``returncode``.

    Captured through one helper so every claim about that message is its own
    single-assertion test, the way ``rerouted_delivery_error`` does above.
    """
    peer = PeerExitingWith(returncode, stderr)
    with pytest.raises(RuntimeError) as excinfo:
        read_remote_manifest(REMOTE_DIR, peer)
    return str(excinfo.value)


def test_command_not_found_does_not_accuse_the_spec_manifest():
    """rc=127 proves the script never ran, so the manifest was never read.

    Reporting it as "could not read the spec manifest" sends the reader to
    hunt a missing or corrupt spec on the peer that was fine all along.
    """
    # Arrange
    # Act
    message = manifest_failure_message(127)
    # Assert
    assert "Could not read the spec manifest" not in message


def test_command_not_found_names_the_missing_command_as_the_cause():
    # Arrange
    # Act
    message = manifest_failure_message(127)
    # Assert
    assert "NOT FOUND on the peer" in message


def test_command_not_found_still_reports_the_spec_dir():
    """Correcting the accusation must not cost the reader the path."""
    # Arrange
    # Act
    message = manifest_failure_message(127)
    # Assert
    assert REMOTE_DIR in message


def test_a_non_executable_command_is_distinguished_from_a_missing_one():
    """126 and 127 are different faults and get different remedies."""
    # Arrange
    # Act
    message = manifest_failure_message(126)
    # Assert
    assert "not executable" in message


def test_a_peer_without_md5sum_is_named_as_such():
    """Exit 3 is the script's OWN signal — it already knows why it stopped."""
    # Arrange
    # Act
    message = manifest_failure_message(3)
    # Assert
    assert "no `md5sum`" in message


def test_an_unenterable_spec_dir_points_at_permissions_not_contents():
    """Exit 1 is the script's ``cd`` failing: the directory IS there."""
    # Arrange
    # Act
    message = manifest_failure_message(1)
    # Assert
    assert "could not be entered" in message


def test_an_undocumented_exit_keeps_the_original_message():
    """A claim is made only for codes whose meaning the script fixes.

    Anything else stays exactly as vague as our knowledge of it — inventing a
    cause for an unknown code is the same defect in the other direction.
    """
    # Arrange
    # Act
    message = manifest_failure_message(11)
    # Assert
    assert "Could not read the spec manifest" in message


def test_the_peers_own_stderr_survives_the_rewrite():
    """Whatever the peer itself said is the most specific evidence there is."""
    # Arrange
    # Act
    message = manifest_failure_message(127, b"sh: 1: find: not found")
    # Assert
    assert "sh: 1: find: not found" in message
