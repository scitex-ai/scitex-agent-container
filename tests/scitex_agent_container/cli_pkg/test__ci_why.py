"""Tests for the ``sac ci why`` failure-extraction core (``_ci_why``).

No mocks. The parser is exercised on real-shaped GitHub-Actions log
STRINGS (timestamp prefixes, ``##[group]`` noise, a ``FAILURES`` block,
a ``short test summary info`` block, and a setup ``##[error]`` log). The
run-level resolver is exercised through the injectable ``run_gh`` seam —
a plain callable returning canned output — so nothing here touches the
network. AAA, one logical assertion per test.
"""

from __future__ import annotations

import json
import subprocess

from scitex_agent_container.cli_pkg._ci_why import (
    CIWhyError,
    RunFailures,
    clean_log_line,
    explain_run,
    parse_failed_log,
    parse_job_context,
    render_text,
    resolve_run_ids,
    run_gh,
    split_log_by_job,
)

# ---------------------------------------------------------------------------
# Real-shaped fixtures: gh --log-failed prefixes each line
# "<job>\t<step>\t<ISO-timestamp>Z <content>".
# ---------------------------------------------------------------------------

_PJ = "pytest-matrix-on-ubuntu-py3.11"
_PS = "Run pytest"


def _line(job: str, step: str, ts: str, content: str) -> str:
    return f"{job}\t{step}\t{ts}Z {content}"


PYTEST_LOG = "\n".join(
    _line(_PJ, _PS, ts, content)
    for ts, content in [
        ("2026-07-15T10:00:00.1000000", "##[group]Run python -m pytest"),
        ("2026-07-15T10:00:00.2000000", "python -m pytest -v"),
        ("2026-07-15T10:00:00.3000000", "##[endgroup]"),
        ("2026-07-15T10:00:01.0000000", "=============== FAILURES ==============="),
        ("2026-07-15T10:00:01.1000000", "_______________ test_math _______________"),
        ("2026-07-15T10:00:01.3000000", "    def test_math():"),
        ("2026-07-15T10:00:01.4000000", ">       assert 3 == 4"),
        ("2026-07-15T10:00:01.5000000", "E       assert 3 == 4"),
        ("2026-07-15T10:00:01.7000000", "tests/test_math.py:5: AssertionError"),
        (
            "2026-07-15T10:00:02.0000000",
            "=========== short test summary info ===========",
        ),
        (
            "2026-07-15T10:00:02.1000000",
            "FAILED tests/test_math.py::test_math - AssertionError: assert 3 == 4",
        ),
        (
            "2026-07-15T10:00:02.2000000",
            "=========== 1 failed, 4 passed in 0.12s ===========",
        ),
        ("2026-07-15T10:00:02.3000000", "##[error]Process completed with exit code 1."),
    ]
)

_SJ = "no-hosted-runners-guard-on-self-hosted"
_SS = "Run astral-sh/setup-uv@v3"

SETUP_LOG = "\n".join(
    _line(_SJ, _SS, ts, content)
    for ts, content in [
        ("2026-07-15T15:59:17.6765113", "##[group]Run astral-sh/setup-uv@v3"),
        ("2026-07-15T15:59:17.6769300", "##[endgroup]"),
        (
            "2026-07-15T15:59:17.7931495",
            'Downloading uv from "https://x/uv.tar.gz" ...',
        ),
        (
            "2026-07-15T15:59:18.4499226",
            "ENOENT: no such file or directory, open '/data/_temp/99eb7246'",
        ),
        (
            "2026-07-15T15:59:48.7185987",
            "##[error]ENOENT: no such file or directory, open '/data/_temp/99eb7246'",
        ),
    ]
)

TAIL_LOG = "\n".join(
    _line("some-job-on-ubuntu-latest", "Build", ts, content)
    for ts, content in [
        ("2026-07-15T10:00:00.1000000", "##[group]Build"),
        ("2026-07-15T10:00:00.2000000", "compiling module foo"),
        ("2026-07-15T10:00:00.3000000", "##[endgroup]"),
        ("2026-07-15T10:00:00.4000000", "linker: undefined reference to bar"),
    ]
)


# ---------------------------------------------------------------------------
# clean_log_line — strips scaffolding.
# ---------------------------------------------------------------------------


def test_clean_log_line_strips_job_prefix_and_timestamp():
    # Arrange
    raw = _line(_PJ, _PS, "2026-07-15T10:00:02.1000000", "FAILED tests/t.py::t - X")
    # Act
    cleaned = clean_log_line(raw)
    # Assert
    assert cleaned == "FAILED tests/t.py::t - X"


