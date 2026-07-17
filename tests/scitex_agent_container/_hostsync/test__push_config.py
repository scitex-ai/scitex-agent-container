"""Tests for the push-config verdicts and orchestration.

PA-306: no ``unittest.mock``. Classification is driven with REAL
strings produced by the real renderer; the orchestrators run over an
injectable runner (a plain callable returning real
``CompletedProcess`` objects — the ``check_peer`` seam). Verdicts are
three-state honest: the tests pin that UNDETERMINED never mutates and
that a push refuses to claim what it cannot verify. Each test: AAA
(TQ002), one assertion (TQ007), behaviour-shaped name (TQ003).
"""

from __future__ import annotations

import base64
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import pytest

from scitex_agent_container._hostsync._peer_config import render_peer_config
from scitex_agent_container._hostsync._push_config import (
    ConfigVerdict,
    check_config_peer,
    classify_remote_config,
    master_config_sha,
    push_config_peer,
)
from scitex_agent_container._hostsync._push_config_io import RemoteConfigRead

_NOW = datetime(2026, 7, 16, 8, 0, 0, tzinfo=timezone.utc)
_LATER = datetime(2026, 7, 16, 9, 30, 0, tzinfo=timezone.utc)


class _ScriptedRunner:
    """Injectable runner: pops real ``CompletedProcess`` results in order."""

    def __init__(self, *results):
        self._results = list(results)
        self.calls: list[tuple[list, dict]] = []

    def __call__(self, argv, **kwargs):
        self.calls.append((list(argv), dict(kwargs)))
        result = self._results.pop(0)
        if isinstance(result, BaseException):
            raise result
        return result


def _proc(stdout: str = "", rc: int = 0, stderr: str = ""):
    return subprocess.CompletedProcess(
        args=["ssh"], returncode=rc, stdout=stdout, stderr=stderr
    )


def _b64_block(text: str) -> str:
    b64 = base64.b64encode(text.encode()).decode()
    return f"SAC_PUSHCFG b64={b64}\nSAC_PUSHCFG end\n"


_ABSENT_BLOCK = "SAC_PUSHCFG __ABSENT__\nSAC_PUSHCFG end\n"


@pytest.fixture
def master_cfg(tmp_path: Path):
    """A real master config (real file, so master_config_sha is real)."""
    from scitex_agent_container._state.host_config import load

    p = tmp_path / "config.yaml"
    p.write_text("host:\n  canonical: master-x\npeers:\n  spartan:\n    ssh: sp\n")
    return load(p)


def _rendered(cfg, *, peer: str = "spartan", now=_NOW, sha: str = "") -> str:
    return render_peer_config(
        peer,
        cfg,
        master_name="master-x",
        now=now,
        master_sha=sha or master_config_sha(cfg),
    )


# ---------------------------------------------------------------------------
# classify_remote_config — pure, driven with real renderer strings
# ---------------------------------------------------------------------------


def test_classify_byte_identical_as_current(master_cfg):
    # Arrange
    rendered = _rendered(master_cfg)
    remote = RemoteConfigRead(ok=True, text=rendered)
    # Act
    verdict, _detail = classify_remote_config(remote, rendered)
    # Assert
    assert verdict is ConfigVerdict.CURRENT


def test_classify_timestamp_only_difference_as_current(master_cfg):
    # Arrange — same master sha, different push time: same topology.
    remote = RemoteConfigRead(ok=True, text=_rendered(master_cfg, now=_NOW))
    rendered = _rendered(master_cfg, now=_LATER)
    # Act
    verdict, _detail = classify_remote_config(remote, rendered)
    # Assert
    assert verdict is ConfigVerdict.CURRENT


def test_classify_changed_master_sha_as_stale(master_cfg):
    # Arrange — the master config changed (sha) but the derived keys did
    # not: header-only staleness, still a drift to reconcile.
    remote = RemoteConfigRead(ok=True, text=_rendered(master_cfg, sha="0" * 64))
    rendered = _rendered(master_cfg)
    # Act
    verdict, _detail = classify_remote_config(remote, rendered)
    # Assert
    assert verdict is ConfigVerdict.STALE_GENERATED


