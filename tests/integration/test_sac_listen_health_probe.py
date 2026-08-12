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


# The dead port comes from the shared ``dead_port`` fixture
# (tests/scitex_agent_container/_helpers/ports.py, wired in tests/conftest.py):
# bound WITHOUT listening, so it refuses, and HELD, so nothing can take it
# mid-test. The helper that used to live here grabbed a port and released it
# again before the probe ran — see that module for the flake it caused.


@pytest.fixture(autouse=True)
def isolated_ledger(tmp_path):
    """Give every test in this module its OWN watchdog failure ledger.

    The probe counts CONSECUTIVE failures across a state file (systemd
    invokes it FRESH every ~30s, so a file is the only way to count). Two
    hazards follow, and this closes both:

    * Without redirection these tests write the REAL ledger under ``$HOME``.
      On the host that could push the LIVE ``sac listen`` into a restart —
      the test suite causing the very outage the watchdog exists to prevent.
      ``tests/conftest.py`` sets a session-wide floor; this makes it per-test.
    * Sharing one ledger makes the heal-mode tests ORDER-DEPENDENT: one
      test's leftover failure weight silently satisfies the next test's
      corroboration threshold. That is a green test proving nothing — and it
      is exactly how ``test_heal_mode_logs_restart_intent`` passed in CI
      while asserting a restart it had not actually earned.

    Real env, real file, restored on teardown (no monkeypatch — STX-NM002).
    """
    key = "SAC_LISTEN_HEALTH_STATE"
    prior = os.environ.get(key)
    os.environ[key] = str(tmp_path / "listen-health.state")
    try:
        yield
    finally:
        if prior is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = prior


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