def test_clean_log_line_drops_group_markers():
    # Arrange
    raw = _line(_PJ, _PS, "2026-07-15T10:00:00.1000000", "##[group]Run x")
    # Act
    cleaned = clean_log_line(raw)
    # Assert
    assert cleaned is None


def test_clean_log_line_keeps_error_annotation():
    # Arrange
    raw = _line(_SJ, _SS, "2026-07-15T15:59:48.7185987", "##[error]ENOENT: boom")
    # Act
    cleaned = clean_log_line(raw)
    # Assert
    assert cleaned == "##[error]ENOENT: boom"


# ---------------------------------------------------------------------------
# split_log_by_job — groups by first tab column.
# ---------------------------------------------------------------------------


def test_split_log_by_job_separates_two_jobs():
    # Arrange
    text = "\n".join(
        [
            _line("job-a", "s", "2026-07-15T10:00:00.1000000", "a1"),
            _line("job-b", "s", "2026-07-15T10:00:00.2000000", "b1"),
            _line("job-a", "s", "2026-07-15T10:00:00.3000000", "a2"),
        ]
    )
    # Act
    groups = split_log_by_job(text)
    # Assert
    assert list(groups) == ["job-a", "job-b"]


# ---------------------------------------------------------------------------
# parse_job_context — python version + OS from the job name.
# ---------------------------------------------------------------------------


def test_parse_job_context_dotted_pyversion_and_os():
    # Arrange
    name = "pytest-matrix-on-ubuntu-py3.11"
    # Act
    py, os_ = parse_job_context(name)
    # Assert
    assert (py, os_) == ("3.11", "ubuntu")


def test_parse_job_context_dashed_pyversion():
    # Arrange
    name = "import-smoke-on-ubuntu-py3-12"
    # Act
    py, _os = parse_job_context(name)
    # Assert
    assert py == "3.12"


def test_parse_job_context_self_hosted_has_no_python():
    # Arrange
    name = "no-hosted-runners-guard-on-self-hosted"
    # Act
    py, os_ = parse_job_context(name)
    # Assert
    assert (py, os_) == (None, "self-hosted")


# ---------------------------------------------------------------------------
# parse_failed_log — the testable core.
# ---------------------------------------------------------------------------


def test_parse_failed_log_extracts_failing_test_id():
    # Arrange
    log = PYTEST_LOG
    # Act
    fail = parse_failed_log(log, job_name=_PJ)
    # Assert
    assert fail.failed_tests == [
        "FAILED tests/test_math.py::test_math - AssertionError: assert 3 == 4"
    ]


def test_parse_failed_log_extracts_assertion_line():
    # Arrange
    log = PYTEST_LOG
    # Act
    fail = parse_failed_log(log, job_name=_PJ)
    # Assert
    assert any("assert 3 == 4" in a for a in fail.assertions)


def test_parse_failed_log_strips_timestamp_and_group_noise():
    # Arrange
    log = PYTEST_LOG
    # Act
    fail = parse_failed_log(log, job_name=_PJ)
    # Assert — no scaffolding leaks into the extracted signal.
    leaked = [
        line
        for line in fail.failed_tests + fail.assertions
        if "2026-" in line or "##[group]" in line
    ]
    assert leaked == []


def test_parse_failed_log_carries_matrix_context():
    # Arrange
    log = PYTEST_LOG
    # Act
    fail = parse_failed_log(log, job_name=_PJ)
    # Assert
    assert (fail.py, fail.os) == ("3.11", "ubuntu")


def test_parse_failed_log_setup_failure_signal_is_annotation():
    # Arrange
    log = SETUP_LOG
    # Act
    fail = parse_failed_log(log, job_name=_SJ)
    # Assert
    assert fail.signal == "annotation"


def test_parse_failed_log_setup_failure_surfaces_enoent():
    # Arrange
    log = SETUP_LOG
    # Act
    fail = parse_failed_log(log, job_name=_SJ)
    # Assert
    assert any("ENOENT" in e for e in fail.errors)


def test_parse_failed_log_falls_back_to_tail_when_no_signal():
    # Arrange
    log = TAIL_LOG
    # Act
    fail = parse_failed_log(log, job_name="some-job-on-ubuntu-latest")
    # Assert
    assert "linker: undefined reference to bar" in fail.tail


# ---------------------------------------------------------------------------
# render_text — compact human output.
# ---------------------------------------------------------------------------


