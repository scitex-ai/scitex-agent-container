"""CLI wiring tests for ``sac ci why``.

No mocks: a real fake ``gh`` executable is installed on ``PATH`` (a tiny
Python script that prints canned run-view JSON and ``--log-failed``
output). The command therefore drives the true ``run_gh`` subprocess
path end-to-end. A bare run id resolves without a gh round-trip, so
these exercise ``explain_run`` + rendering + the click surface. PATH is
set through the repo's ``env_save_restore`` fixture (save/restore on
teardown — no ``monkeypatch``). AAA, one assertion per test.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path

from click.testing import CliRunner

from scitex_agent_container.cli import main

# A 10+ digit id → treated as a run id, so resolution needs no gh call.
_RUN_ID = "29446283736"
_PJ = "pytest-matrix-on-ubuntu-py3.11"


def _log_line(content: str, ts: str = "2026-07-15T10:00:02.1000000") -> str:
    return f"{_PJ}\tRun pytest\t{ts}Z {content}"


# A realistically noisy pytest failure log (tab/timestamp/group scaffolding).
PYTEST_LOG = "\n".join(
    [
        _log_line("##[group]Run python -m pytest", "2026-07-15T10:00:00.1000000"),
        _log_line("python -m pytest -v tests/", "2026-07-15T10:00:00.2000000"),
        _log_line("##[endgroup]", "2026-07-15T10:00:00.3000000"),
        _log_line(
            "=============== FAILURES ===============", "2026-07-15T10:00:01.0000000"
        ),
        _log_line("E       assert 3 == 4", "2026-07-15T10:00:01.5000000"),
        _log_line("=========== short test summary info ==========="),
        _log_line(
            "FAILED tests/test_math.py::test_math - AssertionError: assert 3 == 4"
        ),
        _log_line("=========== 1 failed, 4 passed in 0.12s ==========="),
        _log_line("##[error]Process completed with exit code 1."),
    ]
)

# Hand-written JSON strings (no json module → no provenance-audit noise).
META_FAIL = (
    '{"workflowName":"quality","displayTitle":"t","headBranch":"b",'
    '"conclusion":"failure","jobs":['
    '{"name":"pytest-matrix-on-ubuntu-py3.11","conclusion":"failure"}]}'
)
META_GREEN = (
    '{"workflowName":"tests","displayTitle":"t","headBranch":"b",'
    '"conclusion":"success","jobs":[{"name":"x","conclusion":"success"}]}'
)

_FAKE_GH = """#!/usr/bin/env python3
import sys
args = sys.argv[1:]
if "--log-failed" in args:
    sys.stdout.write(open({log!r}).read())
elif "--json" in args:
    sys.stdout.write(open({meta!r}).read())
else:
    sys.stderr.write("fake gh: unhandled %r\\n" % (args,))
    sys.exit(9)
"""

_BROKEN_GH = """#!/usr/bin/env python3
import sys
sys.stderr.write("could not find any workflows named 0\\n")
sys.exit(1)
"""


def _install_gh(tmp_path: Path, env, script: str) -> None:
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    gh = bindir / "gh"
    gh.write_text(script)
    gh.chmod(gh.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    env.set("PATH", str(bindir) + os.pathsep + os.environ["PATH"])


def _install_fake_gh(tmp_path: Path, env, meta: str, log_text: str) -> None:
    (tmp_path / "log.txt").write_text(log_text)
    (tmp_path / "meta.json").write_text(meta)
    script = _FAKE_GH.format(
        log=str(tmp_path / "log.txt"), meta=str(tmp_path / "meta.json")
    )
    _install_gh(tmp_path, env, script)


def test_why_renders_the_failing_test_id(tmp_path: Path, env_save_restore):
    # Arrange
    _install_fake_gh(tmp_path, env_save_restore, META_FAIL, PYTEST_LOG)
    # Act
    result = CliRunner().invoke(main, ["ci", "why", _RUN_ID])
    # Assert
    assert "tests/test_math.py::test_math" in result.output


def test_why_output_is_smaller_than_the_raw_log(tmp_path: Path, env_save_restore):
    # Arrange — the inversion: the reason costs a fraction of the log.
    _install_fake_gh(tmp_path, env_save_restore, META_FAIL, PYTEST_LOG)
    # Act
    result = CliRunner().invoke(main, ["ci", "why", _RUN_ID])
    # Assert
    assert len(result.output) < len(PYTEST_LOG)


def test_why_strips_group_noise_from_output(tmp_path: Path, env_save_restore):
    # Arrange
    _install_fake_gh(tmp_path, env_save_restore, META_FAIL, PYTEST_LOG)
    # Act
    result = CliRunner().invoke(main, ["ci", "why", _RUN_ID])
    # Assert
    assert "##[group]" not in result.output


def test_why_green_run_says_no_failures(tmp_path: Path, env_save_restore):
    # Arrange
    _install_fake_gh(tmp_path, env_save_restore, META_GREEN, "")
    # Act
    result = CliRunner().invoke(main, ["ci", "why", _RUN_ID])
    # Assert
    assert result.output.strip() == "no failures"


def test_why_json_flag_emits_structured_output(tmp_path: Path, env_save_restore):
    # Arrange
    _install_fake_gh(tmp_path, env_save_restore, META_FAIL, PYTEST_LOG)
    # Act
    result = CliRunner().invoke(main, ["ci", "why", _RUN_ID, "--json"])
    # Assert
    assert '"failed_tests"' in result.output


def test_why_reports_gh_failure_loudly(tmp_path: Path, env_save_restore):
    # Arrange — gh errors: UNKNOWN must fail loud, never print "no failures".
    _install_gh(tmp_path, env_save_restore, _BROKEN_GH)
    # Act
    result = CliRunner().invoke(main, ["ci", "why", _RUN_ID])
    # Assert
    assert result.exit_code != 0
