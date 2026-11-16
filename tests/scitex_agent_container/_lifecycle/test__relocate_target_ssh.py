"""Relocation challenge sends use the installed CLI and preserve its refusal."""

import json
import os
import shutil
import stat
import subprocess
import sys
import uuid
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread

import pytest

from scitex_agent_container._lifecycle._relocate_shell import Shell, marked
from scitex_agent_container._lifecycle._relocate_target_ssh import (
    MARK_BOOT,
    deliver_prompt,
)


@contextmanager
def _owned_delivery(tmp_path, status, *, missing_tool=None):
    calls = []
    staged_modes = []
    traces = []
    staging = tmp_path / "private staging"
    staging.mkdir()

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            calls.append((self.path, body))
            staged_modes.extend(
                stat.S_IMODE(path.stat().st_mode)
                for path in staging.glob("sac-relocate-brief.*")
            )
            payload = json.dumps(
                {"reply": "owned reply", "error": "owned refusal"}
            ).encode()
            self.send_response(status)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    agent = "owned-handshake-" + uuid.uuid4().hex
    payload_path = Path("/tmp") / f"sac-relocate-brief-{agent}.txt"
    payload_path.touch(exist_ok=False)
    payload_path.chmod(0o600)
    env = {
        "PATH": os.pathsep.join([str(Path(sys.executable).parent), "/usr/bin", "/bin"]),
        "HOME": str(tmp_path / "home"),
        "SCITEX_DIR": str(tmp_path / "state"),
        "SAC_AGENT_SCOPE": "user",
        "SAC_LISTEN_BASE_URL": f"http://127.0.0.1:{server.server_port}",
        "SAC_LISTEN_BEARER": "owned-token",
        "PYTHONNOUSERSITE": "1",
        "TMPDIR": str(staging),
    }
    if missing_tool is not None:
        bin_dir = tmp_path / "tools"
        bin_dir.mkdir()
        for tool in ("sh", "mktemp", "base64", "cat", "rm", "sac"):
            if tool != missing_tool:
                target = (
                    Path(sys.executable).parent / tool
                    if tool == "sac"
                    else Path(shutil.which(tool))
                )
                (bin_dir / tool).symlink_to(target)
        env["PATH"] = str(bin_dir)
    for key in ("COVERAGE_PROCESS_START", "COVERAGE_FILE"):
        if key in os.environ:
            env[key] = os.environ[key]

    def execute(argv, *, timeout_s):
        try:
            result = subprocess.run(
                [argv[0], "-x", *argv[1:]],
                env=env,
                text=True,
                capture_output=True,
                timeout=min(timeout_s, 10),
            )
        except subprocess.TimeoutExpired as exc:
            exc.add_note(
                "Owned relocation shell captured tails (4096 bytes per stream): "
                f"stdout={exc.stdout[-4096:] if exc.stdout else b''!r}; "
                f"stderr={exc.stderr[-4096:] if exc.stderr else b''!r}"
            )
            raise
        traces.append(result.stdout + result.stderr)
        return {
            "exit_code": result.returncode,
            "stdout": result.stdout,
            "stderr": result.stderr,
        }

    execute.staged_modes = staged_modes
    execute.traces = traces

    try:
        yield agent, execute, calls
    finally:
        payload_path.unlink(missing_ok=True)
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


@pytest.mark.parametrize(("http_status", "exit_code"), [(200, 0), (403, 1)])
def test_challenge_send_reaches_cli_and_preserves_its_exit(
    tmp_path, http_status, exit_code
):
    # Arrange
    with _owned_delivery(tmp_path, http_status) as (agent, execute, calls):
        shell = Shell("owned-local", is_local=True)

        # Act
        result = deliver_prompt(shell, agent, "bounded challenge", exec_fn=execute)

    # Assert
    assert (calls, result.exit_code, marked(result, MARK_BOOT)) == (
        [(f"/agents/{agent}/send", {"prompt": "bounded challenge"})],
        exit_code,
        [str(exit_code)],
    )