def test_render_text_says_no_failures_when_empty():
    # Arrange
    run = RunFailures(run_id="1")
    # Act
    rendered = render_text(run)
    # Assert
    assert rendered == "no failures"


def test_render_text_is_far_smaller_than_the_raw_log():
    # Arrange — the whole point: the reason costs a fraction of the log.
    jf = parse_failed_log(PYTEST_LOG, job_name=_PJ)
    run = RunFailures(run_id="1", failures=[jf])
    # Act
    rendered = render_text(run)
    # Assert
    assert len(rendered) < len(PYTEST_LOG) // 2


def test_render_text_includes_the_failing_test_id():
    # Arrange
    jf = parse_failed_log(PYTEST_LOG, job_name=_PJ)
    run = RunFailures(run_id="1", failures=[jf])
    # Act
    rendered = render_text(run)
    # Assert
    assert "tests/test_math.py::test_math" in rendered


# ---------------------------------------------------------------------------
# run_gh seam — honest failure, and gh-pr-checks non-zero semantics.
# ---------------------------------------------------------------------------


def test_run_gh_raises_when_gh_missing():
    # Arrange
    def _no_gh(*_a, **_kw):
        raise FileNotFoundError("gh")

    captured = None
    # Act
    try:
        run_gh(["run", "list"], _run=_no_gh)
    except CIWhyError as exc:
        captured = exc
    # Assert
    assert captured is not None


def test_run_gh_returns_stdout_on_nonzero_with_output():
    # Arrange — gh pr checks exits non-zero when checks fail, yet prints JSON.
    def _fake(argv, **_kw):
        return subprocess.CompletedProcess(argv, 1, stdout='[{"a":1}]', stderr="")

    # Act
    out = run_gh(["pr", "checks", "712"], _run=_fake)
    # Assert
    assert out == '[{"a":1}]'


def test_run_gh_raises_on_nonzero_with_empty_output():
    # Arrange — a bad run id: non-zero AND nothing on stdout is a real error.
    def _fake(argv, **_kw):
        return subprocess.CompletedProcess(argv, 1, stdout="", stderr="not found")

    captured = None
    # Act
    try:
        run_gh(["run", "view", "0"], _run=_fake)
    except CIWhyError as exc:
        captured = exc
    # Assert
    assert captured is not None


# ---------------------------------------------------------------------------
# Resolver + explain_run — through the injected run_gh seam (no network).
# ---------------------------------------------------------------------------


def test_resolve_run_ids_treats_large_number_as_run_id_without_calling_gh():
    # Arrange — a run id must not cost a gh round-trip.
    def _boom(_args):
        raise RuntimeError("gh should not be called for a bare run id")

    # Act
    ids = resolve_run_ids("29446283736", run_gh=_boom)
    # Assert
    assert ids == ["29446283736"]


def test_resolve_run_ids_pr_number_extracts_failing_run_id():
    # Arrange — one failing check, its link carries the run id.
    checks = [
        {"bucket": "pass", "state": "SUCCESS", "link": ".../actions/runs/111/job/1"},
        {
            "bucket": "fail",
            "state": "FAILURE",
            "link": "https://github.com/o/r/actions/runs/29446283736/job/9",
        },
    ]

    def _fake(_args):
        return json.dumps(checks)

    # Act
    ids = resolve_run_ids("712", run_gh=_fake)
    # Assert
    assert ids == ["29446283736"]


def _gh_router(jobs: list[dict], log_text: str):
    """A canned run_gh: run-view JSON on --json, the log on --log-failed."""

    def _router(args: list[str]) -> str:
        if "--log-failed" in args:
            return log_text
        if "--json" in args:
            return json.dumps(
                {
                    "workflowName": "tests",
                    "displayTitle": "t",
                    "headBranch": "b",
                    "jobs": jobs,
                }
            )
        raise RuntimeError(f"unexpected gh args: {args}")

    return _router


def test_explain_run_green_has_no_failures():
    # Arrange
    router = _gh_router([{"name": "x", "conclusion": "success"}], "")
    # Act
    run = explain_run("10000001", run_gh=router)
    # Assert
    assert run.failures == []


def test_explain_run_parses_the_failing_job():
    # Arrange
    jobs = [{"name": _PJ, "conclusion": "failure"}]
    router = _gh_router(jobs, PYTEST_LOG)
    # Act
    run = explain_run("10000002", run_gh=router)
    # Assert
    assert (
        run.failures[0]
        .failed_tests[0]
        .startswith("FAILED tests/test_math.py::test_math")
    )
