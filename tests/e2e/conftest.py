"""Conftest for the ``tests/e2e/`` end-to-end workflow layer.

These tests exercise full user-facing workflows against real
subsystems (real ``sac`` subprocess, real ``apptainer`` container
runtime, real HTTP loopback servers). They are MANDATORY — run on
every PR alongside unit + integration tests.

Rationale
=========
Smoke + unit tests use heavy isolation and lots of fakes, so it is easy
for a refactor to silently break the *combination* of subsystems even
while every piece looks healthy in isolation. The integration suite
covers the live-Claude / a2a wire surface; this layer complements it by
driving the full ``sac agents start … stop`` lifecycle against the
actual apptainer + registry + a2a stack.

Environment requirements
========================
* ``sac`` binary on PATH (provided by editable install in CI).
* ``apptainer`` on PATH for lifecycle/fleet tests (CI must install).
* Per-test ``pytest.skip`` is acceptable only when a specific
  subsystem (e.g. apptainer) is genuinely missing from the runner —
  not as a default opt-in gate.

Each test is responsible for its own cleanup (``sac agents stop`` in a
``try/finally`` or via fixture teardown). Registry pollution across
tests is avoided by always using unique agent names (UUID suffix) so
parallel runs of this file don't collide with each other or with the
operator's real agents.
"""

from __future__ import annotations

import shutil
import socket
import subprocess
import time
import uuid
from pathlib import Path
from typing import Iterator

import pytest

# ---------------------------------------------------------------------------
# Shared helpers — port allocation, sac binary discovery, registry probe.
# ---------------------------------------------------------------------------


def _free_port() -> int:
    """Return a free TCP port on localhost."""
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


@pytest.fixture(scope="session")
def sac_bin() -> str:
    """Absolute path to the ``sac`` CLI, or skip the test."""
    found = shutil.which("sac")
    if not found:
        pytest.skip("sac binary not on PATH")
    return found


@pytest.fixture(scope="session")
def apptainer_available() -> bool:
    """True if ``apptainer`` is on PATH and a base image is materialised."""
    if shutil.which("apptainer") is None:
        return False
    base_image = Path("~/.scitex/agent-container/containers/sac-base.sif").expanduser()
    return base_image.is_file()


# ---------------------------------------------------------------------------
# tmp_registry_dir — point sac at a throwaway state dir so tests don't
# pollute the operator's real ``~/.scitex/agent-container/registry``.
# ---------------------------------------------------------------------------


@pytest.fixture
def tmp_registry_dir(tmp_path: Path, monkeypatch_session) -> Iterator[Path]:
    """Isolate sac's state.db / registry under a tmp path.

    The override is communicated to child ``sac`` processes via env
    vars only — we never patch python imports here (no mocks).
    """
    reg = tmp_path / "registry"
    reg.mkdir(parents=True, exist_ok=True)
    monkeypatch_session.setenv("SCITEX_AGENT_CONTAINER_STATE_DIR", str(reg))
    monkeypatch_session.setenv("SCITEX_AGENT_CONTAINER_REGISTRY_DIR", str(reg))
    yield reg


# pytest's ``monkeypatch`` is function-scoped; for env vars we want a
# session-friendly variant. We still use real env mutation + restore,
# not import-time patching — this just lets the fixture be reused.
@pytest.fixture
def monkeypatch_session() -> Iterator[pytest.MonkeyPatch]:
    mp = pytest.MonkeyPatch()
    yield mp
    mp.undo()


# ---------------------------------------------------------------------------
# tmp_home_with_image — a throwaway HOME-style dir that owns its own
# ``.scitex/agent-container/`` tree, optionally seeded with a symlink
# to the real base image so apptainer launches succeed.
# ---------------------------------------------------------------------------


@pytest.fixture
def tmp_home_with_image(tmp_path: Path) -> Path:
    """Build a throwaway sac state tree under tmp_path."""
    root = tmp_path / "sac_home"
    (root / ".scitex/agent-container/agents").mkdir(parents=True, exist_ok=True)
    containers = root / ".scitex/agent-container/containers"
    containers.mkdir(parents=True, exist_ok=True)
    real_image = Path("~/.scitex/agent-container/containers/sac-base.sif").expanduser()
    if real_image.is_file():
        link = containers / "sac-base.sif"
        if not link.exists():
            link.symlink_to(real_image)
    return root


# ---------------------------------------------------------------------------
# unique_agent_name — collision-free name so parallel e2e runs don't
# step on each other's registry rows.
# ---------------------------------------------------------------------------


@pytest.fixture
def unique_agent_name() -> str:
    return f"sac-e2e-{uuid.uuid4().hex[:8]}"


# ---------------------------------------------------------------------------
# Helper: wait for an agent to leave a transient registry state.
# ---------------------------------------------------------------------------


def wait_for_status(
    sac: str, name: str, want: str, *, timeout: float = 60.0
) -> str | None:
    """Poll ``sac agents list --json`` until ``name`` reports ``want``.

    Returns the observed status (which may equal ``want`` on success
    or whatever final state was seen on timeout). Returns ``None`` if
    the agent never appeared.
    """
    import json

    deadline = time.time() + timeout
    last: str | None = None
    while time.time() < deadline:
        proc = subprocess.run(
            [sac, "agents", "list", "--json"],
            capture_output=True,
            text=True,
            timeout=20,
        )
        if proc.returncode == 0 and proc.stdout.strip():
            try:
                payload = json.loads(proc.stdout)
            except json.JSONDecodeError:
                payload = {}
            for row in payload.get("agents", []):
                if row.get("name") == name:
                    last = row.get("status")
                    if last == want:
                        return last
                    break
        time.sleep(0.5)
    return last


# EOF
