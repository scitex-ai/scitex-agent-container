"""Tests for the token remote I/O (read / write / restart / auth probe).

PA-306: no ``unittest.mock``. The runner seam takes a REAL callable
returning real :class:`subprocess.CompletedProcess` objects (or raising a
real ``subprocess.TimeoutExpired``) — the same injection style
``check_peer`` and PR-A's push-config IO use. Marker strings are
hard-coded on purpose: they are the wire format, and a test that imported
them would follow a drift it exists to catch.

The load-bearing assertions here are the SECRECY ones: a token value must
never reach an argv, and the read path must return digests only. Each
test: AAA (TQ002), one assertion (TQ007), behaviour-shaped name (TQ003).
"""

from __future__ import annotations

import subprocess

import pytest

from scitex_agent_container._hostsync._push_tokens_io import (
    probe_peer_listen_auth,
    read_peer_tokens,
    render_token_read_snippet,
    render_token_write_snippet,
    restart_peer_listen,
    write_peer_token,
)
from scitex_agent_container._state.host_config import PeerSpec

_PEERS = {"spartan": PeerSpec(name="spartan", ssh="spartan")}

_SHA_A = "a" * 64
_SHA_B = "b" * 64


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


def _read_block(
    hostname: str = "spartan-login1",
    listen: tuple[tuple[str, str], ...] = (("listen-spartan-login1.token", _SHA_A),),
    peer: tuple[tuple[str, str], ...] = (("master-x.token", _SHA_B),),
) -> str:
    lines = [f"SAC_PUSHTOK hostname={hostname}"]
    lines += [f"SAC_PUSHTOK listen={n} {d}" for n, d in listen]
    lines += [f"SAC_PUSHTOK peer={n} {d}" for n, d in peer]
    lines.append("SAC_PUSHTOK end")
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# snippets
# ---------------------------------------------------------------------------


def test_read_snippet_expands_home_on_the_peer():
    # Arrange — a locally expanded ~ would be the MASTER's home.
    # Act
    snippet = render_token_read_snippet()
    # Assert
    assert "$HOME/.scitex/agent-container/tokens/listen-*.token" in snippet


def test_read_snippet_digests_on_the_peer():
    # Arrange — values must never cross the wire, so the peer digests.
    # Act
    snippet = render_token_read_snippet()
    # Assert
    assert "sha256sum" in snippet


def test_read_snippet_falls_back_for_macos():
    # Arrange — mba has no coreutils sha256sum, only `shasum -a 256`.
    # Act
    snippet = render_token_read_snippet()
    # Assert
    assert "shasum -a 256" in snippet


def test_read_snippet_never_emits_a_token_body():
    # Arrange — base64-ing the file (what the CONFIG probe does) would
    # haul the secret into this process. The token probe must not.
    # Act
    snippet = render_token_read_snippet()
    # Assert
    assert "base64" not in snippet


def test_write_snippet_lands_via_atomic_mv():
    # Arrange
    # (a dropped connection must never leave a half-written token)
    # Act
    snippet = render_token_write_snippet(["tokens/listen-spartan.token"])
    # Assert
    assert 'mv "$p.tmp" "$p"' in snippet


def test_write_snippet_sets_a_private_umask():
    # Arrange
    # Act
    snippet = render_token_write_snippet(["tokens/listen-spartan.token"])
    # Assert
    assert "umask 077" in snippet


def test_write_snippet_chmods_the_token_private():
    # Arrange — belt AND braces: umask governs creation, chmod pins it.
    # Act
    snippet = render_token_write_snippet(["tokens/listen-spartan.token"])
    # Assert
    assert 'chmod 600 "$p.tmp"' in snippet


def test_write_snippet_reads_the_value_from_stdin():
    # Arrange — the value must never appear in the snippet (= the argv).
    # Act
    snippet = render_token_write_snippet(["tokens/listen-spartan.token"])
    # Assert
    assert "v=$(cat)" in snippet


