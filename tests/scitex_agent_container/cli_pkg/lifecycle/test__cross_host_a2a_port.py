"""Cross-host a2a_port propagation tests.

Covers the seam between:

  * peer-side ``sac agents start --no-redispatch --json`` — MUST emit
    the RESOLVED a2a_port (an int when ``spec.a2a.port`` is ``"auto"``
    or an explicit int; ``null`` only when ``spec.a2a`` is missing or
    ``port: null``).
  * lead-side ``_dispatch_remote_start`` — MUST propagate that int
    verbatim into the ``instances.a2a_port`` row.

Background: before this fix the peer-side JSON read
``config.a2a.port`` directly. When the spec said ``port: auto`` the
literal string ``"auto"`` flunked the ``isinstance(_, int)`` check and
the JSON emitted ``null`` — even though the runner had ALREADY claimed
an int via ``resolve_a2a_port``. The lead then wrote NULL into its
instances row, breaking ``sac agents send <name>`` ("state.db records
no a2a_port for it").

No-mocks / no-monkeypatch: PATH-prepended shim binaries for ssh +
rsync (tests 1, 2); context-manager attribute swap for fake
``agent_start`` (tests 3, 4). Same pattern as ``test__dispatch.py`` and
``tests/scitex_agent_container/_lifecycle/test__a2a_port.py``.
"""

from __future__ import annotations

import importlib
import json
import os
import sys
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

import pytest
from click.testing import CliRunner

from scitex_agent_container.cli_pkg.lifecycle._dispatch import _dispatch_remote_start

# ---------------------------------------------------------------------------
# Shim installers (rsync + ssh on PATH).
# ---------------------------------------------------------------------------


def _install_rsync_shim(bin_dir: Path) -> None:
    """rsync shim — always succeeds with first-launch-style output."""
    body = (
        f"#!{sys.executable}\n"
        "import sys\n"
        "is_dry = any(a.startswith('-') and not a.startswith('--') and 'n' in a "
        "for a in sys.argv[1:])\n"
        "sys.stdout.write('>f+++++++++ spec.yaml\\n' if is_dry else '')\n"
        "sys.exit(0)\n"
    )
    script = bin_dir / "rsync"
    script.write_text(body)
    script.chmod(0o755)


def _install_ssh_shim(bin_dir: Path, *, stdout: str) -> None:
    """ssh shim that emits ``stdout`` and exits 0."""
    script = bin_dir / "ssh"
    body = (
        f"#!{sys.executable}\n"
        "import sys\n"
        f"sys.stdout.write({json.dumps(stdout)})\n"
        "sys.exit(0)\n"
    )
    script.write_text(body)
    script.chmod(0o755)


# ---------------------------------------------------------------------------
# Shared fixtures: HOME redirect + state.db redirect + PATH bin.
# ---------------------------------------------------------------------------


@pytest.fixture
def fake_home(tmp_path: Path, env_save_restore):
    """Redirect HOME so Path.home() returns tmp_path."""
    env_save_restore.set("HOME", str(tmp_path))
    return tmp_path


@pytest.fixture
def shim_bin(tmp_path: Path, env_save_restore) -> Path:
    """Prepend a fresh bin dir to PATH for shim installation."""
    bin_dir = tmp_path / "_shim_bin"
    bin_dir.mkdir()
    saved_path = os.environ.get("PATH", "")
    env_save_restore.set("PATH", f"{bin_dir}{os.pathsep}{saved_path}")
    return bin_dir


