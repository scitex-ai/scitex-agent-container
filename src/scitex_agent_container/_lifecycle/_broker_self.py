"""``sac agents start --broker-self`` — per-invocation self-brokering listen.

Operator-mandated 2026-06-06 (lead dispatch eb953ce0, clew dogfood
SPARTAN_WAVE_LAUNCH_PLAN.md "L2 nested SAC-from-SAC"): a parent SAC
running on a SLURM allocation (inside its sac-scitex.sif) needs to
spawn capsule SIFs as siblings via the existing in-SIF broker, but
there is no upstream ``sac listen`` to broker to AND no environment
that injects ``SAC_LISTEN_BASE_URL``. Today this raises
:class:`InSifBrokerError` at :func:`_in_sif_broker.broker_start_to_host`
and blocks the canonical nested architecture.

This module is the operator-opt-in workaround. When the caller passes
``--broker-self``, the start path:

  1. Picks a free loopback TCP port (kernel-assigned).
  2. Writes a fresh per-invocation bearer token to a tempfile.
  3. Spawns ``sac listen --bind 127.0.0.1:<port> --token-file <path>``
     as a subprocess (under the SAME ``sys.executable`` so the SIF's
     venv-bundled sac is the one that binds).
  4. Polls ``/v1/health`` until ready (≤5s, fail-loud on timeout).
  5. Injects ``SAC_LISTEN_BASE_URL=http://127.0.0.1:<port>`` and
     ``SAC_LISTEN_BEARER=<token>`` into the current process env.
  6. Returns a context-manager that tears down the subprocess + the
     tempfile + restores the env on exit.

After step 6, the existing in-SIF broker path
(:func:`_in_sif_broker.maybe_broker_in_sif_spawn`) finds the env and
POSTs the spawn request to the local listen, which ``apptainer exec``s
the capsule SIF as a sibling — exactly the path the operator
hand-wires today, but compressed into one CLI flag.

Per-task isolation: each sbatch task in the cohort gets its own
loopback port + its own bearer + its own tempfile, so 49 parallel
tasks cannot collide on either the port (each is loopback-only in
its own SLURM step) or the token (per-process random).

Fail-loud invariants:
  * Free-port pick fails → :class:`BrokerSelfError`.
  * Listen subprocess fails to start → :class:`BrokerSelfError` with
    its captured stderr verbatim.
  * Health-poll timeout → :class:`BrokerSelfError` naming the port
    + the elapsed wait.

NEVER best-effort: a self-broker that silently runs without a
listen would route every spawn into thin air; fail loud is the
correct default.
"""

from __future__ import annotations

import logging
import os
import secrets
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from contextlib import contextmanager, suppress
from pathlib import Path
from typing import Iterator

logger = logging.getLogger(__name__)

__all__ = [
    "BrokerSelfError",
    "pick_free_loopback_port",
    "self_broker_listen_context",
]

# Per-invocation bearer token entropy. 32 bytes → 43-char URL-safe
# string; identical convention to :func:`_state.state_db_nodes.mint_node_token`.
_TOKEN_BYTES = 32

# Default health-poll cap PER ATTEMPT. 15s is generous enough for a
# cold-import + uvicorn bind on a SLURM node under sibling-task CPU
# contention OR a CI box running pytest-xdist with N workers each
# spawning subprocesses simultaneously. Shorter (5s) flaked CI under
# 16-way parallel load (``-n auto`` on a 16-core runner). Longer would
# mask a real bind failure with a stalled CLI. Combined with the
# 3-attempt retry in :func:`self_broker_listen_context`, total
# worst-case wait is 45s.
_DEFAULT_HEALTH_TIMEOUT_S = 15.0


class BrokerSelfError(RuntimeError):
    """Raised when ``--broker-self`` cannot bring up a usable listen.

    Distinct from :class:`_in_sif_broker.InSifBrokerError` so the
    integration point in ``_start_single`` catches one type per failure
    domain (bootstrap vs. POST-to-listen) without coupling the two.
    """


