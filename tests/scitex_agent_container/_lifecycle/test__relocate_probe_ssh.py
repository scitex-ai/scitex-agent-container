"""An ssh failure is evidence; a transport failure is not, and they must not blur.

Two failures look alike from a distance and mean opposite things:

    the listen daemon could not be reached   -> we learned NOTHING about the
                                                target; every fact must stay
                                                unknown
    ssh ran and could not connect            -> the target did not answer us,
                                                which IS a measurement

This module raises for the first and returns for the second, and these tests pin
that boundary. They also pin the argv shape, because the one-argv-element rule is
not cosmetic: passing ``["sh", "-c", script]`` makes the REMOTE login shell run
only the script's first word with the rest as positional parameters — measured
2026-08-09, where ``echo MARK`` printed an empty line and the rest of the script
ran in a different shell than intended.

The transport seam is a real callable supplied by the test, never a mock: an
``exec_fn`` that returns a canned body is exactly the shape
``request_host_exec`` has.
"""

from __future__ import annotations

import pytest

from scitex_agent_container._lifecycle._relocate_probe_ssh import (
    ProbeTransportError,
    RemoteRun,
    build_probe_argv,
    peer_preamble,
    run_probe_script,
)
from scitex_agent_container._state.host_config import PeerSpec

SCRIPT = 'M=SAC_RELOC\necho "$M begin"\necho "$M end"\n'


@pytest.fixture
def peers():
    """A peers map in the shape ``config.yaml`` parses into."""
    yield {
        "hop": PeerSpec(name="hop", ssh="user@hop"),
        "far": PeerSpec(name="far", ssh="far.internal", via=("hop",)),
        "venv-host": PeerSpec(
            name="venv-host",
            ssh="venv-host",
            env_preamble=('export PATH="$HOME/.env-sac/bin:$PATH"',),
        ),
    }


def _exec_returning(body):
    """A real ``request_host_exec``-shaped callable that returns ``body``."""

    def exec_fn(argv, *, timeout_s=None, **kwargs):
        return body

    return exec_fn


def _exec_raising(exc):
    def exec_fn(argv, *, timeout_s=None, **kwargs):
        raise exc

    return exec_fn


# ---------------------------------------------------------------------------
# argv shape
# ---------------------------------------------------------------------------


def test_the_script_is_a_single_argv_element(peers) -> None:
    # Arrange: split across elements, ssh joins them and the remote shell runs
    # only the first word — silently, with the rest as positional parameters.
    argv = build_probe_argv("hop", SCRIPT, peers)
    # Act
    last = argv[-1]
    # Assert
    assert last == SCRIPT


def test_the_peers_ssh_target_is_used_rather_than_the_key(peers) -> None:
    # Arrange
    argv = build_probe_argv("hop", SCRIPT, peers)
    # Act
    target = argv[-2]
    # Assert
    assert target == "user@hop"


def test_a_multi_hop_peer_gets_its_proxy_jump_chain(peers) -> None:
    # Arrange
    argv = build_probe_argv("far", SCRIPT, peers)
    # Act
    jump = argv[argv.index("-J") + 1]
    # Assert
    assert jump == "user@hop"


def test_an_unregistered_host_is_still_probed_by_name(peers) -> None:
    # Arrange: scitex-nas-01 and -02 are reached through ~/.ssh/config today and
    # are absent from config.yaml. Refusing to probe them would turn a
    # measurable fact into an unknown for a bookkeeping reason.
    argv = build_probe_argv("scitex-nas-01", SCRIPT, peers)
    # Act
    target = argv[-2]
    # Assert
    assert target == "scitex-nas-01"


def test_batch_mode_is_forced(peers) -> None:
    # Arrange: there is no terminal at the far end of a listen-daemon exec, so a
    # password prompt would hang until the timeout instead of failing.
    argv = build_probe_argv("hop", SCRIPT, peers)
    # Act
    batch = "BatchMode=yes" in argv
    # Assert
    assert batch is True


def test_an_empty_host_is_refused_rather_than_guessed(peers) -> None:
    # Arrange
    script = SCRIPT

    # Act
    def run() -> list[str]:
        return build_probe_argv("", script, peers)

    # Assert
    with pytest.raises(ValueError):
        run()


def test_the_peer_preamble_is_read_from_the_config(peers) -> None:
    # Arrange: without it `sac` is off PATH on scitex-compute-03, and the two
    # facts only the target's own validator can answer go unanswered there.
    preamble = peer_preamble("venv-host", peers)
    # Act
    exported = preamble
    # Assert
    assert exported == 'export PATH="$HOME/.env-sac/bin:$PATH"'


def test_a_host_with_no_preamble_gets_an_empty_one(peers) -> None:
    # Arrange
    preamble = peer_preamble("hop", peers)
    # Act
    empty = preamble
    # Assert
    assert empty == ""


# ---------------------------------------------------------------------------
# transport failure vs ssh failure
# ---------------------------------------------------------------------------


def test_a_reachable_target_returns_its_stdout(peers) -> None:
    # Arrange
    body = {"exit_code": 0, "stdout": "SAC_RELOC begin\n", "stderr": ""}
    run = run_probe_script("hop", SCRIPT, exec_fn=_exec_returning(body), peers=peers)
    # Act
    stdout = run.stdout
    # Assert
    assert stdout == "SAC_RELOC begin\n"


def test_an_unreachable_listen_daemon_raises_rather_than_returning(peers) -> None:
    # Arrange: a returned "empty result" here would read downstream as "the
    # target answered nothing", which is a different and false claim.
    exec_fn = _exec_raising(OSError("connection refused"))

    # Act
    def run() -> RemoteRun:
        return run_probe_script("hop", SCRIPT, exec_fn=exec_fn, peers=peers)

    # Assert
    with pytest.raises(ProbeTransportError):
        run()


def test_a_timed_out_exec_raises_rather_than_returning_a_blank(peers) -> None:
    # Arrange
    body = {"exit_code": 124, "stdout": "", "stderr": "", "timed_out": True}

    # Act
    def run() -> RemoteRun:
        return run_probe_script(
            "hop", SCRIPT, exec_fn=_exec_returning(body), peers=peers
        )

    # Assert
    with pytest.raises(ProbeTransportError):
        run()


def test_a_body_without_an_exit_code_raises(peers) -> None:
    # Arrange: a malformed body is a transport we cannot interpret, not a
    # target that failed.
    exec_fn = _exec_returning({"stdout": "x"})

    # Act
    def run() -> RemoteRun:
        return run_probe_script("hop", SCRIPT, exec_fn=exec_fn, peers=peers)

    # Assert
    with pytest.raises(ProbeTransportError):
        run()


def test_an_ssh_connection_failure_is_returned_not_raised(peers) -> None:
    # Arrange: ssh ran and could not connect. That is a measurement about the
    # target, and the caller is entitled to use it.
    body = {"exit_code": 255, "stdout": "", "stderr": "ssh: connect: timed out"}
    run = run_probe_script("hop", SCRIPT, exec_fn=_exec_returning(body), peers=peers)
    # Act
    failed = run.ssh_failed
    # Assert
    assert failed is True


def test_a_script_that_exits_non_zero_is_not_an_ssh_failure(peers) -> None:
    # Arrange: the remote script's own status must not be mistaken for ssh's.
    body = {"exit_code": 1, "stdout": "SAC_RELOC begin\n", "stderr": ""}
    run = run_probe_script("hop", SCRIPT, exec_fn=_exec_returning(body), peers=peers)
    # Act
    failed = run.ssh_failed
    # Assert
    assert failed is False
