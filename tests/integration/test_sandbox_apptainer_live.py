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
    assert _line(runs[0]["report"], "ALLOWED") == "ALLOWED=200"


def test_other_host_is_refused_by_the_proxy(runs):
    assert "response 403" in _line(runs[0]["report"], "BLOCKED")


def test_refused_host_is_logged(runs):
    assert any(b.get("via") == "proxy" and b.get("host") == "github.com" for b in runs[0]["blocked"])


def test_direct_connect_is_refused_and_logged(runs):
    assert any(b.get("via") == "direct" and b.get("port") == 443 for b in runs[0]["blocked"])


def test_only_allowed_env_names_enter(runs):
    names = set(_line(runs[0]["report"], "ENV")[4:].split())
    assert {"KEEP_ME", "MODEL_KEY_X"} <= names and "DROP_ME" not in names


def test_dropped_env_names_are_recorded(runs):
    assert runs[0]["rec"]["env"]["dropped"] == ["DROP_ME"]


def test_loopback_listener_is_mirrored_to_the_host(runs):
    assert runs[0]["mirror"] == [200]


def test_second_run_sees_no_file_of_the_first(runs):
    assert _line(runs[1]["report"], "PREV") == "PREV=none/none"


def test_container_exit_code_is_recorded(runs):
    assert [r["rec"]["exit"] for r in runs] == [{"code": 0}, {"code": 0}]


def test_outcome_record_is_complete(runs):
    rec = runs[0]["rec"]
    keys = ("started_at", "ended_at", "sac_version", "image", "egress", "files", "token_usage")
    assert rec["status"] == "finalized" and all(rec.get(k) for k in keys)


def test_image_digest_is_recorded(runs):
    assert len(runs[0]["rec"]["image"]["sha256"]) == 64


def test_home_writes_are_listed(runs):
    assert "marker" in runs[0]["rec"]["files"]["home_written"]["created"]


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