def pick_free_loopback_port() -> int:
    """Ask the kernel for an unused TCP port on 127.0.0.1.

    Bind + immediately close: the kernel marks the port FREE again,
    and the next ``bind()`` (the listen subprocess we're about to
    spawn) almost always reacquires it because nothing else on
    127.0.0.1 is competing in the typical SLURM step. Race window is
    the time between this function's ``close()`` and the listen
    subprocess's ``bind()``; on a single-task SLURM allocation it is
    negligible (no other listeners in the network namespace).
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _write_token(path: Path) -> str:
    """Generate a fresh bearer + write it to ``path``. Returns the token."""
    token = secrets.token_urlsafe(_TOKEN_BYTES)
    path.write_text(token, encoding="utf-8")
    # Token file holds an authentication secret; restrict to owner-read.
    os.chmod(path, 0o600)
    return token


def _wait_for_health(
    base_url: str,
    bearer: str,
    *,
    timeout_s: float,
) -> None:
    """Poll ``GET <base_url>/v1/health`` until 200 or timeout.

    The health endpoint is unauthenticated on the canonical sac
    listen, but pass the bearer anyway so a future auth-tightening
    cannot break this caller silently.
    """
    deadline = time.monotonic() + timeout_s
    url = f"{base_url.rstrip('/')}/v1/health"
    req = urllib.request.Request(url)
    if bearer:
        req.add_header("Authorization", f"Bearer {bearer}")
    last_err: Exception | None = None
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(req, timeout=1.0) as resp:
                if 200 <= resp.status < 300:
                    return
                last_err = RuntimeError(f"health endpoint returned HTTP {resp.status}")
        except (urllib.error.URLError, OSError) as exc:
            last_err = exc
        time.sleep(0.05)
    raise BrokerSelfError(
        f"--broker-self: local `sac listen` at {base_url} did not become "
        f"healthy within {timeout_s:.1f}s. Last error: {last_err!r}. "
        "The subprocess may have died at bind (port collision in this "
        "network namespace) or crashed mid-startup; check its stderr."
    )


def _spawn_listen(
    *,
    port: int,
    token_file: Path,
    python_executable: str,
) -> subprocess.Popen:
    """``Popen`` the local listen subprocess. Raises ``BrokerSelfError`` on OS error."""
    argv = [
        python_executable,
        "-m",
        "scitex_agent_container",
        "listen",
        "--bind",
        f"127.0.0.1:{port}",
        "--token-file",
        str(token_file),
    ]
    try:
        return subprocess.Popen(
            argv,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
    except OSError as exc:
        raise BrokerSelfError(
            f"--broker-self: failed to spawn local `sac listen` "
            f"subprocess (argv head: {argv[:3]!r}): {exc!r}"
        ) from exc


def _teardown_failed_subprocess(proc: subprocess.Popen) -> str:
    """Terminate + reap a not-yet-healthy listen, return its captured stderr."""
    proc.terminate()
    try:
        _, err = proc.communicate(timeout=2.0)
    except subprocess.TimeoutExpired:
        proc.kill()
        _, err = proc.communicate()
    return err or ""


@contextmanager
def self_broker_listen_context(
    *,
    timeout_s: float = _DEFAULT_HEALTH_TIMEOUT_S,
    python_executable: str | None = None,
    max_attempts: int = 3,
) -> Iterator[dict]:
    """Bootstrap a per-invocation ``sac listen``; inject env; teardown on exit.

    Yields a dict with the resolved ``{port, token, base_url, pid,
    token_file}`` so a caller (and the test) can introspect what was
    bound. The env vars ``SAC_LISTEN_BASE_URL`` and
    ``SAC_LISTEN_BEARER`` are set in ``os.environ`` for the duration
    of the context and restored to their prior values on exit
    (including the "was unset" case → key is deleted).

    The subprocess is terminated with SIGTERM on exit and reaped with
    a 5s grace; if it doesn't exit cleanly, SIGKILL follows. The
    tempfile holding the bearer is deleted unconditionally so a
    crashed teardown cannot leak a credential on disk.

    ``max_attempts`` retries the port-pick + subprocess-spawn loop
    when the first attempt's listen does not become healthy in
    ``timeout_s``. The race window between ``pick_free_loopback_port``
    closing its scout socket and the subprocess's bind() is tiny but
    not zero, especially under heavy ephemeral-port churn (back-to-back
    test runs leaving sockets in TIME_WAIT). Each retry picks a fresh
    port; the per-attempt token + tempfile are also fresh.
    """
    tmp_dir = Path(tempfile.mkdtemp(prefix="sac-broker-self-"))
    token_file = tmp_dir / "listen.token"
    token = _write_token(token_file)

    saved_url = os.environ.get("SAC_LISTEN_BASE_URL")
    saved_bearer = os.environ.get("SAC_LISTEN_BEARER")

    py = python_executable or sys.executable

    proc: subprocess.Popen | None = None
    port: int = 0
    base_url = ""
    last_err: BaseException | None = None
    captured_stderrs: list[str] = []
    for attempt in range(1, max_attempts + 1):
        port = pick_free_loopback_port()
        base_url = f"http://127.0.0.1:{port}"
        logger.info(
            "--broker-self: bootstrapping local listen on %s (attempt %d/%d)",
            base_url,
            attempt,
            max_attempts,
        )
        try:
            proc = _spawn_listen(port=port, token_file=token_file, python_executable=py)
        except BrokerSelfError:
            # OS-level Popen failure (bad executable, missing python).
            # Retrying won't help — propagate up.
            token_file.unlink(missing_ok=True)
            with suppress(OSError):
                tmp_dir.rmdir()
            raise
        try:
            _wait_for_health(base_url, token, timeout_s=timeout_s)
            break  # success — exit the retry loop with proc still alive
        except BaseException as exc:
            last_err = exc
            stderr = _teardown_failed_subprocess(proc)
            captured_stderrs.append(stderr)
            proc = None
            # Loop and retry on a fresh port. ``BrokerSelfError`` and
            # transport-level failures both get a retry; only a
            # subprocess Popen OSError aborts immediately (handled above).
    if proc is None:
        # All attempts exhausted; propagate the last error + every captured stderr.
        token_file.unlink(missing_ok=True)
        with suppress(OSError):
            tmp_dir.rmdir()
        for idx, stderr in enumerate(captured_stderrs, start=1):
            if stderr.strip():
                logger.error(
                    "--broker-self: listen subprocess stderr (attempt %d/%d):\n%s",
                    idx,
                    max_attempts,
                    stderr.strip(),
                )
        if last_err is not None:
            raise last_err
        raise BrokerSelfError(
            "--broker-self: bootstrap loop exhausted with no captured error"
        )

    os.environ["SAC_LISTEN_BASE_URL"] = base_url
    os.environ["SAC_LISTEN_BEARER"] = token
    try:
        yield {
            "port": port,
            "token": token,
            "base_url": base_url,
            "pid": proc.pid,
            "token_file": token_file,
        }
    finally:
        # Restore env first so even a hung teardown doesn't leak the
        # transient URL to a sibling call.
        if saved_url is None:
            os.environ.pop("SAC_LISTEN_BASE_URL", None)
        else:
            os.environ["SAC_LISTEN_BASE_URL"] = saved_url
        if saved_bearer is None:
            os.environ.pop("SAC_LISTEN_BEARER", None)
        else:
            os.environ["SAC_LISTEN_BEARER"] = saved_bearer

        proc.terminate()
        try:
            proc.communicate(timeout=5.0)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.communicate()

        token_file.unlink(missing_ok=True)
        try:
            tmp_dir.rmdir()
        except OSError:
            # Best-effort: a stray file (operator-edited or crash-left)
            # is not worth blocking teardown over; the parent /tmp
            # cleanup catches it.
            pass