def test_challenge_text_remains_literal_across_the_shell(tmp_path):
    # Arrange
    marker = tmp_path / "must-not-exist"
    prompt = f"line one\n'quoted' $(touch {marker}) `touch {marker}`"
    with _owned_delivery(tmp_path, 200) as (agent, execute, calls):
        shell = Shell("owned-local", is_local=True)

        # Act
        result = deliver_prompt(shell, agent, prompt, exec_fn=execute)

    # Assert
    assert (result.exit_code, calls, marker.exists()) == (
        0,
        [(f"/agents/{agent}/send", {"prompt": prompt})],
        False,
    )


def test_predictable_payload_symlink_cannot_overwrite_another_file(tmp_path):
    # Arrange
    canary = tmp_path / "owned canary"
    canary.write_bytes(b"retained bytes")
    with _owned_delivery(tmp_path, 200) as (agent, execute, calls):
        legacy = Path("/tmp") / f"sac-relocate-brief-{agent}.txt"
        legacy.unlink()
        legacy.symlink_to(canary)

        # Act
        result = deliver_prompt(
            Shell("owned-local", is_local=True),
            agent,
            "bounded challenge",
            exec_fn=execute,
        )
        snapshot = (canary.read_bytes(), legacy.is_symlink())

    # Assert
    assert (result.exit_code, len(calls), snapshot) == (0, 1, (b"retained bytes", True))


@pytest.mark.parametrize(("http_status", "exit_code"), [(200, 0), (403, 1)])
def test_private_payload_is_removed_after_the_cli_returns(
    tmp_path, http_status, exit_code
):
    # Arrange
    with _owned_delivery(tmp_path, http_status) as (agent, execute, calls):
        # Act
        result = deliver_prompt(
            Shell("owned-local", is_local=True),
            agent,
            "bounded challenge",
            exec_fn=execute,
        )
        remaining = list((tmp_path / "private staging").iterdir())

    # Assert
    assert (result.exit_code, len(calls), execute.staged_modes, remaining) == (
        exit_code,
        1,
        [0o600],
        [],
    )


@pytest.mark.parametrize("missing_tool", ["mktemp", "base64", "cat", "rm"])
def test_staging_failure_stops_before_cli_invocation(tmp_path, missing_tool):
    # Arrange
    with _owned_delivery(tmp_path, 200, missing_tool=missing_tool) as (
        agent,
        execute,
        calls,
    ):
        # Act
        result = deliver_prompt(
            Shell("owned-local", is_local=True),
            agent,
            "bounded challenge",
            exec_fn=execute,
        )
        remaining = list((tmp_path / "private staging").iterdir())

    # Assert
    assert (
        result.exit_code != 0,
        calls,
        any("sac agents send" in trace for trace in execute.traces),
        remaining,
    ) == (
        True,
        [],
        False,
        [],
    )


def test_payload_write_failure_stops_before_cli_invocation(tmp_path):
    # Arrange
    shell = Shell(
        "owned-local",
        is_local=True,
        preamble="ulimit -c 0; trap '' XFSZ; ulimit -f 0",
    )
    with _owned_delivery(tmp_path, 200) as (agent, execute, calls):
        # Act
        result = deliver_prompt(shell, agent, "bounded challenge", exec_fn=execute)
        trace = "\n".join(execute.traces)
        remaining = list((tmp_path / "private staging").iterdir())

    # Assert
    assert (
        result.exit_code != 0,
        calls,
        "base64 -d" in trace,
        "sac agents send" in trace,
        remaining,
    ) == (True, [], True, False, [])


def test_owned_shell_timeout_retains_captured_diagnostics(tmp_path):
    # Arrange
    with _owned_delivery(tmp_path, 200) as (_agent, execute, calls):
        command = [
            "sh",
            "-c",
            "printf owned-output; printf owned-error >&2; exec sleep 30",
        ]

        # Act
        try:
            execute(command, timeout_s=0.5)
        except subprocess.TimeoutExpired as failure:
            note = "\n".join(failure.__notes__)
            snapshot = (
                failure.timeout,
                "owned-output" in note,
                "owned-error" in note,
            )
        else:
            snapshot = None

    # Assert
    assert (snapshot, calls) == ((0.5, True, True), [])