def test_classify_changed_keys_as_stale(master_cfg):
    # Arrange — generated for a different canonical name: keys differ.
    remote = RemoteConfigRead(ok=True, text=_rendered(master_cfg, peer="other"))
    rendered = _rendered(master_cfg)
    # Act
    verdict, _detail = classify_remote_config(remote, rendered)
    # Assert
    assert verdict is ConfigVerdict.STALE_GENERATED


def test_classify_headerless_file_as_hand_edited(master_cfg):
    # Arrange
    remote = RemoteConfigRead(ok=True, text="peers:\n  mba: {ssh: m}\n")
    # Act
    verdict, _detail = classify_remote_config(remote, _rendered(master_cfg))
    # Assert
    assert verdict is ConfigVerdict.HAND_EDITED


def test_classify_empty_file_as_hand_edited(master_cfg):
    # Arrange — an existing empty file is not ours; refuse-and-diff.
    remote = RemoteConfigRead(ok=True, text="")
    # Act
    verdict, _detail = classify_remote_config(remote, _rendered(master_cfg))
    # Assert
    assert verdict is ConfigVerdict.HAND_EDITED


def test_classify_absent_sentinel_as_absent(master_cfg):
    # Arrange
    remote = RemoteConfigRead(ok=True, absent=True)
    # Act
    verdict, _detail = classify_remote_config(remote, _rendered(master_cfg))
    # Assert
    assert verdict is ConfigVerdict.ABSENT


def test_classify_failed_read_as_undetermined(master_cfg):
    # Arrange — "I could not look" must never read as anything else.
    remote = RemoteConfigRead(ok=False, detail="ssh exit 255")
    # Act
    verdict, _detail = classify_remote_config(remote, _rendered(master_cfg))
    # Assert
    assert verdict is ConfigVerdict.UNDETERMINED


# ---------------------------------------------------------------------------
# check_config_peer — read-only verdicts + exit codes
# ---------------------------------------------------------------------------


def test_check_current_peer_exits_zero(master_cfg):
    # Arrange
    runner = _ScriptedRunner(_proc(stdout=_b64_block(_rendered(master_cfg))))
    # Act
    result = check_config_peer(
        "spartan", master_cfg, master_name="master-x", now=_LATER, runner=runner
    )
    # Assert
    assert result.exit_code == 0


def test_check_stale_peer_exits_one(master_cfg):
    # Arrange
    runner = _ScriptedRunner(
        _proc(stdout=_b64_block(_rendered(master_cfg, sha="0" * 64)))
    )
    # Act
    result = check_config_peer(
        "spartan", master_cfg, master_name="master-x", now=_NOW, runner=runner
    )
    # Assert
    assert result.exit_code == 1


def test_check_hand_edited_peer_exits_one(master_cfg):
    # Arrange
    runner = _ScriptedRunner(_proc(stdout=_b64_block("peers: {}\n")))
    # Act
    result = check_config_peer(
        "spartan", master_cfg, master_name="master-x", now=_NOW, runner=runner
    )
    # Assert
    assert result.exit_code == 1


def test_check_absent_peer_exits_one(master_cfg):
    # Arrange
    runner = _ScriptedRunner(_proc(stdout=_ABSENT_BLOCK))
    # Act
    result = check_config_peer(
        "spartan", master_cfg, master_name="master-x", now=_NOW, runner=runner
    )
    # Assert
    assert result.exit_code == 1


def test_check_unreachable_peer_exits_two(master_cfg):
    # Arrange — UNKNOWN is neither clean nor drifted; it is its own code.
    runner = _ScriptedRunner(_proc(stdout="", rc=255, stderr="refused\n"))
    # Act
    result = check_config_peer(
        "spartan", master_cfg, master_name="master-x", now=_NOW, runner=runner
    )
    # Assert
    assert result.exit_code == 2


def test_check_never_dispatches_a_write(master_cfg):
    # Arrange — a drifted peer under --check must stay untouched.
    runner = _ScriptedRunner(_proc(stdout=_b64_block("peers: {}\n")))
    # Act
    check_config_peer(
        "spartan", master_cfg, master_name="master-x", now=_NOW, runner=runner
    )
    # Assert — structurally read-only: exactly one (read) dispatch.
    assert len(runner.calls) == 1