def test_write_snippet_seeds_every_requested_path():
    # Arrange — the FQDN fix: one value, both candidate paths.
    paths = ["tokens/listen-spartan-login1.token", "tokens/listen-spartan.token"]
    # Act
    snippet = render_token_write_snippet(paths)
    # Assert
    assert all(f'p="$HOME/.scitex/agent-container/{p}"' in snippet for p in paths)


def test_write_snippet_with_stamp_backs_up_first():
    # Arrange
    # Act
    snippet = render_token_write_snippet(
        ["tokens/listen-spartan.token"], backup_stamp="20260716T080000Z"
    )
    # Assert
    assert 'cp -p "$p" "$p.pre-rotate-20260716T080000Z"' in snippet


def test_write_snippet_rejects_a_path_that_could_break_quoting():
    # Arrange — a path carrying $ would be expanded by the remote shell.
    unsafe = "tokens/$(whoami).token"
    # Act
    # Assert — the raise IS the assertion.
    with pytest.raises(ValueError):
        render_token_write_snippet([unsafe])


def test_write_snippet_rejects_an_empty_path_list():
    # Arrange — a write with no destination is a caller bug, not a no-op.
    # Act
    # Assert
    with pytest.raises(ValueError):
        render_token_write_snippet([])


# ---------------------------------------------------------------------------
# read_peer_tokens
# ---------------------------------------------------------------------------


def test_read_returns_the_peer_hostname():
    # Arrange — WHICH listen-<host>.token the peer reads hinges on this.
    runner = _ScriptedRunner(_proc(stdout=_read_block(hostname="spartan-login2")))
    # Act
    remote = read_peer_tokens("spartan", _PEERS, runner=runner)
    # Assert
    assert remote.hostname == "spartan-login2"


def test_read_returns_listen_token_digests():
    # Arrange
    runner = _ScriptedRunner(_proc(stdout=_read_block()))
    # Act
    remote = read_peer_tokens("spartan", _PEERS, runner=runner)
    # Assert
    assert remote.listen_tokens == {"listen-spartan-login1.token": _SHA_A}


def test_read_returns_peer_token_digests():
    # Arrange
    runner = _ScriptedRunner(_proc(stdout=_read_block()))
    # Act
    remote = read_peer_tokens("spartan", _PEERS, runner=runner)
    # Assert
    assert remote.peer_tokens == {"master-x.token": _SHA_B}


def test_read_collects_every_listen_token_file():
    # Arrange — the FQDN hazard made visible: two login nodes, two files.
    block = _read_block(
        listen=(
            ("listen-spartan-login1.token", _SHA_A),
            ("listen-spartan-login2.token", _SHA_B),
        )
    )
    runner = _ScriptedRunner(_proc(stdout=block))
    # Act
    remote = read_peer_tokens("spartan", _PEERS, runner=runner)
    # Assert
    assert len(remote.listen_tokens) == 2


def test_read_without_end_marker_is_not_ok():
    # Arrange — a truncated probe told us NOTHING.
    runner = _ScriptedRunner(_proc(stdout="", rc=255, stderr="connect refused\n"))
    # Act
    remote = read_peer_tokens("spartan", _PEERS, runner=runner)
    # Assert
    assert not remote.ok


def test_read_without_a_hostname_is_not_ok():
    # Arrange — no hostname means we cannot say which token file the
    # peer's listen reads; that is UNKNOWN, not "no tokens".
    runner = _ScriptedRunner(_proc(stdout="SAC_PUSHTOK end\n"))
    # Act
    remote = read_peer_tokens("spartan", _PEERS, runner=runner)
    # Assert
    assert not remote.ok


def test_read_with_no_token_files_is_ok_and_empty():
    # Arrange — a finished probe listing nothing is a REAL state (the
    # peer has no tokens), distinct from a dead transport.
    runner = _ScriptedRunner(_proc(stdout=_read_block(listen=(), peer=())))
    # Act
    remote = read_peer_tokens("spartan", _PEERS, runner=runner)
    # Assert
    assert remote.ok and not remote.listen_tokens


