"""Tests for the token rotation + master-bearer push (ADR-0021 PR-B).

PA-306: no ``unittest.mock``. The rotation is driven over the injectable
runner seam with real ``CompletedProcess`` replies, against real 0600
token files in ``tmp_path``. The mint is a real callable returning a
pinned value (a seam, not a mock), so the digests in the assertions are
real ``sha256`` output.

**No live peer is ever touched.** Every ssh here is a scripted reply.

The legs that matter are the FAILURE legs: a rotation that half-lands
must never report success, must say WHICH SIDE HOLDS WHAT, and must keep
the pre-rotate backup. Each test: AAA (TQ002), one assertion (TQ007),
behaviour-shaped name (TQ003).
"""

from __future__ import annotations

import hashlib
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import pytest

from scitex_agent_container._hostsync._token_rotate import (
    push_master_bearer,
    rotate_peer_tokens,
)
from scitex_agent_container._hostsync._token_state import sha12

_NOW = datetime(2026, 7, 16, 8, 0, 0, tzinfo=timezone.utc)
_STAMP = "20260716T080000Z"
_MASTER = "master-x"
_MASTER_BEARER = "master-bearer-value"
_OLD_PEER_BEARER = "old-peer-bearer-value"
_NEW_PEER_BEARER = "new-peer-bearer-value-minted"


