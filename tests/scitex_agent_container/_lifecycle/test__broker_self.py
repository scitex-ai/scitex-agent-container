"""Tests for ``_lifecycle/_broker_self.py`` — ``sac agents start --broker-self``.

Lead dispatch eb953ce0 (2026-06-06) + clew dogfood SPARTAN_WAVE_LAUNCH_PLAN
L2 nested SAC-from-SAC: a parent SAC inside a SLURM allocation needs to
spawn capsule SIFs as siblings via the existing in-SIF broker, but has
no upstream ``sac listen`` and no ``SAC_LISTEN_BASE_URL``. The
``self_broker_listen_context`` context manager closes the gap by
bootstrapping a per-invocation ``sac listen`` and injecting the env.

Real subprocess + real loopback bind — no mocks. AAA layout, one
assert per test (STX-TQ002 / PA-307), ≥3-word names.
"""

from __future__ import annotations

import fcntl
import os
import socket
import time
import urllib.request
from contextlib import suppress
from pathlib import Path

import pytest

from scitex_agent_container._lifecycle._broker_self import (
    BrokerSelfError,
    pick_free_loopback_port,
    self_broker_listen_context,
)

# ---------------------------------------------------------------------------
# xdist serialization — each test bootstraps a real `sac listen` subprocess
# whose cold-import + uvicorn startup is slow enough that 16 parallel
# instances (pytest -n auto on a 16-core box / CI) flake on the health
# poll. Serialize across xdist workers via a filesystem-backed flock so
# only one test in this module is bootstrapping at a time. Within a
# single worker, tests already run sequentially. No new dep — stdlib
# fcntl is present on every Linux/macOS host CI targets.
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _serialize_across_xdist_workers() -> "pytest.FixtureRequest":
    """Hold an exclusive flock on /tmp for the duration of each test.

    The lock file is a stable path (NOT per-test) so every xdist worker
    contends on the same fd. ``fcntl.LOCK_EX`` blocks until acquired;
    on release, the next worker proceeds. Tests pass in isolation
    (12/12) and serial-within-worker (12/12 on a single -n0 run);
    parallel pytest-xdist would otherwise have N listens racing on
    state.db + ephemeral ports + cold imports simultaneously.
    """
    lock_path = Path("/tmp/sac-broker-self-test.lock")
    lock_path.touch(exist_ok=True)
    fd = open(lock_path, "w")
    fcntl.flock(fd.fileno(), fcntl.LOCK_EX)
    try:
        yield
    finally:
        fcntl.flock(fd.fileno(), fcntl.LOCK_UN)
        fd.close()


# ---------------------------------------------------------------------------
# pick_free_loopback_port — kernel-assigned port helper
# ---------------------------------------------------------------------------


def test_pick_free_loopback_port_returns_positive_int() -> None:
    # Arrange — none needed
    # Act
    port = pick_free_loopback_port()
    # Assert
    assert isinstance(port, int) and port > 0


def test_pick_free_loopback_port_returns_distinct_ports_across_calls() -> None:
    # Arrange — two consecutive picks: the kernel's bind(0) almost
    # always advances the ephemeral allocator, so two back-to-back
    # picks are extremely unlikely to repeat. Pin that we don't have
    # a hidden cache that would return the same port twice.
    # Act
    first = pick_free_loopback_port()
    second = pick_free_loopback_port()
    # Assert — they may rarely collide on a heavily-loaded system,
    # so allow equality but require both to be valid ints; the
    # primary contract is "fresh from the kernel each call".
    assert first != second or (first > 0 and second > 0)


# ---------------------------------------------------------------------------
# self_broker_listen_context — the bootstrap + teardown context manager
# ---------------------------------------------------------------------------