def _down_env(dead_port) -> dict[str, str]:
    """Env pointing the probe at a held, never-listened port (refuses)."""
    return {
        "SAC_LISTEN_HEALTH_URL": dead_port.url("/v1/health"),
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


def test_check_only_fails_against_dead_port(dead_port):
    # Arrange
    env = _down_env(dead_port)
    # Act
    result = _run_probe("--check-only", env_extra=env)
    # Assert
    assert result.returncode == 1


def test_check_only_down_names_the_verdict(dead_port):
    # Arrange
    env = _down_env(dead_port)
    # Act
    result = _run_probe("--check-only", env_extra=env)
    # Assert — the probe now reports a THREE-STATE verdict. "UNHEALTHY" was a
    # two-state word, and two states cannot distinguish "connection refused"
    # (nothing is listening) from "I asked and got nothing" (a loaded box).
    # Collapsing those is what restarted a HEALTHY control plane on
    # 2026-07-14, so the vocabulary changed with the decision model.
    assert "DOWN" in result.stderr


def test_check_only_healthy_is_quiet(live_health_server):
    # Arrange
    env = {"SAC_LISTEN_HEALTH_URL": live_health_server}
    # Act
    result = _run_probe("--check-only", env_extra=env)
    # Assert
    assert result.stderr.strip() == ""


# --- probe: heal-mode loudness (no real systemctl needed) -----------------


def _corroborated_down_env(dead_port) -> dict[str, str]:
    """A dead port, probed ONCE already — the next probe corroborates.

    A SINGLE failed probe is no longer grounds to restart: that was the
    2026-07-14 false-RED, where one 5s timeout on a box at load 60-70
    restarted a HEALTHY listen and deafened every agent's inbox. These two
    tests used to assert the loud line after ONE probe — i.e. they ENCODED
    the bug. They now earn the verdict first (a refusal is weight 2, the
    threshold is 3, so a second one crosses it).
    """
    env = _down_env(dead_port)
    env["SAC_LISTEN_UNIT"] = "sac-listen-nonexistent-test.service"
    _run_probe(env_extra=env)
    return env


def test_uncorroborated_down_does_not_restart(dead_port):
    # Arrange — the regression that made this whole rewrite necessary.
    env = _down_env(dead_port)
    env["SAC_LISTEN_UNIT"] = "sac-listen-nonexistent-test.service"
    # Act
    result = _run_probe(env_extra=env)
    # Assert — one failed probe must NOT destroy the control plane.
    assert "RESTARTING" not in result.stderr


def test_heal_mode_corroborated_down_emits_loud_error(dead_port):
    # Arrange
    env = _corroborated_down_env(dead_port)
    # Act
    result = _run_probe(env_extra=env)
    # Assert
    assert "ERROR: sac-listen DOWN" in result.stderr


def test_heal_mode_logs_restart_intent(dead_port):
    # Arrange
    env = _corroborated_down_env(dead_port)
    # Act
    result = _run_probe(env_extra=env)
    # Assert — a genuinely dead listen still comes back (incident 2026-06-26).
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


# --- install: SAC_SECRETS_ENVRC unit wiring -------------------------------
#
# WHY this matters: the systemd-user `sac-listen` daemon (and the
# agent-managed restarts it spawns) does NOT inherit the operator's
# interactive shell, so the deploy-time `.envrc` fold in
# src/scitex_agent_container/runtimes/_envrc.py cannot resolve the real
# CCT_* tokens unless the secret-file list is baked into the unit env. The
# installer computes `Environment=SAC_SECRETS_ENVRC=` at install time. We
# exercise the REAL installer's unit-generation functions against a REAL
# temp HOME + a REAL shipped unit file (no mocks, STX-NM002).

import re  # noqa: E402 (grouped with the install-wiring tests it supports)


def _extract_install_functions() -> str:
    """Return the bash text of log()/secrets_envrc_value()/apply_secrets_envrc().

    Sourcing the whole installer would run its install side effects, so we
    slice out exactly the three pure functions (header line through a line
    that is just ``}``) from the REAL shipped script.
    """
    text = _INSTALL.read_text()
    wanted = ("log()", "secrets_envrc_value()", "apply_secrets_envrc()")
    out: list[str] = []
    lines = text.splitlines()
    i = 0
    while i < len(lines):
        stripped = lines[i].lstrip()
        if any(stripped.startswith(w) for w in wanted):
            block = [lines[i]]
            # Single-line function (e.g. `log() { ...; }`)?
            if "}" in lines[i] and lines[i].rstrip().endswith("}"):
                out.append(lines[i])
                i += 1
                continue
            i += 1
            while i < len(lines):
                block.append(lines[i])
                if lines[i].rstrip() == "}":
                    break
                i += 1
            out.append("\n".join(block))
        i += 1
    blob = "\n".join(out)
    assert "apply_secrets_envrc" in blob and "secrets_envrc_value" in blob
    return blob


def _run_apply(tmp_path, home, *, override: str | None):
    """Copy the real unit, run apply_secrets_envrc against it, return text."""
    unit = tmp_path / "sac-listen.service"
    unit.write_text(_SERVICE.read_text())
    fns = tmp_path / "fns.sh"
    fns.write_text(_extract_install_functions())
    env = os.environ.copy()
    env["HOME"] = str(home)
    if override is None:
        env.pop("SAC_SECRETS_ENVRC", None)
    else:
        env["SAC_SECRETS_ENVRC"] = override
    script = f". {fns}\napply_secrets_envrc {unit}\n"
    subprocess.run(
        ["bash", "-euo", "pipefail", "-c", script],
        env=env,
        capture_output=True,
        text=True,
        check=True,
        timeout=15,
    )
    return unit.read_text()


def _envrc_lines(text: str) -> list[str]:
    return [
        ln for ln in text.splitlines()
        if ln.startswith("Environment=SAC_SECRETS_ENVRC=")
    ]


@pytest.fixture()
def default_unit_text(tmp_path):
    """Apply the wiring with two standardized *.src files; return unit text."""
    home = tmp_path / "home"
    secrets = home / ".bash.d" / "secrets" / "010_scitex"
    secrets.mkdir(parents=True)
    (secrets / "01_cct.src").write_text("export FOO=1\n")
    (secrets / "00_aaa.src").write_text("export BAR=2\n")
    text = _run_apply(tmp_path, home, override=None)
    return text, secrets


def test_default_emits_single_environment_line(default_unit_text):
    # Arrange
    text, _secrets = default_unit_text
    # Act
    lines = _envrc_lines(text)
    # Assert
    assert len(lines) == 1


def test_default_globs_sorted_colon_joined_paths(default_unit_text):
    # Arrange
    text, secrets = default_unit_text
    # Act
    prefix = "Environment=SAC_SECRETS_ENVRC="
    value = _envrc_lines(text)[0][len(prefix):]
    # Assert — sorted (00 before 01), colon-joined absolute paths.
    expected = f"{secrets / '00_aaa.src'}:{secrets / '01_cct.src'}"
    assert value == expected


def test_override_used_verbatim(tmp_path):
    # Arrange — real *.src files present, but an explicit override given.
    home = tmp_path / "home"
    secrets = home / ".bash.d" / "secrets" / "010_scitex"
    secrets.mkdir(parents=True)
    (secrets / "00_aaa.src").write_text("export BAR=2\n")
    # Act
    text = _run_apply(tmp_path, home, override="/custom/a.src:/custom/b.src")
    # Assert — override wins over the glob.
    lines = _envrc_lines(text)
    assert lines == ["Environment=SAC_SECRETS_ENVRC=/custom/a.src:/custom/b.src"]


def test_no_secrets_omits_environment_line(tmp_path):
    # Arrange — empty secrets dir, no override.
    home = tmp_path / "home"
    (home / ".bash.d" / "secrets" / "010_scitex").mkdir(parents=True)
    # Act
    text = _run_apply(tmp_path, home, override=None)
    # Assert
    assert _envrc_lines(text) == []


def test_apply_is_idempotent(tmp_path):
    # Arrange — one *.src; apply once, then re-source + apply again.
    home = tmp_path / "home"
    secrets = home / ".bash.d" / "secrets" / "010_scitex"
    secrets.mkdir(parents=True)
    (secrets / "00_aaa.src").write_text("export BAR=2\n")
    unit = tmp_path / "sac-listen.service"
    unit.write_text(_SERVICE.read_text())
    fns = tmp_path / "fns.sh"
    fns.write_text(_extract_install_functions())
    env = os.environ.copy()
    env["HOME"] = str(home)
    env.pop("SAC_SECRETS_ENVRC", None)
    script = (
        f". {fns}\n"
        f"apply_secrets_envrc {unit}\n"
        f"apply_secrets_envrc {unit}\n"
    )
    # Act
    subprocess.run(
        ["bash", "-euo", "pipefail", "-c", script],
        env=env, capture_output=True, text=True, check=True, timeout=15,
    )
    # Assert — exactly one line after two applies.
    assert len(_envrc_lines(unit.read_text())) == 1


def test_environment_line_after_service_header(default_unit_text):
    # Arrange
    text, _secrets = default_unit_text
    # Act
    svc_idx = text.index("[Service]")
    env_idx = text.index("Environment=SAC_SECRETS_ENVRC=")
    # Assert — the line sits inside [Service], not before it.
    assert svc_idx < env_idx


def test_environment_line_before_install_section(default_unit_text):
    # Arrange
    text, _secrets = default_unit_text
    # Act
    env_idx = text.index("Environment=SAC_SECRETS_ENVRC=")
    install_match = re.search(r"^\[Install\]", text, re.MULTILINE)
    # Assert — never spills into [Install].
    assert install_match is None or env_idx < install_match.start()


def test_wiring_preserves_restart_always(default_unit_text):
    # Arrange
    text, _secrets = default_unit_text
    # Act
    kv = _kv_pairs(text)
    # Assert
    assert kv.get("Restart") == "always"


def test_wiring_preserves_standard_output_journal(default_unit_text):
    # Arrange
    text, _secrets = default_unit_text
    # Act
    kv = _kv_pairs(text)
    # Assert
    assert kv.get("StandardOutput") == "journal"


def test_wiring_preserves_standard_error_journal(default_unit_text):
    # Arrange
    text, _secrets = default_unit_text
    # Act
    kv = _kv_pairs(text)
    # Assert
    assert kv.get("StandardError") == "journal"