def test_read_timeout_degrades_to_not_ok():
    # Arrange — a real TimeoutExpired, raised by the scripted callable.
    runner = _ScriptedRunner(subprocess.TimeoutExpired(cmd=["ssh"], timeout=30))
    # Act
    remote = read_peer_tokens("spartan", _PEERS, runner=runner)
    # Assert
    assert not remote.ok


def test_read_unknown_peer_is_not_ok():
    # Arrange
    runner = _ScriptedRunner()
    # Act
    remote = read_peer_tokens("ghost", {}, runner=runner)
    # Assert
    assert not remote.ok


def test_read_dispatches_through_ssh_argv():
    # Arrange
    runner = _ScriptedRunner(_proc(stdout=_read_block()))
    # Act
    read_peer_tokens("spartan", _PEERS, runner=runner)
    # Assert — the choke point: argv is an ssh invocation of the peer.
    assert runner.calls[0][0][0] == "ssh"


# ---------------------------------------------------------------------------
# write_peer_token — the value must ride stdin, never the argv
# ---------------------------------------------------------------------------


def test_write_pipes_the_value_via_stdin():
    # Arrange
    runner = _ScriptedRunner(_proc())
    # Act
    write_peer_token(
        "spartan", _PEERS, "s3cret-value", ["tokens/x.token"], runner=runner
    )
    # Assert
    assert runner.calls[0][1]["input"] == "s3cret-value"


def test_write_never_puts_the_value_in_the_argv():
    # Arrange — an argv token is readable via `ps` by every user on the
    # peer for the life of the command.
    runner = _ScriptedRunner(_proc())
    # Act
    write_peer_token(
        "spartan", _PEERS, "s3cret-value", ["tokens/x.token"], runner=runner
    )
    # Assert
    assert "s3cret-value" not in " ".join(runner.calls[0][0])


def test_write_refuses_an_empty_value():
    # Arrange — writing "" would silently brick the peer's auth.
    runner = _ScriptedRunner()
    # Act
    ok, _detail = write_peer_token(
        "spartan", _PEERS, "", ["tokens/x.token"], runner=runner
    )
    # Assert
    assert not ok


def test_write_reports_failure_on_nonzero_exit():
    # Arrange
    runner = _ScriptedRunner(_proc(rc=1, stderr="disk full\n"))
    # Act
    ok, _detail = write_peer_token(
        "spartan", _PEERS, "v", ["tokens/x.token"], runner=runner
    )
    # Assert
    assert not ok


def test_write_failure_detail_carries_the_remote_stderr():
    # Arrange
    runner = _ScriptedRunner(_proc(rc=1, stderr="disk full\n"))
    # Act
    _ok, detail = write_peer_token(
        "spartan", _PEERS, "v", ["tokens/x.token"], runner=runner
    )
    # Assert
    assert "disk full" in detail


# ---------------------------------------------------------------------------
# restart_peer_listen
# ---------------------------------------------------------------------------


def test_restart_dispatches_the_peers_own_verb():
    # Arrange — never a re-implemented stop/start: the peer owns that.
    runner = _ScriptedRunner(_proc())
    # Act
    restart_peer_listen("spartan", _PEERS, runner=runner)
    # Assert
    assert runner.calls[0][0][-3:] == ["sac", "listen", "restart"]


def test_restart_reports_failure_on_nonzero_exit():
    # Arrange
    runner = _ScriptedRunner(_proc(rc=1, stderr="ERROR: port still held\n"))
    # Act
    ok, _detail = restart_peer_listen("spartan", _PEERS, runner=runner)
    # Assert
    assert not ok


def test_restart_failure_names_the_real_cause():
    # Arrange
    runner = _ScriptedRunner(_proc(rc=1, stderr="ERROR: port still held by PID 9\n"))
    # Act
    _ok, detail = restart_peer_listen("spartan", _PEERS, runner=runner)
    # Assert
    assert "port still held by PID 9" in detail


