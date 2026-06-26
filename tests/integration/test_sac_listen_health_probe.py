"""Behavioural tests for the sac listen health watchdog + its units.

Incident 2026-06-26: the central ``sac listen`` died mid-session with
NO auto-restart and NO alarm, so the fleet lost a2a comms silently.
The hardening adds:

* ``scripts/systemd/sac-listen.service`` — ``Restart=always`` decoupled
  from agent/lead lifecycle.
* ``scripts/systemd/sac-listen-health-probe.sh`` — HTTP-probes
  ``/v1/health``; on failure logs a LOUD ERROR, alarms, and restarts.
* ``scripts/systemd/sac-listen-health.{service,timer}`` — fire the
  probe every ~30s.
* ``scripts/systemd/install-sac-listen.sh`` — install path.

No mocks (STX-NM002): the probe is exercised against a REAL local HTTP
server bound to a REAL ephemeral port over a REAL socket. The "down"
case points the probe at a closed port (real connection refused). The
unit-file assertions parse the REAL shipped files. AAA markers (TQ002);
3+-word test names.
"""

from __future__ import annotations

import http.server
import os
import shutil
import socket
import subprocess
import threading
from pathlib import Path

import pytest

# --- locate the shipped scripts -------------------------------------------

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SYSTEMD_DIR = _REPO_ROOT / "scripts" / "systemd"
_PROBE = _SYSTEMD_DIR / "sac-listen-health-probe.sh"
_INSTALL = _SYSTEMD_DIR / "install-sac-listen.sh"
_SERVICE = _SYSTEMD_DIR / "sac-listen.service"
_HEALTH_SERVICE = _SYSTEMD_DIR / "sac-listen-health.service"
_HEALTH_TIMER = _SYSTEMD_DIR / "sac-listen-health.timer"

pytestmark = pytest.mark.skipif(
    shutil.which("curl") is None,
    reason="sac-listen-health-probe.sh requires curl",
)


# --- real-HTTP-server fixture ---------------------------------------------


class _QuietHandler(http.server.BaseHTTPRequestHandler):
    """Answers /v1/health with the real listen health shape; silent log."""

    def do_GET(self):  # noqa: N802 (http.server API)
        body = b'{"ok": true, "service": "sac-listen", "v": 1}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args):  # silence the default stderr spam
        return