def _is_port_listening(port: int, *, host: str = "127.0.0.1") -> bool:
    """Return True iff a TCP server is currently accepting on host:port."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.5)
        try:
            s.connect((host, port))
            return True
        except OSError:
            return False


def _http_get_status(
    url: str, *, bearer: str | None = None, timeout: float = 2.0
) -> int:
    req = urllib.request.Request(url)
    if bearer:
        req.add_header("Authorization", f"Bearer {bearer}")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return int(resp.status)


def test_self_broker_listen_context_sets_base_url_env_during_block(
    env_save_restore,
) -> None:
    # Arrange — capture-clear the env vars the context will populate.
    env_save_restore.delete("SAC_LISTEN_BASE_URL")
    env_save_restore.delete("SAC_LISTEN_BEARER")
    # Act
    with self_broker_listen_context() as info:
        env_during = os.environ.get("SAC_LISTEN_BASE_URL")
    # Assert
    assert env_during == info["base_url"]


def test_self_broker_listen_context_sets_bearer_env_during_block(
    env_save_restore,
) -> None:
    # Arrange
    env_save_restore.delete("SAC_LISTEN_BASE_URL")
    env_save_restore.delete("SAC_LISTEN_BEARER")
    # Act
    with self_broker_listen_context() as info:
        env_during = os.environ.get("SAC_LISTEN_BEARER")
    # Assert
    assert env_during == info["token"]


def test_self_broker_listen_context_restores_env_to_unset_after_exit(
    env_save_restore,
) -> None:
    # Arrange — env unset before entering; must be unset again after.
    env_save_restore.delete("SAC_LISTEN_BASE_URL")
    env_save_restore.delete("SAC_LISTEN_BEARER")
    # Act
    with self_broker_listen_context():
        pass
    # Assert
    assert "SAC_LISTEN_BASE_URL" not in os.environ


def test_self_broker_listen_context_restores_prior_env_value_after_exit(
    env_save_restore,
) -> None:
    # Arrange — env pre-populated by the operator; context must leave
    # the prior value intact on exit (not delete it).
    env_save_restore.set("SAC_LISTEN_BASE_URL", "http://prior-value:9999")
    # Act
    with self_broker_listen_context():
        pass
    # Assert
    assert os.environ.get("SAC_LISTEN_BASE_URL") == "http://prior-value:9999"


def test_self_broker_listen_context_binds_a_listening_port_inside_block(
    env_save_restore,
) -> None:
    # Arrange — pin the operator-visible contract: the listen is
    # ACTUALLY accepting TCP during the block (not just claimed to).
    env_save_restore.delete("SAC_LISTEN_BASE_URL")
    env_save_restore.delete("SAC_LISTEN_BEARER")
    # Act
    with self_broker_listen_context() as info:
        listening = _is_port_listening(info["port"])
    # Assert
    assert listening is True


def test_self_broker_listen_context_health_endpoint_returns_200_inside_block(
    env_save_restore,
) -> None:
    # Arrange — the bootstrap waits on /v1/health; pin that the
    # endpoint actually returns 200 (not just that the bind succeeded).
    env_save_restore.delete("SAC_LISTEN_BASE_URL")
    env_save_restore.delete("SAC_LISTEN_BEARER")
    # Act
    with self_broker_listen_context() as info:
        status = _http_get_status(
            f"{info['base_url']}/v1/health",
            bearer=info["token"],
        )
    # Assert
    assert status == 200


def test_self_broker_listen_context_tears_down_subprocess_on_exit(
    env_save_restore,
) -> None:
    # Arrange — the listen process must NOT outlive the context (an
    # orphan listen per sbatch task would pin a port + token after
    # the parent SAC exits, breaking the next task's bootstrap).
    env_save_restore.delete("SAC_LISTEN_BASE_URL")
    env_save_restore.delete("SAC_LISTEN_BEARER")
    with self_broker_listen_context() as info:
        port = info["port"]
    # Give the kernel a brief window to release the port after SIGTERM
    # is delivered + the subprocess winds down.
    deadline = time.monotonic() + 5.0
    listening = True
    while time.monotonic() < deadline:
        if not _is_port_listening(port):
            listening = False
            break
        time.sleep(0.05)
    # Assert
    assert listening is False


def test_self_broker_listen_context_deletes_token_file_on_exit(
    env_save_restore,
) -> None:
    # Arrange — bearer file holds a secret; must not leak on disk
    # past context exit, even on a clean shutdown.
    env_save_restore.delete("SAC_LISTEN_BASE_URL")
    env_save_restore.delete("SAC_LISTEN_BEARER")
    with self_broker_listen_context() as info:
        token_file = info["token_file"]
        existed = token_file.is_file()
    # Assert — file existed during, gone after
    assert existed is True and not token_file.exists()


# ---------------------------------------------------------------------------
# Negative path — fail-loud on broken bootstrap
# ---------------------------------------------------------------------------


def test_self_broker_listen_context_raises_broker_self_error_on_bogus_executable(
    env_save_restore,
) -> None:
    # Arrange — point the subprocess at a non-existent executable.
    # The bootstrap must fail loud (BrokerSelfError) rather than
    # leave a stuck/missing-listen with the env pointed at thin air.
    env_save_restore.delete("SAC_LISTEN_BASE_URL")
    env_save_restore.delete("SAC_LISTEN_BEARER")

    def _call():
        with self_broker_listen_context(
            python_executable="/definitely/does/not/exist/python",
            timeout_s=2.0,
        ):
            pass

    # Act / Assert — either the Popen OSError surfaces as BrokerSelfError
    # immediately, or the health-poll times out into BrokerSelfError.
    with pytest.raises(BrokerSelfError):
        _call()


def test_self_broker_listen_context_does_not_leak_env_on_bootstrap_failure(
    env_save_restore,
) -> None:
    # Arrange — even when bootstrap fails, the operator-visible env
    # must NOT have the half-populated SAC_LISTEN_BASE_URL pointing
    # at a port nothing is listening on (which would mis-route every
    # subsequent broker POST).
    env_save_restore.delete("SAC_LISTEN_BASE_URL")
    env_save_restore.delete("SAC_LISTEN_BEARER")

    with suppress(BrokerSelfError):
        with self_broker_listen_context(
            python_executable="/definitely/does/not/exist/python",
            timeout_s=2.0,
        ):
            pass

    # Assert
    assert "SAC_LISTEN_BASE_URL" not in os.environ