def test_check_drifted_peer_carries_a_diff(master_cfg):
    # Arrange
    runner = _ScriptedRunner(_proc(stdout=_b64_block("peers: {}\n")))
    # Act
    result = check_config_peer(
        "spartan", master_cfg, master_name="master-x", now=_NOW, runner=runner
    )
    # Assert — evidence, not a summary: the diff is part of the verdict.
    assert "rendered (master truth)" in result.diff


# ---------------------------------------------------------------------------
# push_config_peer — actions, refusals, verification
# ---------------------------------------------------------------------------


def test_push_creates_on_absent_and_reports_created(master_cfg):
    # Arrange — read: absent; write: ok; verify read-back: the render.
    rendered = _rendered(master_cfg)
    runner = _ScriptedRunner(
        _proc(stdout=_ABSENT_BLOCK), _proc(), _proc(stdout=_b64_block(rendered))
    )
    # Act
    result = push_config_peer(
        "spartan", master_cfg, master_name="master-x", now=_NOW, runner=runner
    )
    # Assert
    assert result.action == "created"


def test_push_writes_the_rendered_bytes_on_stdin(master_cfg):
    # Arrange
    rendered = _rendered(master_cfg)
    runner = _ScriptedRunner(
        _proc(stdout=_ABSENT_BLOCK), _proc(), _proc(stdout=_b64_block(rendered))
    )
    # Act
    push_config_peer(
        "spartan", master_cfg, master_name="master-x", now=_NOW, runner=runner
    )
    # Assert — what went over the wire is exactly the render.
    assert runner.calls[1][1]["input"] == rendered


def test_push_overwrites_stale_generated_and_reports_pushed(master_cfg):
    # Arrange — the peer holds our output for an older master config.
    rendered = _rendered(master_cfg)
    runner = _ScriptedRunner(
        _proc(stdout=_b64_block(_rendered(master_cfg, sha="0" * 64))),
        _proc(),
        _proc(stdout=_b64_block(rendered)),
    )
    # Act
    result = push_config_peer(
        "spartan", master_cfg, master_name="master-x", now=_NOW, runner=runner
    )
    # Assert
    assert result.action == "pushed"


def test_push_current_peer_is_a_reported_noop(master_cfg):
    # Arrange
    runner = _ScriptedRunner(_proc(stdout=_b64_block(_rendered(master_cfg))))
    # Act
    push_config_peer(
        "spartan", master_cfg, master_name="master-x", now=_NOW, runner=runner
    )
    # Assert — no write dispatched for a current peer.
    assert len(runner.calls) == 1


def test_push_refuses_hand_edited_without_adopt(master_cfg):
    # Arrange
    runner = _ScriptedRunner(_proc(stdout=_b64_block("peers: {}\n")))
    # Act
    result = push_config_peer(
        "spartan", master_cfg, master_name="master-x", now=_NOW, runner=runner
    )
    # Assert
    assert result.action == "refused"


def test_push_hand_edited_refusal_never_writes(master_cfg):
    # Arrange — the refusal must be structural, not cosmetic.
    runner = _ScriptedRunner(_proc(stdout=_b64_block("peers: {}\n")))
    # Act
    push_config_peer(
        "spartan", master_cfg, master_name="master-x", now=_NOW, runner=runner
    )
    # Assert
    assert len(runner.calls) == 1


def test_push_hand_edited_refusal_names_adopt(master_cfg):
    # Arrange
    runner = _ScriptedRunner(_proc(stdout=_b64_block("peers: {}\n")))
    # Act
    result = push_config_peer(
        "spartan", master_cfg, master_name="master-x", now=_NOW, runner=runner
    )
    # Assert — on failure, name the next command.
    assert "--adopt" in result.detail


def test_push_hand_edited_refusal_prints_the_diff(master_cfg):
    # Arrange
    runner = _ScriptedRunner(_proc(stdout=_b64_block("peers: {}\n")))
    # Act
    result = push_config_peer(
        "spartan", master_cfg, master_name="master-x", now=_NOW, runner=runner
    )
    # Assert — nobody overwrites (or declines) what they never saw.
    assert "rendered (master truth)" in result.diff


