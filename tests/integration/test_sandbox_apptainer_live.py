"""LIVE sealed-run check: real apptainer, real network, real image.

Skipped unless BOTH are set (CI has neither, by design — it needs a host
with apptainer, user namespaces and outbound HTTPS):

* ``SAC_SANDBOX_TEST_SIF``  — an image with bash, curl, python3, unshare;
* ``SAC_SANDBOX_TEST_ROOT`` — a durable scratch dir for run roots (never /tmp).

It drives the production pieces (``begin_run`` -> ``supervised_argv`` ->
supervisor ``main`` -> ``end_run``) around a hand-built ``apptainer exec``
argv carrying :func:`sandbox_flags` + :func:`netgw_wrap`, and asserts:
an allowlisted host is reachable, a non-allowlisted one is refused AND
logged, a direct (proxy-bypassing) connect is refused AND logged, only the
allowed env names enter, a loopback listener inside is reachable from the
host through the mirror, a second run sees none of the first run's files,
and the outcome record is complete.

The control-plane mask (``_sandbox._hide``) is run the same way: inside the
real container, behind the gateway, a probe started through the launcher
must see none of the state dir, the gateway API key, the secret env file or
the launching SAC installation, nor any other process, and must not be able
to undo the mask; the owner outside it must still see the state dir.
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import sys
import threading
import time
import urllib.request
from pathlib import Path
from types import SimpleNamespace

import pytest

import scitex_agent_container
from scitex_agent_container._sandbox import (
    begin_run,
    end_run,
    netgw_wrap,
    parse_sandbox,
    sandbox_flags,
    supervised_argv,
)
from scitex_agent_container._sandbox._supervisor import main as supervise

SIF = os.environ.get("SAC_SANDBOX_TEST_SIF", "")
ROOT = os.environ.get("SAC_SANDBOX_TEST_ROOT", "")
pytestmark = pytest.mark.skipif(
    not (SIF and ROOT and Path(SIF).is_file() and shutil.which("apptainer")),
    reason="live sealed-run test: set SAC_SANDBOX_TEST_SIF and SAC_SANDBOX_TEST_ROOT",
)

SCRIPT = r"""
exec > $HOME/report.txt 2>&1
echo "PREV=$(cat $HOME/marker 2>/dev/null || echo none)/$(cat /opt/marker 2>/dev/null || echo none)"
echo run > $HOME/marker; echo run > /opt/marker
echo "ENV=$(env | cut -d= -f1 | sort | tr '\n' ' ')"
echo "ALLOWED=$(curl -sS -m 20 -o /dev/null -w '%{http_code}' https://pypi.org/simple/ 2>&1)"
echo "BLOCKED=$(curl -sS -m 10 -o /dev/null -w '%{http_code}' https://github.com 2>&1; echo " rc=$?")"
echo "DIRECT=$(curl -sS -m 5 --noproxy '*' https://140.82.112.3 2>&1 | head -1; echo " rc=$?")"
python3 -m http.server 8765 --bind 127.0.0.1 >/dev/null 2>&1 &
sleep 8
kill %1
"""


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _config(root: Path):
    sb = parse_sandbox(
        {
            "sandbox": {
                "egress": {"allow": ["pypi.org:443"]},
                "env": {"allow": ["KEEP_ME"], "model_key": "MODEL_KEY_X"},
                "run": {"root": str(root), "size": "256M", "after": "archive"},
            }
        }
    )
    return SimpleNamespace(
        name="sealed-live", sandbox=sb, apptainer=None, hermes_failover=None
    )


def _one_run(cfg, state_dir: Path) -> dict:
    port = _free_port()
    run_dir = begin_run(cfg, state_dir)
    argv = [
        "apptainer", "exec", "--containall", "--cleanenv", "--home", "/home/agent",
        "--bind", f"{state_dir}/home:/home/agent",
        "--env", "KEEP_ME=1", "--env", "DROP_ME=1", "--env", "MODEL_KEY_X=not-a-real-key",
        *sandbox_flags(cfg, state_dir), SIF, *netgw_wrap(["bash", "-c", SCRIPT.replace("8765", str(port))]),
    ]  # fmt: skip
    mirror: list = []

    def probe():
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            try:
                mirror.append(
                    urllib.request.urlopen(
                        f"http://127.0.0.1:{port}/", timeout=2
                    ).status
                )
                return
            except OSError:
                time.sleep(0.5)

    threading.Thread(target=probe, daemon=True).start()
    full = supervised_argv(cfg, argv, state_dir=state_dir, image=SIF)
    rc = supervise(full[full.index("supervise") + 1 :])
    rec = end_run(cfg, state_dir)
    blocked = (run_dir / "out" / "egress_blocked.jsonl").read_text().splitlines()
    return {
        "rc": rc,
        "rec": rec,
        "report": (run_dir / "home" / "report.txt").read_text(),
        "blocked": [json.loads(b) for b in blocked],
        "mirror": mirror,
    }


@pytest.fixture(scope="module")
def runs():
    root = Path(ROOT) / f"live-{os.getpid()}"
    state_dir = root / "state" / "sealed-live"
    cfg = _config(root / "runs")
    return _one_run(cfg, state_dir), _one_run(cfg, state_dir)


def _line(report: str, key: str) -> str:
    return next(line for line in report.splitlines() if line.startswith(key + "="))


def test_allowlisted_host_is_reachable(runs):
    # Arrange
    report = runs[0]["report"]
    # Act
    line = _line(report, "ALLOWED")
    # Assert
    assert line == "ALLOWED=200"


def test_other_host_is_refused_by_the_proxy(runs):
    # Arrange
    report = runs[0]["report"]
    # Act
    line = _line(report, "BLOCKED")
    # Assert
    assert "response 403" in line


def test_refused_host_is_logged(runs):
    # Arrange
    blocked = runs[0]["blocked"]
    # Act
    hosts = {b.get("host") for b in blocked if b.get("via") == "proxy"}
    # Assert
    assert "github.com" in hosts


def test_direct_connect_is_refused_and_logged(runs):
    # Arrange
    blocked = runs[0]["blocked"]
    # Act
    ports = {b.get("port") for b in blocked if b.get("via") == "direct"}
    # Assert
    assert 443 in ports


def test_allowed_env_names_enter(runs):
    # Arrange
    report = runs[0]["report"]
    # Act
    names = set(_line(report, "ENV")[4:].split())
    # Assert
    assert {"KEEP_ME", "MODEL_KEY_X"} <= names


def test_disallowed_env_name_does_not_enter(runs):
    # Arrange
    report = runs[0]["report"]
    # Act
    names = set(_line(report, "ENV")[4:].split())
    # Assert
    assert "DROP_ME" not in names


def test_dropped_env_names_are_recorded(runs):
    # Arrange
    rec = runs[0]["rec"]
    # Act
    dropped = rec["env"]["dropped"]
    # Assert
    assert dropped == ["DROP_ME"]


def test_loopback_listener_is_mirrored_to_the_host(runs):
    # Arrange
    run = runs[0]
    # Act
    statuses = run["mirror"]
    # Assert
    assert statuses == [200]


def test_second_run_sees_no_file_of_the_first(runs):
    # Arrange
    report = runs[1]["report"]
    # Act
    line = _line(report, "PREV")
    # Assert
    assert line == "PREV=none/none"


def test_container_exit_code_is_recorded(runs):
    # Arrange
    recs = [r["rec"] for r in runs]
    # Act
    exits = [rec["exit"] for rec in recs]
    # Assert
    assert exits == [{"code": 0}, {"code": 0}]


def test_outcome_record_is_finalized(runs):
    # Arrange
    rec = runs[0]["rec"]
    # Act
    status = rec["status"]
    # Assert
    assert status == "finalized"


def test_outcome_record_has_every_field(runs):
    # Arrange
    rec = runs[0]["rec"]
    keys = (
        "started_at",
        "ended_at",
        "sac_version",
        "image",
        "egress",
        "files",
        "token_usage",
    )
    # Act
    missing = [k for k in keys if not rec.get(k)]
    # Assert
    assert missing == []


def test_image_digest_is_recorded(runs):
    # Arrange
    rec = runs[0]["rec"]
    # Act
    digest = rec["image"]["sha256"]
    # Assert
    assert len(digest) == 64


def test_home_writes_are_listed(runs):
    # Arrange
    rec = runs[0]["rec"]
    # Act
    created = rec["files"]["home_written"]["created"]
    # Assert
    assert "marker" in created


def test_overlay_writes_are_listed(runs):
    # Arrange
    ov = runs[0]["rec"]["files"]["overlay_written"]
    # Act
    created = ov["created"]
    # Assert
    assert "opt/marker" in created


def test_size_peak_is_recorded(runs):
    # Arrange
    size = runs[0]["rec"]["size"]
    # Act
    peak = size["peak_bytes"]
    # Assert
    assert 0 < peak < size["limit_mb"] * 1024 * 1024


# --------------------------------------------------------------------------
# Control-plane mask (_sandbox._hide): the agent tree of a sealed run sees
# neither its state dir nor SAC. The launching SAC (this test's interpreter
# and source) is bound read-only at its own paths, as in production.
# --------------------------------------------------------------------------
SAC_PY = sys.executable
SAC_PREFIX = Path(sys.prefix)
SAC_SRC = Path(scitex_agent_container.__file__).resolve().parents[1]
STATE = "/state/sealed-live"

HIDDEN_PROBE = f"""
exec > $HOME/hide-report.txt 2>&1
echo "STATE_LS=[$(ls -A {STATE} 2>&1 | tr '\\n' ' ')]"
echo "KEY_READ=$(cat {STATE}/hermes-api.key >/dev/null 2>&1 && echo yes || echo no)"
echo "SECRET_READ=$(cat {STATE}/secret-env/env >/dev/null 2>&1 && echo yes || echo no)"
echo "SAC_ON_PATH=[$(command -v sac)]"
echo "SAC_RUN=$({SAC_PREFIX}/bin/sac --version >/dev/null 2>&1 && echo yes || echo no)"
echo "SAC_CONTROL=$({SAC_PREFIX}/bin/sac agents list >/dev/null 2>&1 && echo yes || echo no)"
echo "SAC_PKG_LS=[$(ls -A {SAC_SRC}/scitex_agent_container 2>&1 | head -3 | tr '\\n' ' ')]"
echo "PIDS=$(ls /proc | grep -c '^[0-9]')"
echo "UNMOUNT=$(umount {STATE} >/dev/null 2>&1 && echo yes || echo no)"
echo "NESTED_UNMOUNT=$(unshare --user --map-root-user --mount sh -c 'umount {STATE} && ls -A {STATE}' >/dev/null 2>&1 && echo yes || echo no)"
echo "CHMOD=$(chmod 700 {STATE} >/dev/null 2>&1 && echo yes || echo no)"
echo "CAPS=$(awk '/^CapEff/{{print $2}}' /proc/self/status)"
exit 7
"""

HIDE = f"{SAC_PY} -m scitex_agent_container._sandbox._hide"
OWNER_SCRIPT = f"""
{HIDE} --hide {STATE} --hide {SAC_PREFIX} --hide /opt/venv-sac -- bash $HOME/hide-probe.sh
echo "HIDE_RC=$?" >> $HOME/hide-report.txt
echo "OWNER_STATE_LS=[$(ls -A {STATE} | tr '\\n' ' ')]" >> $HOME/hide-report.txt
PATH={SAC_PREFIX}/bin:$PATH {HIDE} --hide {STATE} -- touch $HOME/refused-but-ran
echo "REFUSED_RC=$?" >> $HOME/hide-report.txt
echo "REFUSED_RAN=$(test -e $HOME/refused-but-ran && echo yes || echo no)" >> $HOME/hide-report.txt
"""


def _hidden_run(root: Path) -> str:
    sb = parse_sandbox(
        {
            "sandbox": {
                "egress": {"allow": ["pypi.org:443"]},
                "env": {"allow": ["PYTHONPATH"], "model_key": "MODEL_KEY_X"},
                "run": {"root": str(root / "runs"), "size": "256M", "after": "archive"},
            }
        }
    )
    cfg = SimpleNamespace(
        name="sealed-live", sandbox=sb, apptainer=None, hermes_failover=None
    )
    state_dir = root / "state" / "sealed-live"
    (state_dir / "secret-env").mkdir(parents=True, exist_ok=True)
    (state_dir / "hermes-api.key").write_text("not-a-real-key-0123456789\n")
    (state_dir / "secret-env" / "env").write_text("MODEL_KEY_X=not-a-real-key\n")
    run_dir = begin_run(cfg, state_dir)
    (run_dir / "home" / "hide-probe.sh").write_text(HIDDEN_PROBE)
    python_root = Path(sys.base_prefix).parent
    argv = [
        "apptainer", "exec", "--containall", "--cleanenv", "--home", "/home/agent",
        "--bind", f"{state_dir}/home:/home/agent",
        "--bind", f"{state_dir}:{STATE}",
        "--bind", f"{SAC_PREFIX}:{SAC_PREFIX}:ro",
        "--bind", f"{python_root}:{python_root}:ro",
        "--bind", f"{SAC_SRC}:{SAC_SRC}:ro",
        "--env", f"PYTHONPATH={SAC_SRC}",
        *sandbox_flags(cfg, state_dir), SIF, *netgw_wrap(["bash", "-c", OWNER_SCRIPT]),
    ]  # fmt: skip
    full = supervised_argv(cfg, argv, state_dir=state_dir, image=SIF)
    supervise(full[full.index("supervise") + 1 :])
    end_run(cfg, state_dir)
    return (run_dir / "home" / "hide-report.txt").read_text()


@pytest.fixture(scope="module")
def hidden():
    return _hidden_run(Path(ROOT) / f"hide-{os.getpid()}")


def test_agent_lists_nothing_in_its_state_dir(hidden):
    # Arrange
    report = hidden
    # Act
    line = _line(report, "STATE_LS")
    # Assert
    assert line == f"STATE_LS=[ls: cannot open directory '{STATE}': Permission denied ]"


def test_agent_cannot_read_the_gateway_api_key(hidden):
    # Arrange
    report = hidden
    # Act
    line = _line(report, "KEY_READ")
    # Assert
    assert line == "KEY_READ=no"


def test_agent_cannot_read_the_secret_env_file(hidden):
    # Arrange
    report = hidden
    # Act
    line = _line(report, "SECRET_READ")
    # Assert
    assert line == "SECRET_READ=no"


def test_agent_finds_no_sac_on_its_path(hidden):
    # Arrange
    report = hidden
    # Act
    line = _line(report, "SAC_ON_PATH")
    # Assert
    assert line == "SAC_ON_PATH=[]"


def test_agent_cannot_run_the_launching_sac(hidden):
    # Arrange
    report = hidden
    # Act
    line = _line(report, "SAC_RUN")
    # Assert
    assert line == "SAC_RUN=no"


def test_agent_cannot_invoke_a_sac_control_action(hidden):
    # Arrange
    report = hidden
    # Act
    line = _line(report, "SAC_CONTROL")
    # Assert
    assert line == "SAC_CONTROL=no"


def test_agent_cannot_list_the_sac_package_source(hidden):
    # Arrange
    report = hidden
    # Act
    line = _line(report, "SAC_PKG_LS")
    # Assert
    assert "Permission denied" in line


def test_agent_sees_only_its_own_processes(hidden):
    # Arrange
    report = hidden
    # Act
    count = int(_line(report, "PIDS").split("=")[1])
    # Assert
    assert count < 8


def test_agent_cannot_unmount_the_mask(hidden):
    # Arrange
    report = hidden
    # Act
    line = _line(report, "UNMOUNT")
    # Assert
    assert line == "UNMOUNT=no"


def test_nested_namespace_cannot_unmount_the_mask(hidden):
    # Arrange
    report = hidden
    # Act
    line = _line(report, "NESTED_UNMOUNT")
    # Assert
    assert line == "NESTED_UNMOUNT=no"


def test_agent_cannot_reopen_the_mask_by_chmod(hidden):
    # Arrange
    report = hidden
    # Act
    line = _line(report, "CHMOD")
    # Assert
    assert line == "CHMOD=no"


def test_agent_holds_no_capabilities(hidden):
    # Arrange
    report = hidden
    # Act
    line = _line(report, "CAPS")
    # Assert
    assert line == "CAPS=0000000000000000"


def test_agent_exit_status_reaches_the_owner(hidden):
    # Arrange
    report = hidden
    # Act
    line = _line(report, "HIDE_RC")
    # Assert
    assert line == "HIDE_RC=7"


def test_owner_still_sees_the_state_dir(hidden):
    # Arrange
    report = hidden
    # Act
    line = _line(report, "OWNER_STATE_LS")
    # Assert
    assert "hermes-api.key" in line


def test_launcher_refuses_while_sac_stays_runnable(hidden):
    # Arrange
    report = hidden
    # Act
    line = _line(report, "REFUSED_RC")
    # Assert
    assert line == "REFUSED_RC=125"


def test_refused_command_never_runs(hidden):
    # Arrange
    report = hidden
    # Act
    line = _line(report, "REFUSED_RAN")
    # Assert
    assert line == "REFUSED_RAN=no"