@pytest.fixture
def state_db(fake_home: Path) -> Iterator[Path]:
    """Redirect state.db to tmp; reload state_db AND port_allocator so the
    bound ``init_schema`` / ``open_db`` references both pick up the new
    ``DEFAULT_DB_PATH``. ``port_allocator`` imports from ``state_db`` at
    module load, so reloading only ``state_db`` leaves a stale binding
    inside ``port_allocator``.
    """
    db = fake_home / "state.db"
    saved = os.environ.get("SCITEX_AGENT_CONTAINER_STATE_DB")
    os.environ["SCITEX_AGENT_CONTAINER_STATE_DB"] = str(db)
    import scitex_agent_container._state.port_allocator as _port_alloc_mod
    import scitex_agent_container._state.state_db as _state_db_mod

    importlib.reload(_state_db_mod)
    importlib.reload(_port_alloc_mod)
    try:
        yield db
    finally:
        if saved is None:
            os.environ.pop("SCITEX_AGENT_CONTAINER_STATE_DB", None)
        else:
            os.environ["SCITEX_AGENT_CONTAINER_STATE_DB"] = saved
        importlib.reload(_state_db_mod)
        importlib.reload(_port_alloc_mod)


@pytest.fixture
def spec_dir_alpha(fake_home: Path) -> Path:
    """Populated spec dir at ``~/.scitex/agent-container/agents/alpha``."""
    d = fake_home / ".scitex" / "agent-container" / "agents" / "alpha"
    d.mkdir(parents=True)
    (d / "spec.yaml").write_text("name: alpha\n")
    return d


def _write_peer_config(home: Path, env_save_restore, peer: str = "peer-host") -> None:
    """Write ``config.yaml`` registering ``peer``."""
    cfg = home / "config.yaml"
    cfg.write_text(
        f"host:\n  fallback: hostname-short\npeers:\n  {peer}:\n    ssh: {peer}\n"
    )
    env_save_restore.set("SCITEX_AGENT_CONTAINER_CONFIG", str(cfg))


# ===========================================================================
# Test 1 — lead propagates resolved int from peer JSON into state.db.
# ===========================================================================


def test_dispatch_remote_start_propagates_resolved_a2a_port_int(
    spec_dir_alpha: Path,
    shim_bin: Path,
    state_db: Path,
    fake_home: Path,
    env_save_restore,
) -> None:
    """Lead's record_instance_start receives the int from peer JSON."""
    # Arrange
    _write_peer_config(fake_home, env_save_restore)
    _install_rsync_shim(shim_bin)
    _install_ssh_shim(
        shim_bin,
        stdout=json.dumps({"a2a_port": 19000, "started_at": "2026-05-17T00:00:00Z"}),
    )
    # Act
    _dispatch_remote_start("alpha", "peer-host", dry_run=False, force=False)
    # Assert
    from scitex_agent_container._state.state_db import list_active_instances

    rows = [r for r in list_active_instances() if r["name"] == "alpha"]
    assert rows[0]["a2a_port"] == 19000


# ===========================================================================
# Test 2 — None propagates as NULL when peer JSON has "a2a_port": null.
# ===========================================================================


def test_dispatch_remote_start_propagates_a2a_port_none_when_spec_omits_it(
    spec_dir_alpha: Path,
    shim_bin: Path,
    state_db: Path,
    fake_home: Path,
    env_save_restore,
) -> None:
    """When peer JSON has ``a2a_port: null`` (sidecar disabled), lead writes NULL."""
    # Arrange
    _write_peer_config(fake_home, env_save_restore)
    _install_rsync_shim(shim_bin)
    _install_ssh_shim(
        shim_bin,
        stdout=json.dumps({"a2a_port": None, "started_at": "2026-05-17T00:00:00Z"}),
    )
    # Act
    _dispatch_remote_start("alpha", "peer-host", dry_run=False, force=False)
    # Assert
    from scitex_agent_container._state.state_db import list_active_instances

    rows = [r for r in list_active_instances() if r["name"] == "alpha"]
    assert rows[0]["a2a_port"] is None


# ===========================================================================
# Tests 3 & 4 — local `sac agents start --no-redispatch --json` output.
# ---------------------------------------------------------------------------
# Strategy: pre-populate ``port_allocator``'s ``a2a_ports`` table with the
# resolved port for the agent (simulating what ``resolve_a2a_port`` does
# inside ``agent_start``), then swap ``agent_start`` for a no-op via the
# hand-rolled context-manager pattern (NOT pytest monkeypatch), and drive
# the click command with ``--no-redispatch --json``. The JSON payload
# under test reads from port_allocator — that's the seam.
# ---------------------------------------------------------------------------