# ---------------------------------------------------------------------------
# probe_peer_listen_auth — the falsifiable verification
# ---------------------------------------------------------------------------


def _status(code: int) -> str:
    return f"\nSAC_PUSHTOK status={code}\n"


def test_probe_reports_the_listens_status():
    # Arrange
    runner = _ScriptedRunner(_proc(stdout=_status(200)))
    # Act
    probe = probe_peer_listen_auth("spartan", _PEERS, bearer="tok", runner=runner)
    # Assert
    assert probe.status == 200


def test_probe_never_puts_the_bearer_in_the_argv():
    # Arrange — the whole point of the --config seam.
    runner = _ScriptedRunner(_proc(stdout=_status(200)))
    # Act
    probe_peer_listen_auth("spartan", _PEERS, bearer="s3cret-bearer", runner=runner)
    # Assert
    assert "s3cret-bearer" not in " ".join(runner.calls[0][0])


def test_probe_passes_the_bearer_on_stdin():
    # Arrange
    runner = _ScriptedRunner(_proc(stdout=_status(200)))
    # Act
    probe_peer_listen_auth("spartan", _PEERS, bearer="s3cret-bearer", runner=runner)
    # Assert
    assert "s3cret-bearer" in runner.calls[0][1]["input"]


def test_probe_hits_an_authenticated_route_not_health():
    # Arrange — /v1/health is PUBLIC: it answers 200 to any bearer, so a
    # probe against it could not disagree with us.
    runner = _ScriptedRunner(_proc(stdout=_status(200)))
    # Act
    probe_peer_listen_auth("spartan", _PEERS, bearer="tok", runner=runner)
    # Assert
    assert "/v1/health" not in " ".join(runner.calls[0][0])


def test_probe_reads_403_as_rejected():
    # Arrange — the auth middleware's wrong-token answer.
    runner = _ScriptedRunner(_proc(stdout=_status(403)))
    # Act
    probe = probe_peer_listen_auth("spartan", _PEERS, bearer="tok", runner=runner)
    # Assert
    assert probe.rejected


def test_probe_reads_401_as_rejected():
    # Arrange — the missing-bearer answer.
    runner = _ScriptedRunner(_proc(stdout=_status(401)))
    # Act
    probe = probe_peer_listen_auth("spartan", _PEERS, bearer="tok", runner=runner)
    # Assert
    assert probe.rejected


def test_probe_reads_404_as_accepted():
    # Arrange — auth runs OUTSIDE the router, so a 404 still proves the
    # bearer cleared the gate. Gating on 200 is the documented mistake.
    runner = _ScriptedRunner(_proc(stdout=_status(404)))
    # Act
    probe = probe_peer_listen_auth("spartan", _PEERS, bearer="tok", runner=runner)
    # Assert
    assert probe.accepted


def test_probe_transport_failure_is_not_a_rejection():
    # Arrange — "I could not ask" must never read as "it said no".
    runner = _ScriptedRunner(_proc(stdout="", rc=255, stderr="refused\n"))
    # Act
    probe = probe_peer_listen_auth("spartan", _PEERS, bearer="tok", runner=runner)
    # Assert
    assert not probe.rejected


def test_probe_transport_failure_is_not_an_acceptance():
    # Arrange — nor may it read as "it said yes".
    runner = _ScriptedRunner(_proc(stdout="", rc=255, stderr="refused\n"))
    # Act
    probe = probe_peer_listen_auth("spartan", _PEERS, bearer="tok", runner=runner)
    # Assert
    assert not probe.accepted


def test_probe_timeout_degrades_to_unknown():
    # Arrange
    runner = _ScriptedRunner(subprocess.TimeoutExpired(cmd=["ssh"], timeout=30))
    # Act
    probe = probe_peer_listen_auth("spartan", _PEERS, bearer="tok", runner=runner)
    # Assert
    assert probe.status == -1
