"""``run_single_targets`` — assume_yes / ``SAC_ASSUME_YES`` propagation.

Bug fix (2026-07-05, reported by paper-scitex-clew): ``sac agents start
<name> -y`` invoked INSIDE an apptainer SIF is brokered to the host's
``sac listen`` via :func:`_lifecycle._in_sif_broker.maybe_broker_in_sif_spawn`
→ :func:`_lifecycle._spawn_client.request_spawn`. Before this fix, the
POST body carried no consent field at all, so the host's re-shelled
``sac agents start <name>`` subprocess ALWAYS hit
``should_preview_and_require_yes``'s refusal, even though ``-y`` was
given at the top of the call chain.

This module pins the IN-SIF caller's half of the fix:
:func:`run_single_targets` must fold ``yes`` (CLI ``-y``/``--yes``) OR
``SAC_ASSUME_YES=1`` into a single ``effective_yes`` value and forward it
to :func:`agent_start` as ``assume_yes``, which the in-SIF broker then
puts on the wire as the POST body's ``assume_yes`` field.

NO MOCKS — real spec.yaml on disk + the injectable ``opener`` seam on
the in-SIF broker (mirrors ``test__in_sif_broker.py`` and
``test__spawn_client.py``). Each test: AAA markers (TQ002), one
assertion (TQ007), 3+-word name.
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

# ---------------------------------------------------------------------------
# Env fixtures — in-SIF markers, listen base URL, and SAC_ASSUME_YES.
# ---------------------------------------------------------------------------

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
    """Yield a setter for the in-SIF + listen + SAC_ASSUME_YES env vars."""
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
    """Real isolated state.db + runtime dir + HOME (mirrors test__in_sif_broker).

    DEPENDS ON ``pg_schema`` since 2026-08-28: ``agent_start`` resolves the
    agent's a2a port, and that ledger moved to PostgreSQL, so an isolated
    state.db is no longer the whole isolation a start needs.
    """
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
    state_db.init_schema(db)
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
        explicitize_yaml("apiVersion: scitex-agent-container/v3\n"
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
        "    interval: 60\n")
    )
    return spec


def _start_loopback_listen():
    """Start a real loopback HTTP server recording the /agents POST body.

    Returns (base_url, captured) where captured['body'] is populated
    after the first POST. A real server (not an injected opener) is
    used here because ``run_single_targets`` does not expose an
    ``in_sif_opener`` seam of its own — only ``agent_start`` does, and
    ``run_single_targets`` calls it without one (production shape).
    """
    import http.server
    import threading

    captured: dict = {}

    class _Handler(http.server.BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802
            length = int(self.headers.get("Content-Length", "0"))
            raw = self.rfile.read(length)
            captured["body"] = raw
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


def test_yes_flag_forwards_assume_yes_true_to_broker_body(
    isolated_state, broker_env, tmp_path: Path
) -> None:
    """``-y``/``--yes`` on the CLI must reach the host's POST body."""
    # Arrange — in-SIF + a real loopback listen server.
    broker_env("APPTAINER_CONTAINER", "/path/to/agent.sif")
    base_url, captured, server, thread = _start_loopback_listen()
    broker_env("SAC_LISTEN_BASE_URL", base_url)
    spec = _write_spec(tmp_path / "yaml", "capsule-child")
    try:
        # Act
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
    # Assert — the POST body the host received carries assume_yes: true.
    body = json.loads(captured["body"])
    assert body.get("assume_yes") is True


def test_sac_assume_yes_env_forwards_assume_yes_true_to_broker_body(
    isolated_state, broker_env, tmp_path: Path
) -> None:
    """``SAC_ASSUME_YES=1`` alone (no ``-y``) must ALSO reach the POST body.

    This is the escape-valve fallback (requirement 5 of the bug fix):
    callers that cannot easily thread ``assume_yes`` through every
    layer set the env var instead.
    """
    # Arrange — in-SIF + real loopback listen; --yes NOT passed.
    broker_env("APPTAINER_CONTAINER", "/path/to/agent.sif")
    base_url, captured, server, thread = _start_loopback_listen()
    broker_env("SAC_LISTEN_BASE_URL", base_url)
    broker_env("SAC_ASSUME_YES", "1")
    spec = _write_spec(tmp_path / "yaml", "capsule-child")
    try:
        # Act
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
            yes=False,
        )
    finally:
        server.shutdown()
        thread.join(timeout=2.0)
    # Assert
    body = json.loads(captured["body"])
    assert body.get("assume_yes") is True


def test_no_yes_and_no_env_omits_assume_yes_from_broker_body(
    isolated_state, broker_env, tmp_path: Path
) -> None:
    """Regression guard — the default (no consent given) is unchanged.

    Neither ``--yes`` nor ``SAC_ASSUME_YES`` is set: the wire body must
    NOT carry ``assume_yes`` at all (back-compat with pre-fix brokers,
    same convention as ``foreground``/``one_shot``).
    """
    # Arrange
    broker_env("APPTAINER_CONTAINER", "/path/to/agent.sif")
    base_url, captured, server, thread = _start_loopback_listen()
    broker_env("SAC_LISTEN_BASE_URL", base_url)
    spec = _write_spec(tmp_path / "yaml", "capsule-child")
    try:
        # Act
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
            yes=False,
        )
    finally:
        server.shutdown()
        thread.join(timeout=2.0)
    # Assert
    body = json.loads(captured["body"])
    assert "assume_yes" not in body