def test_adopt_replaces_hand_edited_and_reports_adopted(master_cfg):
    # Arrange
    rendered = _rendered(master_cfg)
    runner = _ScriptedRunner(
        _proc(stdout=_b64_block("peers: {}\n")),
        _proc(),
        _proc(stdout=_b64_block(rendered)),
    )
    # Act
    result = push_config_peer(
        "spartan",
        master_cfg,
        adopt=True,
        master_name="master-x",
        now=_NOW,
        runner=runner,
    )
    # Assert
    assert result.action == "adopted"


def test_adopt_write_carries_the_backup_stamp(master_cfg):
    # Arrange
    rendered = _rendered(master_cfg)
    runner = _ScriptedRunner(
        _proc(stdout=_b64_block("peers: {}\n")),
        _proc(),
        _proc(stdout=_b64_block(rendered)),
    )
    # Act
    push_config_peer(
        "spartan",
        master_cfg,
        adopt=True,
        master_name="master-x",
        now=_NOW,
        runner=runner,
    )
    # Assert — the peer-side backup happens in the same write dispatch.
    assert "pre-adopt-20260716T080000Z" in " ".join(runner.calls[1][0])


def test_adopt_refused_when_verdict_is_not_hand_edited(master_cfg):
    # Arrange — --adopt on a CURRENT peer is an operator mistake.
    runner = _ScriptedRunner(_proc(stdout=_b64_block(_rendered(master_cfg))))
    # Act
    result = push_config_peer(
        "spartan",
        master_cfg,
        adopt=True,
        master_name="master-x",
        now=_NOW,
        runner=runner,
    )
    # Assert
    assert result.action == "refused"


def test_adopt_refused_on_absent_peer_too(master_cfg):
    # Arrange — nothing to adopt when there is no file.
    runner = _ScriptedRunner(_proc(stdout=_ABSENT_BLOCK))
    # Act
    result = push_config_peer(
        "spartan",
        master_cfg,
        adopt=True,
        master_name="master-x",
        now=_NOW,
        runner=runner,
    )
    # Assert
    assert result.exit_code == 1


def test_push_undetermined_peer_never_mutates(master_cfg):
    # Arrange — an unreachable peer gets exactly one (read) dispatch.
    runner = _ScriptedRunner(_proc(stdout="", rc=255, stderr="refused\n"))
    # Act
    push_config_peer(
        "spartan", master_cfg, master_name="master-x", now=_NOW, runner=runner
    )
    # Assert
    assert len(runner.calls) == 1


def test_push_undetermined_peer_exits_two(master_cfg):
    # Arrange
    runner = _ScriptedRunner(_proc(stdout="", rc=255, stderr="refused\n"))
    # Act
    result = push_config_peer(
        "spartan", master_cfg, master_name="master-x", now=_NOW, runner=runner
    )
    # Assert
    assert result.exit_code == 2


def test_push_reports_failed_when_write_fails(master_cfg):
    # Arrange — read: absent; write: non-zero exit.
    runner = _ScriptedRunner(
        _proc(stdout=_ABSENT_BLOCK), _proc(rc=1, stderr="disk full\n")
    )
    # Act
    result = push_config_peer(
        "spartan", master_cfg, master_name="master-x", now=_NOW, runner=runner
    )
    # Assert
    assert result.action == "failed"


def test_push_reports_failed_when_read_back_differs(master_cfg):
    # Arrange — the write "succeeded" but the peer reads back different
    # bytes: sac must not report a success it cannot substantiate.
    runner = _ScriptedRunner(
        _proc(stdout=_ABSENT_BLOCK),
        _proc(),
        _proc(stdout=_b64_block("something: else\n")),
    )
    # Act
    result = push_config_peer(
        "spartan", master_cfg, master_name="master-x", now=_NOW, runner=runner
    )
    # Assert
    assert result.action == "failed"


def test_push_verify_failure_exits_two(master_cfg):
    # Arrange
    runner = _ScriptedRunner(
        _proc(stdout=_ABSENT_BLOCK),
        _proc(),
        _proc(stdout=_b64_block("something: else\n")),
    )
    # Act
    result = push_config_peer(
        "spartan", master_cfg, master_name="master-x", now=_NOW, runner=runner
    )
    # Assert
    assert result.exit_code == 2