@contextmanager
def _swap_attr(module: Any, name: str, replacement: Any) -> Iterator[None]:
    saved = getattr(module, name)
    setattr(module, name, replacement)
    try:
        yield
    finally:
        setattr(module, name, saved)


def _write_local_spec(home: Path, name: str, *, a2a_port: Any) -> Path:
    """Materialise a minimal spec yaml at ``~/.scitex/agent-container/agents/<name>``.

    The spec uses the runtime ``apptainer`` (the default sac runtime).
    We never actually invoke the runner — agent_start is swapped for a
    no-op — so the spec only needs to load cleanly.
    """
    agents_dir = home / ".scitex" / "agent-container" / "agents" / name
    agents_dir.mkdir(parents=True)
    yaml_path = agents_dir / f"{name}.yaml"
    port_line = "null" if a2a_port is None else json.dumps(a2a_port)
    yaml_path.write_text(
        "apiVersion: scitex-agent-container/v3\n"
        "kind: Agent\n"
        "metadata: {}\n"
        "spec:\n"
        "  runtime: apptainer\n"
        f"  a2a:\n    port: {port_line}\n"
    )
    return yaml_path


def _run_start_no_redispatch_json(
    name: str, yaml_path: Path, *, preclaim_port: int | None
) -> dict:
    """Drive ``sac agents start <yaml> --no-redispatch --json`` with a
    no-op fake ``agent_start`` and (optionally) a pre-claimed allocator
    row. Returns the parsed JSON object emitted on stdout.

    Pre-claiming substitutes for what the real ``agent_start`` would do
    via ``resolve_a2a_port`` — keeping the test focused on the JSON
    emission seam without spinning real apptainer.
    """
    from scitex_agent_container._state import port_allocator

    if preclaim_port is not None:
        port_allocator.claim_port(name, explicit=preclaim_port)

    from scitex_agent_container.cli_pkg.lifecycle import _start as start_mod

    def _fake_agent_start(*args: Any, **kwargs: Any) -> bool:
        return True

    runner = CliRunner()
    with _swap_attr(start_mod, "agent_start", _fake_agent_start):
        result = runner.invoke(
            start_mod.start,
            [str(yaml_path), "--no-redispatch", "--json"],
            catch_exceptions=False,
        )
    # The --json branch emits one JSON object per target on stdout.
    stdout_lines = [
        ln for ln in result.output.splitlines() if ln.strip().startswith("{")
    ]
    assert stdout_lines, (
        f"no JSON line in stdout. exit={result.exit_code}, output={result.output!r}"
    )
    return json.loads(stdout_lines[-1])


def test_start_no_redispatch_json_includes_resolved_a2a_port(
    fake_home: Path, state_db: Path, env_save_restore
) -> None:
    """``port: auto`` spec → JSON ``a2a_port`` is an int (resolved by allocator)."""
    # Arrange
    yaml_path = _write_local_spec(fake_home, "alpha", a2a_port="auto")
    # Act — pre-claim port 19200 to simulate resolve_a2a_port's effect.
    payload = _run_start_no_redispatch_json("alpha", yaml_path, preclaim_port=19200)
    # Assert
    assert payload["a2a_port"] == 19200


def test_start_no_redispatch_json_includes_a2a_port_when_explicit_int(
    fake_home: Path, state_db: Path, env_save_restore
) -> None:
    """``port: 19500`` spec → JSON ``a2a_port`` is exactly that int."""
    # Arrange
    yaml_path = _write_local_spec(fake_home, "alpha", a2a_port=19500)
    # Act
    payload = _run_start_no_redispatch_json("alpha", yaml_path, preclaim_port=19500)
    # Assert
    assert payload["a2a_port"] == 19500
