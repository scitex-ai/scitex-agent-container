"""LIVE ``sac sandbox exec``: a sealed plain command in a real apptainer image.

Skipped unless BOTH are set (CI has neither, by design — it needs a host
with apptainer, user namespaces and outbound HTTPS):

* ``SAC_SANDBOX_TEST_SIF``  — an image with bash, curl, python3 (+pip), unshare;
* ``SAC_SANDBOX_TEST_ROOT`` — a durable scratch dir for run roots (never /tmp).

Drives the real CLI (``sac sandbox exec``) in a subprocess, exactly as a
caller such as scitex-agentic-journal would, and asserts: the command's
exit status reaches the caller and the record; an allowlisted host is
reachable and a non-allowlisted one refused AND logged; no egress at all
without ``--egress-allow``; a caller env var reaches the command only when
named by ``--env-allow`` (value intact); SAC itself is not visible; a second
run sees none of the first run's files; ``--timeout`` stops the command and
says so; a SIGTERM to sac is attributed to the caller; a ``pip download``
restricted to the package index works; ``--run-root`` under /tmp is refused;
outcome.json is complete.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

import scitex_agent_container

SIF = os.environ.get("SAC_SANDBOX_TEST_SIF", "")
ROOT = os.environ.get("SAC_SANDBOX_TEST_ROOT", "")
pytestmark = pytest.mark.skipif(
    not (SIF and ROOT and Path(SIF).is_file() and shutil.which("apptainer")),
    reason="live sealed-run test: set SAC_SANDBOX_TEST_SIF and SAC_SANDBOX_TEST_ROOT",
)

SAC_SRC = Path(scitex_agent_container.__file__).resolve().parents[1]
SAC = [
    sys.executable,
    "-c",
    "from scitex_agent_container.cli_pkg._main import main; main()",
]
#: Every character apptainer carries intact (`$ ` " ' \` are refused up front).
PASS_VALUE = "a b!*?~#%&()[]{}<>|;:,.=+-_@^/ end"

SCRIPT = f"""
echo "PREV=$(cat $HOME/marker 2>/dev/null || echo none)/$(cat /opt/marker 2>/dev/null || echo none)"
echo run > $HOME/marker; echo run > /opt/marker
echo "ENV=$(env | cut -d= -f1 | sort | tr '\\n' ' ')"
echo "PASS=[$SAC_PASS_ME]"
echo "SAC_SRC=$(test -e {SAC_SRC} && echo yes || echo no)"
echo "ALLOWED=$(curl -sS -m 20 -o /dev/null -w '%{{http_code}}' https://pypi.org/simple/pip/ 2>&1)"
echo "BLOCKED=$(curl -sS -m 10 -o /dev/null -w '%{{http_code}}' https://github.com 2>&1; echo " rc=$?")"
echo to-stderr >&2
exit 7
"""


def _caller_env() -> dict[str, str]:
    return {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": os.environ.get("HOME", "/"),
        "PYTHONPATH": str(SAC_SRC),
        "SCITEX_AGENT_CONTAINER_FRESHNESS_QUIET": "1",
        "SAC_SECRET_SENTINEL": "sentinel-not-for-the-command",
        "SAC_PASS_ME": PASS_VALUE,
        "SAC_UNSAFE": "costs $5",
    }


def _argv(
    root: Path, *extra: str, command: list[str], timeout: str = "120"
) -> list[str]:
    return [*SAC, "sandbox", "exec", "--image", SIF, "--run-root", str(root),
            "--timeout", timeout, *extra, "--", *command]  # fmt: skip


def _finish(proc: subprocess.Popen) -> dict:
    out, err = proc.communicate(timeout=600)
    m = re.search(r"outcome: (\S+)", err.decode())
    rec = json.loads(Path(m.group(1)).read_text()) if m else {}
    return {"rc": proc.returncode, "out": out.decode(), "err": err.decode(), "rec": rec}


def _sac(root: Path, *extra: str, command: list[str], timeout: str = "120") -> dict:
    proc = subprocess.Popen(
        _argv(root, *extra, command=command, timeout=timeout),
        env=_caller_env(),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    return _finish(proc)


def _stopped_by_sigterm(root: Path) -> dict:
    proc = subprocess.Popen(
        _argv(root, command=["sleep", "120"]),
        env=_caller_env(),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        recs = [json.loads(p.read_text()) for p in root.glob("exec/*/out/outcome.json")]
        if recs and recs[0].get("status") == "running":
            break
        time.sleep(0.2)
    time.sleep(3)
    proc.send_signal(signal.SIGTERM)
    return _finish(proc)


@pytest.fixture(scope="module")
def runs():
    base = Path(ROOT) / f"exec-live-{os.getpid()}"
    sealed = ["--egress-allow", "pypi.org:443", "--env-allow", "SAC_PASS_ME"]
    first = _sac(base / "main", *sealed, command=["bash", "-c", SCRIPT])
    second = _sac(base / "main", *sealed, command=["bash", "-c", SCRIPT])
    return {
        "first": first,
        "second": second,
        "noegress": _sac(base / "noegress", command=["bash", "-c", SCRIPT]),
        "timeout": _sac(
            base / "timeout", "--after", "remove", command=["sleep", "120"], timeout="5"
        ),  # fmt: skip
        "sigterm": _stopped_by_sigterm(base / "sigterm"),
        "pip": _sac(
            base / "pip",
            "--egress-allow",
            "pypi.org:443",
            "--egress-allow",
            "files.pythonhosted.org:443",
            command=[
                "python3",
                "-m",
                "pip",
                "download",
                "--no-deps",
                "--disable-pip-version-check",
                "-d",
                "/home/agent/dl",
                "six",
            ],
        ),  # fmt: skip
        "tmp": _sac(Path("/tmp") / f"sac-exec-live-{os.getpid()}", command=["true"]),
        "unsafe": _sac(base / "unsafe", "--env-allow", "SAC_UNSAFE", command=["true"]),
    }


def _line(text: str, key: str) -> str:
    return next(line for line in text.splitlines() if line.startswith(key + "="))


def test_command_exit_status_reaches_the_caller(runs):
    # Arrange
    run = runs["first"]
    # Act
    rc = run["rc"]
    # Assert
    assert rc == 7


def test_command_exit_status_is_recorded(runs):
    # Arrange
    rec = runs["first"]["rec"]
    # Act
    exit_doc = rec["exit"]
    # Assert
    assert exit_doc == {"code": 7}


def test_command_stdout_streams_to_the_caller(runs):
    # Arrange
    out = runs["first"]["out"]
    # Act
    line = _line(out, "ALLOWED")
    # Assert
    assert line == "ALLOWED=200"


def test_command_stderr_streams_to_the_caller(runs):
    # Arrange
    err = runs["first"]["err"]
    # Act
    lines = err.splitlines()
    # Assert
    assert "to-stderr" in lines


def test_command_output_is_kept_in_the_run(runs):
    # Arrange
    rec = runs["first"]["rec"]
    # Act
    kept = Path(rec["output"]["stdout"]).read_text()
    # Assert
    assert kept == runs["first"]["out"]


def test_other_host_is_refused_by_the_proxy(runs):
    # Arrange
    out = runs["first"]["out"]
    # Act
    line = _line(out, "BLOCKED")
    # Assert
    assert "response 403" in line


def test_refused_host_is_logged(runs):
    # Arrange
    rec = runs["first"]["rec"]
    # Act
    blocked = Path(rec["egress"]["logs"]["blocked"]).read_text()
    # Assert
    assert '"host": "github.com"' in blocked


def test_allowed_host_is_logged(runs):
    # Arrange
    rec = runs["first"]["rec"]
    # Act
    allowed = rec["egress"]["allowed"]
    # Assert
    assert allowed == {"pypi.org:443": 1}


def test_no_egress_without_an_allowlist(runs):
    # Arrange
    out = runs["noegress"]["out"]
    # Act
    line = _line(out, "ALLOWED")
    # Assert
    assert "response 403" in line


def test_unlisted_caller_variable_is_invisible(runs):
    # Arrange
    out = runs["first"]["out"]
    # Act
    names = set(_line(out, "ENV")[4:].split())
    # Assert
    assert "SAC_SECRET_SENTINEL" not in names


def test_unlisted_caller_variable_is_recorded_as_dropped(runs):
    # Arrange
    rec = runs["first"]["rec"]
    # Act
    dropped = rec["env"]["dropped"]
    # Assert
    assert "SAC_SECRET_SENTINEL" in dropped


def test_listed_caller_variable_arrives_intact(runs):
    # Arrange
    out = runs["first"]["out"]
    # Act
    line = _line(out, "PASS")
    # Assert
    assert line == f"PASS=[{PASS_VALUE}]"


def test_listed_caller_variable_is_recorded_as_passed(runs):
    # Arrange
    rec = runs["first"]["rec"]
    # Act
    passed = rec["env"]["passed"]
    # Assert
    assert "SAC_PASS_ME" in passed


def test_sac_itself_is_not_visible(runs):
    # Arrange
    out = runs["first"]["out"]
    # Act
    line = _line(out, "SAC_SRC")
    # Assert
    assert line == "SAC_SRC=no"


def test_first_run_starts_empty(runs):
    # Arrange
    out = runs["first"]["out"]
    # Act
    line = _line(out, "PREV")
    # Assert
    assert line == "PREV=none/none"


def test_second_run_sees_no_file_of_the_first(runs):
    # Arrange
    out = runs["second"]["out"]
    # Act
    line = _line(out, "PREV")
    # Assert
    assert line == "PREV=none/none"


def test_timeout_ends_the_run_with_status_124(runs):
    # Arrange
    run = runs["timeout"]
    # Act
    rc = run["rc"]
    # Assert
    assert rc == 124


def test_timeout_is_recorded(runs):
    # Arrange
    rec = runs["timeout"]["rec"]
    # Act
    exit_doc = rec["exit"]
    # Assert
    assert exit_doc["timed_out"] is True and exit_doc["reason"].startswith("timeout")


def test_after_remove_keeps_only_the_out_dir(runs):
    # Arrange
    run_dir = Path(runs["timeout"]["rec"]["run_dir"])
    # Act
    left = sorted(p.name for p in run_dir.iterdir())
    # Assert
    assert left == ["out"]


def test_sigterm_to_sac_is_attributed_to_the_caller(runs):
    # Arrange
    rec = runs["sigterm"]["rec"]
    # Act
    exit_doc = rec["exit"]
    # Assert
    assert (exit_doc["stopped_by"], exit_doc["stop_signal"]) == ("caller", 15)


def test_pip_download_through_the_package_index_succeeds(runs):
    # Arrange
    run = runs["pip"]
    # Act
    rc = run["rc"]
    # Assert
    assert rc == 0, run["err"][-2000:]


def test_pip_download_writes_the_wheel_into_the_run(runs):
    # Arrange
    rec = runs["pip"]["rec"]
    # Act
    created = rec["files"]["home_written"]["created"]
    # Assert
    assert any(c.startswith("dl/six-") for c in created)


def test_tmp_run_root_is_refused(runs):
    # Arrange
    run = runs["tmp"]
    # Act
    rc = run["rc"]
    # Assert
    assert rc == 2 and "never tmp" in run["err"]


def test_outcome_record_is_complete(runs):
    # Arrange
    rec = runs["first"]["rec"]
    keys = ("prepared_at", "started_at", "ended_at", "finalized_at", "sac_version",
            "image", "egress", "env", "files", "exit", "command", "policy")  # fmt: skip
    # Act
    missing = [k for k in keys if not rec.get(k)]
    # Assert
    assert missing == []


def test_outcome_record_is_finalized(runs):
    # Arrange
    rec = runs["first"]["rec"]
    # Act
    status = (rec["status"], rec["kind"])
    # Assert
    assert status == ("finalized", "command")


def test_image_digest_is_recorded(runs):
    # Arrange
    rec = runs["first"]["rec"]
    # Act
    digest = rec["image"]["sha256"]
    # Assert
    assert re.fullmatch(r"[0-9a-f]{64}", digest)


def test_files_written_are_listed(runs):
    # Arrange
    files = runs["first"]["rec"]["files"]
    # Act
    written = (files["home_written"]["created"], files["overlay_written"]["created"])
    # Assert
    assert ("marker" in written[0]) and ("opt/marker" in written[1])


def test_value_apptainer_would_rewrite_is_refused_up_front(runs):
    # Arrange
    run = runs["unsafe"]
    # Act
    rc = run["rc"]
    # Assert
    assert rc == 2 and "apptainer would rewrite" in run["err"]


def test_refused_value_leaves_no_run_behind(runs):
    # Arrange
    root = Path(ROOT) / f"exec-live-{os.getpid()}" / "unsafe"
    # Act
    exists = root.exists()
    # Assert
    assert exists is False