def _full(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


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


def _cfg_read_block(cfg_path: Path, master_cfg) -> str:
    """A CURRENT config read — the rotation's precondition."""
    import base64

    from scitex_agent_container._hostsync._peer_config import render_peer_config
    from scitex_agent_container._hostsync._push_config import master_config_sha

    text = render_peer_config(
        "spartan",
        master_cfg,
        master_name=_MASTER,
        now=_NOW,
        master_sha=master_config_sha(master_cfg),
    )
    b64 = base64.b64encode(text.encode()).decode()
    return f"SAC_PUSHCFG b64={b64}\nSAC_PUSHCFG end\n"


def _tok_read_block(listen_digest: str = "") -> str:
    digest = listen_digest or _full(_OLD_PEER_BEARER)
    return (
        "SAC_PUSHTOK hostname=spartan-login1\n"
        f"SAC_PUSHTOK listen=listen-spartan-login1.token {digest}\n"
        f"SAC_PUSHTOK peer={_MASTER}.token {_full(_MASTER_BEARER)}\n"
        "SAC_PUSHTOK end\n"
    )


def _status(code: int) -> str:
    return f"\nSAC_PUSHTOK status={code}\n"


@pytest.fixture
def cfg_file(tmp_path: Path) -> Path:
    p = tmp_path / "config.yaml"
    p.write_text(f"host:\n  canonical: {_MASTER}\npeers:\n  spartan:\n    ssh: sp\n")
    return p


@pytest.fixture
def master_cfg(cfg_file: Path):
    from scitex_agent_container._state.host_config import load

    return load(cfg_file)


@pytest.fixture
def master_token(tmp_path: Path) -> Path:
    p = tmp_path / "listen-master.token"
    p.write_text(_MASTER_BEARER)
    p.chmod(0o600)
    return p


@pytest.fixture
def peer_tokens_dir(tmp_path: Path) -> Path:
    """The master's peer-tokens/ registry, holding the OLD peer bearer."""
    d = tmp_path / "peer-tokens"
    d.mkdir()
    tok = d / "spartan.token"
    tok.write_text(_OLD_PEER_BEARER)
    tok.chmod(0o600)
    return d


def _rotate(master_cfg, peer_tokens_dir, runner, **kw):
    return rotate_peer_tokens(
        "spartan",
        master_cfg,
        master_name=_MASTER,
        now=_NOW,
        runner=runner,
        tokens_dir=peer_tokens_dir,
        mint=lambda: _NEW_PEER_BEARER,
        **kw,
    )


def _happy_runner(cfg_file, master_cfg):
    """config read -> token read -> seed -> restart -> probe(new) -> probe(bogus)."""
    return _ScriptedRunner(
        _proc(stdout=_cfg_read_block(cfg_file, master_cfg)),
        _proc(stdout=_tok_read_block()),
        _proc(),  # seed write
        _proc(),  # sac listen restart
        _proc(stdout=_status(200)),  # new bearer accepted
        _proc(stdout=_status(403)),  # bogus bearer rejected
    )


# ---------------------------------------------------------------------------
# rotate — the happy path
# ---------------------------------------------------------------------------


def test_rotation_reports_rotated_on_the_happy_path(
    cfg_file, master_cfg, peer_tokens_dir
):
    # Arrange
    runner = _happy_runner(cfg_file, master_cfg)
    # Act
    result = _rotate(master_cfg, peer_tokens_dir, runner)
    # Assert
    assert result.action == "rotated"


def test_rotation_exits_zero_on_the_happy_path(cfg_file, master_cfg, peer_tokens_dir):
    # Arrange
    runner = _happy_runner(cfg_file, master_cfg)
    # Act
    result = _rotate(master_cfg, peer_tokens_dir, runner)
    # Assert
    assert result.exit_code == 0


def test_rotation_stores_the_new_bearer_on_the_master(
    cfg_file, master_cfg, peer_tokens_dir
):
    # Arrange
    runner = _happy_runner(cfg_file, master_cfg)
    # Act
    _rotate(master_cfg, peer_tokens_dir, runner)
    # Assert
    assert (peer_tokens_dir / "spartan.token").read_text() == _NEW_PEER_BEARER


def test_rotation_leaves_the_master_copy_private(cfg_file, master_cfg, peer_tokens_dir):
    # Arrange
    runner = _happy_runner(cfg_file, master_cfg)
    # Act
    _rotate(master_cfg, peer_tokens_dir, runner)
    mode = (peer_tokens_dir / "spartan.token").stat().st_mode & 0o777
    # Assert
    assert mode == 0o600


def test_rotation_seeds_both_candidate_paths_on_the_peer(
    cfg_file, master_cfg, peer_tokens_dir
):
    # Arrange — the FQDN fix: whichever file the listen reads, same value.
    runner = _happy_runner(cfg_file, master_cfg)
    # Act
    _rotate(master_cfg, peer_tokens_dir, runner)
    seed_argv = " ".join(runner.calls[2][0])
    # Assert
    assert (
        "tokens/listen-spartan-login1.token" in seed_argv
        and "tokens/listen-spartan.token" in seed_argv
    )


def test_rotation_pipes_the_new_bearer_on_stdin(cfg_file, master_cfg, peer_tokens_dir):
    # Arrange
    runner = _happy_runner(cfg_file, master_cfg)
    # Act
    _rotate(master_cfg, peer_tokens_dir, runner)
    # Assert
    assert runner.calls[2][1]["input"] == _NEW_PEER_BEARER


def test_rotation_never_puts_the_new_bearer_in_an_argv(
    cfg_file, master_cfg, peer_tokens_dir
):
    # Arrange — every dispatch, not just the write.
    runner = _happy_runner(cfg_file, master_cfg)
    # Act
    _rotate(master_cfg, peer_tokens_dir, runner)
    every_argv = " ".join(" ".join(argv) for argv, _kw in runner.calls)
    # Assert
    assert _NEW_PEER_BEARER not in every_argv


def test_rotation_restarts_the_peers_listen(cfg_file, master_cfg, peer_tokens_dir):
    # Arrange — the seeded file is inert until the process re-reads it.
    runner = _happy_runner(cfg_file, master_cfg)
    # Act
    _rotate(master_cfg, peer_tokens_dir, runner)
    # Assert
    assert runner.calls[3][0][-3:] == ["sac", "listen", "restart"]


def test_rotation_verifies_with_two_probes(cfg_file, master_cfg, peer_tokens_dir):
    # Arrange — one probe cannot disagree; the control makes it real.
    runner = _happy_runner(cfg_file, master_cfg)
    # Act
    _rotate(master_cfg, peer_tokens_dir, runner)
    # Assert — 6 dispatches: cfg, tok, seed, restart, probe, control.
    assert len(runner.calls) == 6


def test_verified_rotation_discards_the_pre_rotate_backup(
    cfg_file, master_cfg, peer_tokens_dir
):
    # Arrange
    runner = _happy_runner(cfg_file, master_cfg)
    # Act
    _rotate(master_cfg, peer_tokens_dir, runner)
    # Assert — only a VERIFIED rotation may destroy the old bearer.
    assert not (peer_tokens_dir / f"spartan.token.pre-rotate-{_STAMP}").exists()


def test_rotation_reports_the_new_digest_not_the_value(
    cfg_file, master_cfg, peer_tokens_dir
):
    # Arrange
    runner = _happy_runner(cfg_file, master_cfg)
    # Act
    result = _rotate(master_cfg, peer_tokens_dir, runner)
    # Assert
    assert result.new_sha12 == sha12(_NEW_PEER_BEARER)


def test_rotation_result_never_carries_a_token_value(
    cfg_file, master_cfg, peer_tokens_dir
):
    # Arrange
    runner = _happy_runner(cfg_file, master_cfg)
    # Act
    result = _rotate(master_cfg, peer_tokens_dir, runner)
    rendered = repr(result.to_dict())
    # Assert
    assert _NEW_PEER_BEARER not in rendered and _OLD_PEER_BEARER not in rendered


# ---------------------------------------------------------------------------
# rotate — refusals (nothing may be written)
# ---------------------------------------------------------------------------


def test_rotation_refuses_an_unreadable_config_state(master_cfg, peer_tokens_dir):
    # Arrange — a peer we cannot read is a peer we do not rotate.
    runner = _ScriptedRunner(_proc(stdout="", rc=255, stderr="refused\n"))
    # Act
    result = _rotate(master_cfg, peer_tokens_dir, runner)
    # Assert
    assert result.action == "refused"


def test_rotation_refusal_on_unknown_config_writes_nothing(master_cfg, peer_tokens_dir):
    # Arrange — the refusal must be structural, not cosmetic.
    runner = _ScriptedRunner(_proc(stdout="", rc=255, stderr="refused\n"))
    # Act
    _rotate(master_cfg, peer_tokens_dir, runner)
    # Assert — the master's copy is untouched.
    assert (peer_tokens_dir / "spartan.token").read_text() == _OLD_PEER_BEARER


def test_rotation_refuses_an_unreadable_token_state(
    cfg_file, master_cfg, peer_tokens_dir
):
    # Arrange — config readable, tokens not.
    runner = _ScriptedRunner(
        _proc(stdout=_cfg_read_block(cfg_file, master_cfg)),
        _proc(stdout="", rc=255, stderr="refused\n"),
    )
    # Act
    result = _rotate(master_cfg, peer_tokens_dir, runner)
    # Assert
    assert result.action == "refused"


def test_rotation_refusal_exits_two(master_cfg, peer_tokens_dir):
    # Arrange — UNKNOWN is its own exit code, never "drift".
    runner = _ScriptedRunner(_proc(stdout="", rc=255, stderr="refused\n"))
    # Act
    result = _rotate(master_cfg, peer_tokens_dir, runner)
    # Assert
    assert result.exit_code == 2


# ---------------------------------------------------------------------------
# rotate — failure legs (each names which side holds what)
# ---------------------------------------------------------------------------


def test_seed_failure_leaves_the_master_copy_unchanged(
    cfg_file, master_cfg, peer_tokens_dir
):
    # Arrange — the peer write fails, so the master must not move.
    runner = _ScriptedRunner(
        _proc(stdout=_cfg_read_block(cfg_file, master_cfg)),
        _proc(stdout=_tok_read_block()),
        _proc(rc=1, stderr="disk full\n"),
    )
    # Act
    _rotate(master_cfg, peer_tokens_dir, runner)
    # Assert
    assert (peer_tokens_dir / "spartan.token").read_text() == _OLD_PEER_BEARER


def test_seed_failure_reports_failed(cfg_file, master_cfg, peer_tokens_dir):
    # Arrange
    runner = _ScriptedRunner(
        _proc(stdout=_cfg_read_block(cfg_file, master_cfg)),
        _proc(stdout=_tok_read_block()),
        _proc(rc=1, stderr="disk full\n"),
    )
    # Act
    result = _rotate(master_cfg, peer_tokens_dir, runner)
    # Assert
    assert result.action == "failed"


def test_restart_failure_reports_failed(cfg_file, master_cfg, peer_tokens_dir):
    # Arrange — both sides written, but the listen never restarted: the
    # master now holds a bearer the peer's listen does not serve.
    runner = _ScriptedRunner(
        _proc(stdout=_cfg_read_block(cfg_file, master_cfg)),
        _proc(stdout=_tok_read_block()),
        _proc(),
        _proc(rc=1, stderr="ERROR: port still held by PID 9\n"),
    )
    # Act
    result = _rotate(master_cfg, peer_tokens_dir, runner)
    # Assert
    assert result.action == "failed"


def test_restart_failure_retains_the_pre_rotate_backup(
    cfg_file, master_cfg, peer_tokens_dir
):
    # Arrange — the operator's undo must survive an unverified rotation.
    runner = _ScriptedRunner(
        _proc(stdout=_cfg_read_block(cfg_file, master_cfg)),
        _proc(stdout=_tok_read_block()),
        _proc(),
        _proc(rc=1, stderr="ERROR: port still held\n"),
    )
    # Act
    _rotate(master_cfg, peer_tokens_dir, runner)
    backup = peer_tokens_dir / f"spartan.token.pre-rotate-{_STAMP}"
    # Assert
    assert backup.read_text() == _OLD_PEER_BEARER


def test_restart_failure_never_claims_verified(cfg_file, master_cfg, peer_tokens_dir):
    # Arrange
    runner = _ScriptedRunner(
        _proc(stdout=_cfg_read_block(cfg_file, master_cfg)),
        _proc(stdout=_tok_read_block()),
        _proc(),
        _proc(rc=1, stderr="ERROR: port still held\n"),
    )
    # Act
    result = _rotate(master_cfg, peer_tokens_dir, runner)
    # Assert
    assert not result.verified


def test_restart_failure_names_the_split_state(cfg_file, master_cfg, peer_tokens_dir):
    # Arrange — never leave the two sides silently split.
    runner = _ScriptedRunner(
        _proc(stdout=_cfg_read_block(cfg_file, master_cfg)),
        _proc(stdout=_tok_read_block()),
        _proc(),
        _proc(rc=1, stderr="ERROR: port still held\n"),
    )
    # Act
    result = _rotate(master_cfg, peer_tokens_dir, runner)
    # Assert
    assert "a2a is DOWN" in result.detail


def test_rejected_new_bearer_reports_failed(cfg_file, master_cfg, peer_tokens_dir):
    # Arrange — the listen restarted but did NOT adopt the new token.
    runner = _ScriptedRunner(
        _proc(stdout=_cfg_read_block(cfg_file, master_cfg)),
        _proc(stdout=_tok_read_block()),
        _proc(),
        _proc(),
        _proc(stdout=_status(403)),
    )
    # Act
    result = _rotate(master_cfg, peer_tokens_dir, runner)
    # Assert
    assert result.action == "failed"


def test_rejected_new_bearer_says_the_listen_refused_it(
    cfg_file, master_cfg, peer_tokens_dir
):
    # Arrange
    runner = _ScriptedRunner(
        _proc(stdout=_cfg_read_block(cfg_file, master_cfg)),
        _proc(stdout=_tok_read_block()),
        _proc(),
        _proc(),
        _proc(stdout=_status(403)),
    )
    # Act
    result = _rotate(master_cfg, peer_tokens_dir, runner)
    # Assert
    assert "REJECTED the new bearer" in result.detail


def test_admitted_bogus_bearer_reports_failed(cfg_file, master_cfg, peer_tokens_dir):
    # Arrange — THE false-green guard: a listen that admits EVERYTHING
    # would accept our new token too. That proves nothing.
    runner = _ScriptedRunner(
        _proc(stdout=_cfg_read_block(cfg_file, master_cfg)),
        _proc(stdout=_tok_read_block()),
        _proc(),
        _proc(),
        _proc(stdout=_status(200)),  # new bearer accepted
        _proc(stdout=_status(200)),  # bogus ALSO accepted -> gate is dead
    )
    # Act
    result = _rotate(master_cfg, peer_tokens_dir, runner)
    # Assert
    assert result.action == "failed"


def test_admitted_bogus_bearer_names_the_false_green(
    cfg_file, master_cfg, peer_tokens_dir
):
    # Arrange
    runner = _ScriptedRunner(
        _proc(stdout=_cfg_read_block(cfg_file, master_cfg)),
        _proc(stdout=_tok_read_block()),
        _proc(),
        _proc(),
        _proc(stdout=_status(200)),
        _proc(stdout=_status(200)),
    )
    # Act
    result = _rotate(master_cfg, peer_tokens_dir, runner)
    # Assert
    assert "ADMITTED a bogus bearer" in result.detail


def test_unanswered_probe_is_not_a_verified_rotation(
    cfg_file, master_cfg, peer_tokens_dir
):
    # Arrange — a transport failure is UNKNOWN, and unknown is not proof.
    runner = _ScriptedRunner(
        _proc(stdout=_cfg_read_block(cfg_file, master_cfg)),
        _proc(stdout=_tok_read_block()),
        _proc(),
        _proc(),
        _proc(stdout="", rc=255, stderr="refused\n"),
    )
    # Act
    result = _rotate(master_cfg, peer_tokens_dir, runner)
    # Assert
    assert not result.verified


def test_unverified_rotation_exits_two(cfg_file, master_cfg, peer_tokens_dir):
    # Arrange
    runner = _ScriptedRunner(
        _proc(stdout=_cfg_read_block(cfg_file, master_cfg)),
        _proc(stdout=_tok_read_block()),
        _proc(),
        _proc(),
        _proc(stdout=_status(403)),
    )
    # Act
    result = _rotate(master_cfg, peer_tokens_dir, runner)
    # Assert
    assert result.exit_code == 2


def test_unverified_rotation_retains_the_backup(cfg_file, master_cfg, peer_tokens_dir):
    # Arrange
    runner = _ScriptedRunner(
        _proc(stdout=_cfg_read_block(cfg_file, master_cfg)),
        _proc(stdout=_tok_read_block()),
        _proc(),
        _proc(),
        _proc(stdout=_status(403)),
    )
    # Act
    result = _rotate(master_cfg, peer_tokens_dir, runner)
    # Assert
    assert result.backup.endswith(f"spartan.token.pre-rotate-{_STAMP}")


def test_rotation_backs_up_the_peer_side_too(cfg_file, master_cfg, peer_tokens_dir):
    # Arrange
    runner = _happy_runner(cfg_file, master_cfg)
    # Act
    _rotate(master_cfg, peer_tokens_dir, runner)
    # Assert — the peer's old token file is preserved in the same write.
    assert f"pre-rotate-{_STAMP}" in " ".join(runner.calls[2][0])


# ---------------------------------------------------------------------------
# push_master_bearer — the OUTBOUND leg only
# ---------------------------------------------------------------------------


def test_push_master_bearer_reports_pushed_when_verified(master_cfg, master_token):
    # Arrange — write, then a read-back reporting the master's digest.
    runner = _ScriptedRunner(
        _proc(),
        _proc(
            stdout=(
                "SAC_PUSHTOK hostname=spartan-login1\n"
                f"SAC_PUSHTOK peer={_MASTER}.token {_full(_MASTER_BEARER)}\n"
                "SAC_PUSHTOK end\n"
            )
        ),
    )
    # Act
    result = push_master_bearer(
        "spartan",
        master_cfg,
        master_name=_MASTER,
        runner=runner,
        master_token_path=master_token,
    )
    # Assert
    assert result.action == "pushed"


def test_push_master_bearer_verifies_by_read_back_not_exit_code(
    master_cfg, master_token
):
    # Arrange — the write "succeeded" but the peer reads back something
    # else: sac does not report a success it cannot substantiate.
    runner = _ScriptedRunner(
        _proc(),
        _proc(
            stdout=(
                "SAC_PUSHTOK hostname=spartan-login1\n"
                f"SAC_PUSHTOK peer={_MASTER}.token {_full('something-else')}\n"
                "SAC_PUSHTOK end\n"
            )
        ),
    )
    # Act
    result = push_master_bearer(
        "spartan",
        master_cfg,
        master_name=_MASTER,
        runner=runner,
        master_token_path=master_token,
    )
    # Assert
    assert result.action == "failed"


def test_push_master_bearer_never_puts_the_value_in_the_argv(master_cfg, master_token):
    # Arrange
    runner = _ScriptedRunner(
        _proc(),
        _proc(
            stdout=(
                "SAC_PUSHTOK hostname=spartan-login1\n"
                f"SAC_PUSHTOK peer={_MASTER}.token {_full(_MASTER_BEARER)}\n"
                "SAC_PUSHTOK end\n"
            )
        ),
    )
    # Act
    push_master_bearer(
        "spartan",
        master_cfg,
        master_name=_MASTER,
        runner=runner,
        master_token_path=master_token,
    )
    # Assert
    assert _MASTER_BEARER not in " ".join(runner.calls[0][0])


def test_push_master_bearer_refuses_without_a_master_bearer(master_cfg, tmp_path):
    # Arrange — the master's listen never ran; there is nothing to push.
    runner = _ScriptedRunner()
    # Act
    result = push_master_bearer(
        "spartan",
        master_cfg,
        master_name=_MASTER,
        runner=runner,
        master_token_path=tmp_path / "does-not-exist.token",
    )
    # Assert
    assert result.action == "refused"


def test_push_master_bearer_refusal_dispatches_nothing(master_cfg, tmp_path):
    # Arrange
    runner = _ScriptedRunner()
    # Act
    push_master_bearer(
        "spartan",
        master_cfg,
        master_name=_MASTER,
        runner=runner,
        master_token_path=tmp_path / "does-not-exist.token",
    )
    # Assert
    assert len(runner.calls) == 0


def test_push_master_bearer_never_restarts_the_peers_listen(master_cfg, master_token):
    # Arrange — the forwarder reads peer-tokens/ per request, so this leg
    # needs no restart. Restarting anyway would be gratuitous downtime.
    runner = _ScriptedRunner(
        _proc(),
        _proc(
            stdout=(
                "SAC_PUSHTOK hostname=spartan-login1\n"
                f"SAC_PUSHTOK peer={_MASTER}.token {_full(_MASTER_BEARER)}\n"
                "SAC_PUSHTOK end\n"
            )
        ),
    )
    # Act
    push_master_bearer(
        "spartan",
        master_cfg,
        master_name=_MASTER,
        runner=runner,
        master_token_path=master_token,
    )
    every_argv = " ".join(" ".join(argv) for argv, _kw in runner.calls)
    # Assert
    assert "listen restart" not in every_argv