@pytest.fixture()
def live_health_server():
    """Start a real HTTP server on an ephemeral loopback port; yield URL."""
    server = http.server.HTTPServer(("127.0.0.1", 0), _QuietHandler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{port}/v1/health"
    finally:
        server.shutdown()
        server.server_close()


def _free_port() -> int:
    """Grab then release a port so nothing is listening on it."""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _run_probe(*args: str, env_extra: dict[str, str] | None = None):
    env = os.environ.copy()
    # Never let a test accidentally fire the operator alarm.
    env["SAC_LISTEN_NOTIFY"] = "0"
    if env_extra:
        env.update(env_extra)
    return subprocess.run(
        ["bash", str(_PROBE), *args],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )


def _down_env() -> dict[str, str]:
    """Env pointing the probe at a real closed port (connection refused)."""
    return {
        "SAC_LISTEN_HEALTH_URL": f"http://127.0.0.1:{_free_port()}/v1/health",
        "SAC_LISTEN_PROBE_TIMEOUT": "2",
    }


# --- probe: check-only classification -------------------------------------


def test_check_only_passes_against_live_server(live_health_server):
    # Arrange
    env = {"SAC_LISTEN_HEALTH_URL": live_health_server}
    # Act
    result = _run_probe("--check-only", env_extra=env)
    # Assert
    assert result.returncode == 0


def test_check_only_fails_against_dead_port():
    # Arrange
    env = _down_env()
    # Act
    result = _run_probe("--check-only", env_extra=env)
    # Assert
    assert result.returncode == 1


def test_check_only_down_logs_unhealthy_line():
    # Arrange
    env = _down_env()
    # Act
    result = _run_probe("--check-only", env_extra=env)
    # Assert
    assert "UNHEALTHY" in result.stderr


def test_check_only_healthy_is_quiet(live_health_server):
    # Arrange
    env = {"SAC_LISTEN_HEALTH_URL": live_health_server}
    # Act
    result = _run_probe("--check-only", env_extra=env)
    # Assert
    assert result.stderr.strip() == ""


# --- probe: heal-mode loudness (no real systemctl needed) -----------------


def test_heal_mode_down_emits_loud_error():
    # Arrange
    env = _down_env()
    env["SAC_LISTEN_UNIT"] = "sac-listen-nonexistent-test.service"
    # Act
    result = _run_probe(env_extra=env)
    # Assert
    assert "ERROR: sac-listen DOWN" in result.stderr


def test_heal_mode_logs_restart_intent():
    # Arrange
    env = _down_env()
    env["SAC_LISTEN_UNIT"] = "sac-listen-nonexistent-test.service"
    # Act
    result = _run_probe(env_extra=env)
    # Assert
    assert "RESTARTING" in result.stderr


def test_heal_mode_healthy_does_not_restart(live_health_server):
    # Arrange
    env = {"SAC_LISTEN_HEALTH_URL": live_health_server}
    # Act
    result = _run_probe(env_extra=env)
    # Assert
    assert result.returncode == 0 and "RESTARTING" not in result.stderr


def test_usage_error_on_bad_arg():
    # Arrange
    args = ("--bogus",)
    # Act
    result = _run_probe(*args)
    # Assert
    assert result.returncode == 2


# --- probe + install: shell syntax ----------------------------------------


def test_probe_script_has_valid_bash_syntax():
    # Arrange
    cmd = ["bash", "-n", str(_PROBE)]
    # Act
    result = subprocess.run(cmd, capture_output=True, text=True)
    # Assert
    assert result.returncode == 0, result.stderr


def test_install_script_has_valid_bash_syntax():
    # Arrange
    cmd = ["bash", "-n", str(_INSTALL)]
    # Act
    result = subprocess.run(cmd, capture_output=True, text=True)
    # Assert
    assert result.returncode == 0, result.stderr


# --- unit-file lint --------------------------------------------------------


def _kv_pairs(text: str) -> dict[str, str]:
    """Flatten a unit file to last-wins key=value (ignoring comments)."""
    out: dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or line.startswith("["):
            continue
        if "=" in line:
            k, _, v = line.partition("=")
            out[k.strip()] = v.strip()
    return out


def test_listen_unit_restarts_always():
    # Arrange
    kv = _kv_pairs(_SERVICE.read_text())
    # Act
    restart = kv.get("Restart")
    # Assert
    assert restart == "always"


def test_listen_unit_has_restart_debounce():
    # Arrange
    kv = _kv_pairs(_SERVICE.read_text())
    # Act
    restart_sec = kv.get("RestartSec")
    # Assert
    assert restart_sec == "5s"


def _directive_lines(text: str) -> list[str]:
    """Non-comment, non-section unit-file lines (the actual directives)."""
    return [
        ln.strip()
        for ln in text.splitlines()
        if ln.strip()
        and not ln.strip().startswith("#")
        and not ln.strip().startswith("[")
    ]


def test_listen_unit_is_decoupled_from_agents():
    # Arrange
    lines = _directive_lines(_SERVICE.read_text())
    # Act — scan only real directives, not the explanatory comments.
    offenders = [
        ln
        for ln in lines
        if ln.startswith(("Requires=", "BindsTo=", "PartOf="))
    ]
    # Assert
    assert offenders == []


def test_listen_unit_only_orders_after_network():
    # Arrange
    text = _SERVICE.read_text()
    # Act
    afters = [
        line.split("=", 1)[1].strip()
        for line in text.splitlines()
        if line.strip().startswith("After=")
    ]
    # Assert
    assert afters == ["network.target"]


def test_health_timer_fires_periodically():
    # Arrange
    kv = _kv_pairs(_HEALTH_TIMER.read_text())
    # Act
    has_periodic = "OnUnitActiveSec" in kv
    # Assert
    assert has_periodic


def test_health_service_is_oneshot():
    # Arrange
    kv = _kv_pairs(_HEALTH_SERVICE.read_text())
    # Act
    service_type = kv.get("Type")
    # Assert
    assert service_type == "oneshot"


def test_health_service_execstart_names_probe():
    # Arrange
    kv = _kv_pairs(_HEALTH_SERVICE.read_text())
    # Act
    exec_start = kv.get("ExecStart", "")
    # Assert
    assert "sac-listen-health-probe.sh" in exec_start


def test_health_units_decoupled_from_listen():
    # Arrange
    svc = _HEALTH_SERVICE.read_text()
    # Act
    has_hard_dep = "Requires=sac-listen.service" in svc
    # Assert
    assert not has_hard_dep


def test_probe_script_is_executable():
    # Arrange
    mode = _PROBE.stat().st_mode
    # Act
    is_exec = bool(mode & 0o111)
    # Assert
    assert is_exec, "probe script must be executable"
