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
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import threading
import time
import urllib.request
from pathlib import Path
from types import SimpleNamespace

import pytest

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
    return SimpleNamespace(name="sealed-live", sandbox=sb, apptainer=None, hermes_failover=None)


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
                mirror.append(urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=2).status)
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
    keys = ("started_at", "ended_at", "sac_version", "image", "egress", "files", "token_usage")
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
