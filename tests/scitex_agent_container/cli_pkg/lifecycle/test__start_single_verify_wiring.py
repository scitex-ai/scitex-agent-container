"""``run_single_targets`` — the launch verdict is WIRED into the start path.

The unit suites pin the verdict (``test__launch_verify``) and its
rendering (``test__start_verify_report``); this module pins that
``run_single_targets`` actually routes its report through them — i.e.
that ``sac agents start --json`` now carries a ``verify`` sub-object.

Exercised through the in-SIF broker path (the same real loopback listen
server ``test__start_single_assume_yes`` uses) because it reaches the
real reporting code with zero container infrastructure: a brokered start
returns success locally while the EVIDENCE lives on the host, so the
verdict must be an honest ``skipped`` — pre-fix, the payload had no
``verify`` key at all and the start was reported as a bare ``started``.

NO MOCKS — real spec.yaml on disk, real isolated state.db, a real
loopback HTTP listen. AAA markers, one assertion per test, 3+-word names.
"""

from __future__ import annotations

from tests.scitex_agent_container._helpers.explicit_spec import explicitize_yaml

import json
import os
from pathlib import Path
from typing import Any, Iterator

import pytest

from scitex_agent_container._state import state_db
from scitex_agent_container.cli_pkg.lifecycle._start_single import (
    run_single_targets,
)

_SIF_KEYS = ("APPTAINER_CONTAINER", "SINGULARITY_CONTAINER")
_LISTEN_KEYS = (
    "SAC_LISTEN_BASE_URL",
    "SCITEX_AGENT_CONTAINER_LISTEN_BASE_URL",
    "SAC_LISTEN_BEARER",
    "SCITEX_AGENT_CONTAINER_LISTEN_BEARER",
    "SAC_NAME",
    "SCITEX_AGENT_CONTAINER_NAME",
    "SAC_ASSUME_YES",
)


@pytest.fixture
def broker_env() -> Iterator[Any]:
    """Yield a setter for the in-SIF + listen env vars (save/restore)."""
    keys = _SIF_KEYS + _LISTEN_KEYS
    saved = {k: os.environ.get(k) for k in keys}
    for k in keys:
        os.environ.pop(k, None)

    def _set(key: str, value: str | None) -> None:
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value

    try:
        yield _set
    finally:
        for k, prev in saved.items():
            if prev is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = prev


@pytest.fixture
def isolated_state(tmp_path: Path, pg_schema: str) -> Iterator[Path]:
    """Real isolated state.db + runtime dir + HOME (mirrors the sibling
    ``test__start_single_assume_yes`` fixture)."""
    db = tmp_path / "state.db"
    runtime_dir = tmp_path / "runtime"
    home = tmp_path / "home"
    home.mkdir()
    keys = {
        "SCITEX_AGENT_CONTAINER_STATE_DB": str(db),
        "SCITEX_AGENT_CONTAINER_RUNTIME_DIR": str(runtime_dir),
        "HOME": str(home),
        "SCITEX_DIR": str(home / ".scitex"),
    }
    saved = {k: os.environ.get(k) for k in keys}
    saved_default = state_db.DEFAULT_DB_PATH
    os.environ.update(keys)
    state_db.DEFAULT_DB_PATH = db
    try:
        yield db
    finally:
        state_db.DEFAULT_DB_PATH = saved_default
        for k, prev in saved.items():
            if prev is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = prev


def _write_spec(yaml_root: Path, name: str) -> Path:
    agent_dir = yaml_root / name
    agent_dir.mkdir(parents=True)
    spec = agent_dir / "spec.yaml"
    spec.write_text(
        explicitize_yaml(
            "apiVersion: scitex-agent-container/v3\n"
            "kind: Agent\n"
            "spec:\n"
            "  runtime: apptainer\n"
            "  host: ${HOSTNAME}\n"
            f"  workdir: {yaml_root / (name + '-work')}\n"
            "  apptainer:\n    image: /x.sif\n    binds: []\n"
            "  restart:\n    policy: on-failure\n    max_retries: 3\n"
            "  claude:\n"
            "    model: sonnet\n"
            "  health:\n"
            "    enabled: false\n"
            "    interval: 60\n"
        )
    )
    return spec


def _start_loopback_listen():
    """Real loopback HTTP server answering the brokered /agents POST."""
    import http.server
    import threading

    captured: dict = {}

    class _Handler(http.server.BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802
            length = int(self.headers.get("Content-Length", "0"))
            captured["body"] = self.rfile.read(length)
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(
                json.dumps({"name": "capsule-child", "returncode": 0}).encode()
            )

        def log_message(self, *_args):  # silence test noise
            return

    server = http.server.HTTPServer(("127.0.0.1", 0), _Handler)
    host, port = server.server_address
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return f"http://{host}:{port}", captured, server, thread


def _run_brokered_start(broker_env, tmp_path: Path, capsys) -> dict:
    """Drive one brokered ``--json`` start; return the report payload."""
    broker_env("APPTAINER_CONTAINER", "/path/to/agent.sif")
    base_url, _captured, server, thread = _start_loopback_listen()
    broker_env("SAC_LISTEN_BASE_URL", base_url)
    spec = _write_spec(tmp_path / "yaml", "capsule-child")
    try:
        run_single_targets(
            [str(spec)],
            no_preflight=True,
            force=False,
            resume_id=None,
            session_mode=None,
            dry_run=False,
            as_json=True,
            foreground=False,
            one_shot=False,
            strict_drift=False,
            no_redispatch=True,
            multi_foreground=False,
            preflight_runner=lambda: None,
            yes=True,
        )
    finally:
        server.shutdown()
        thread.join(timeout=2.0)
    for line in capsys.readouterr().out.splitlines():
        line = line.strip()
        if line.startswith("{"):
            payload = json.loads(line)
            if payload.get("name") == "capsule-child":
                return payload
    raise AssertionError("no JSON report line for 'capsule-child' on stdout")


def test_brokered_start_json_carries_verify_object(
    isolated_state, broker_env, tmp_path: Path, capsys
) -> None:
    """The ``--json`` report now carries the launch verdict (pre-fix the
    payload had no ``verify`` key and success was asserted blind)."""
    # Arrange / Act — one brokered start through the real loopback listen.
    # Arrange
    del isolated_state
    # Act
    payload = _run_brokered_start(broker_env, tmp_path, capsys)
    # Assert
    assert "verify" in payload


def test_brokered_start_verdict_is_honest_skipped(
    isolated_state, broker_env, tmp_path: Path, capsys
) -> None:
    """A brokered start cannot see host evidence — the verdict must say
    ``skipped`` (verification happens host-side), never a fabricated
    verified-up."""
    # Arrange
    del isolated_state
    # Act
    payload = _run_brokered_start(broker_env, tmp_path, capsys)
    # Assert
    assert payload["verify"]["status"] == "skipped"
