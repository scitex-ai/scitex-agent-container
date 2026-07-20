"""Tests for the deployed-vs-declared ExecStart detector.

No mocks and no monkeypatch anywhere. Where a systemd is needed the suite
writes a REAL executable ``systemctl`` script into a real ``tmp_path`` and
runs it through the real :func:`subprocess.run`, injected through the same
``which`` / ``runner`` fake-callable seams scitex-dev's own
``resolve_execstart`` documents. A fixture that is a real program on disk
exercises real exec, real pipes and real exit codes; a mock would only
exercise the test's own opinion.

The canned outputs below are VERBATIM captures from the fleet host
(systemd 249), not invented shapes — including the two facts that decide
the parser's design:

* ``show`` exits 0 for a unit that does not exist, printing only
  ``LoadState=not-found``, so the exit code cannot discriminate;
* the ``ExecStart`` record carries runtime noise (``pid``, ``status``,
  timestamps) around the one field worth comparing, ``argv[]``.
"""

from __future__ import annotations

import ast
import subprocess
from pathlib import Path

import pytest

pytest.importorskip(
    "scitex_dev.jobs",
    reason="installed scitex-dev predates the scitex_dev.jobs contract",
)

from scitex_agent_container._execstart_audit import (  # noqa: E402
    ExecFinding,
    ExecStartReport,
    ExecVerdict,
    UnitState,
    audit_execstart,
    audit_job,
    commands_equal,
    parse_show_output,
    query_unit,
    unit_name_for,
)
from scitex_agent_container._jobs_plugin import provide_jobs  # noqa: E402

# --- verbatim captures from the fleet host (systemd 249) -------------------

REAL_LOADED = (
    "ExecStart={ path=/home/ywatanabe/.env-3.11/bin/sac ; "
    "argv[]=/home/ywatanabe/.env-3.11/bin/sac accounts refresh --all "
    "--include-active --sync-active-login ; ignore_errors=no ; "
    "start_time=[Mon 2026-07-20 12:11:25 JST] ; "
    "stop_time=[Mon 2026-07-20 12:11:25 JST] ; pid=1206453 ; "
    "code=exited ; status=1 }\n"
    "LoadState=loaded\n"
)

REAL_NOT_FOUND = "LoadState=not-found\n"

#: The historical hazard: resolve_execstart's rule-3 last resort, which is
#: what the 2026-07-10 unit actually carried before the drop-in masked it.
REAL_BARE_ENV = (
    "ExecStart={ path=/usr/bin/env ; "
    "argv[]=/usr/bin/env sac accounts refresh --all --include-active "
    "--sync-active-login ; ignore_errors=no ; pid=0 ; "
    "code=(null) ; status=0/0 }\n"
    "LoadState=loaded\n"
)


def _job(name: str):
    (match,) = [j for j in provide_jobs() if j.name == name]
    return match


def _fake_systemctl(tmp_path: Path, *, stdout: str, stderr: str = "", rc: int = 0):
    """Write a REAL executable systemctl onto disk and return a ``which``.

    Not a mock: the returned path is a program the OS execs, whose output
    the real subprocess machinery pipes back. ``stdout``/``stderr`` are
    written via a heredoc so the canned systemd text survives verbatim.
    """
    script = tmp_path / "systemctl"
    script.write_text(
        "#!/bin/sh\n"
        "cat <<'SAC_EOF_OUT'\n" + stdout + "SAC_EOF_OUT\n"
        "cat >&2 <<'SAC_EOF_ERR'\n" + stderr + "SAC_EOF_ERR\n"
        f"exit {rc}\n"
    )
    script.chmod(0o755)
    return lambda _name: str(script)


def _fake_systemctl_per_unit(tmp_path: Path, by_unit: dict[str, str]):
    """A REAL systemctl that answers PER UNIT, like the real one does.

    The uniform helper above replies with the same record whatever unit it
    is asked about, which is fine for probing one unit but wrong for a
    fleet-wide audit: it makes every OTHER job look divergent. This writes
    one response file per unit and dispatches on the unit argument,
    defaulting to the measured ``LoadState=not-found`` shape.
    """
    responses = tmp_path / "responses"
    responses.mkdir()
    for unit, text in by_unit.items():
        (responses / unit).write_text(text)
    script = tmp_path / "systemctl"
    script.write_text(
        "#!/bin/sh\n"
        'for a in "$@"; do last="$a"; done\n'
        f'if [ -f "{responses}/$last" ]; then cat "{responses}/$last"; '
        "else echo 'LoadState=not-found'; fi\n"
    )
    script.chmod(0o755)
    return lambda _name: str(script)


def _loaded_record(argv: str) -> str:
    """A ``LoadState=loaded`` response whose argv[] is ``argv``."""
    return (
        f"ExecStart={{ path={argv.split()[0]} ; argv[]={argv} ; "
        "ignore_errors=no ; pid=0 ; code=(null) ; status=0/0 }\n"
        "LoadState=loaded\n"
    )


def _all_units_agreeing() -> dict[str, str]:
    """Every declared systemd job reporting exactly what it declares."""
    from scitex_agent_container._execstart_audit import (
        SYSTEMD_KINDS,
        unit_name_for,
    )

    return {
        unit_name_for(j): _loaded_record(j.command)
        for j in provide_jobs()
        if j.kind in SYSTEMD_KINDS
    }


# ---------------------------------------------------------------------------
# parse_show_output — against the verbatim captures
# ---------------------------------------------------------------------------


def test_parse_extracts_argv_and_discards_runtime_noise() -> None:
    # Arrange — the real record wraps argv[] in path/pid/status/timestamps,
    # every one of which differs between two identical runs. Comparing the
    # whole record would report a divergence on every restart.
    # Act
    state = parse_show_output(REAL_LOADED)
    # Assert
    assert state.execstart == (
        "/home/ywatanabe/.env-3.11/bin/sac accounts refresh --all "
        "--include-active --sync-active-login"
    )


def test_parse_reads_load_state() -> None:
    # Arrange — LoadState is the crisp discriminator; the exit code is not.
    # Act
    state = parse_show_output(REAL_LOADED)
    # Assert
    assert state.load_state == "loaded"


def test_parse_absent_unit_is_not_found_with_no_execstart() -> None:
    # Arrange — a nonexistent unit: systemd omits the ExecStart key entirely
    # and still exits 0. Measured on the fleet host.
    # Act
    state = parse_show_output(REAL_NOT_FOUND)
    # Assert
    assert (state.load_state, state.execstart) == ("not-found", None)


# ---------------------------------------------------------------------------
# query_unit — real exec against a real fixture program
# ---------------------------------------------------------------------------


def test_query_unit_reads_a_real_subprocess(tmp_path: Path) -> None:
    # Arrange — a real executable emitting the real captured shape.
    which = _fake_systemctl(tmp_path, stdout=REAL_LOADED)
    # Act
    state = query_unit(
        "sac.accounts-refresh.service", runner=subprocess.run, which=which
    )
    # Assert
    assert state.load_state == "loaded"


def test_query_unit_without_systemctl_is_unknown_never_a_pass() -> None:
    # Arrange — the container case. This is THE case the brief singles out:
    # a check that cannot distinguish "matches" from "could not ask" is not
    # a check. `which` finding nothing must surface as an error carrier.
    # Act
    state = query_unit("sac.accounts-refresh.service", which=lambda _n: None)
    # Assert — load_state None + a stated reason == the UNKNOWN carrier.
    assert state.load_state is None and state.error


def test_query_unit_carries_stderr_on_failure_never_discards_it(tmp_path: Path) -> None:
    # Arrange — a systemctl that fails the way a session with no user bus
    # does. The stderr text is the ONLY report of a failure we did not
    # anticipate; `2>/dev/null` here is what hid a dead cron job for 49 days.
    which = _fake_systemctl(
        tmp_path,
        stdout="",
        stderr="Failed to connect to bus: No such file or directory",
        rc=1,
    )
    # Act
    state = query_unit("sac.accounts-refresh.service", which=which)
    # Assert
    assert "Failed to connect to bus" in (state.error or "")


def test_query_unit_rc_zero_but_unparseable_is_unknown(tmp_path: Path) -> None:
    # Arrange — rc=0 with a shape carrying no LoadState. Reporting it beats
    # deriving a verdict from output we do not understand.
    which = _fake_systemctl(tmp_path, stdout="something entirely unexpected\n")
    # Act
    state = query_unit("sac.accounts-refresh.service", which=which)
    # Assert
    assert state.load_state is None and "no LoadState" in (state.error or "")


# ---------------------------------------------------------------------------
# audit_job — the verdict table
# ---------------------------------------------------------------------------


def test_matching_execstart_is_a_match() -> None:
    # Arrange — the post-fix steady state: the declared absolute command and
    # the unit's argv[] are the same exec vector.
    job = _job("sac.accounts-refresh")
    state = parse_show_output(REAL_LOADED)
    # Act
    finding = audit_job(job, state=state, intended=job.command)
    # Assert
    assert finding.verdict is ExecVerdict.MATCH


def test_divergent_execstart_is_diverged() -> None:
    # Arrange — THE detector's reason to exist, replayed against the real
    # historical hazard: the unit runs `/usr/bin/env sac ...` while the
    # source declares the absolute path.
    job = _job("sac.accounts-refresh")
    state = parse_show_output(REAL_BARE_ENV)
    # Act
    finding = audit_job(job, state=state, intended=job.command)
    # Assert
    assert finding.verdict is ExecVerdict.DIVERGED


def test_divergence_reports_both_sides_so_it_is_actionable() -> None:
    # Arrange — a divergence naming only one side cannot be acted on: the
    # reader cannot tell which side is wrong.
    job = _job("sac.accounts-refresh")
    state = parse_show_output(REAL_BARE_ENV)
    # Act
    finding = audit_job(job, state=state, intended=job.command)
    # Assert
    assert finding.intended == job.command and "/usr/bin/env" in finding.resolved


def test_absent_unit_is_not_installed_not_a_divergence() -> None:
    # Arrange — several sac jobs are declared behind a DELIBERATE deploy gate
    # (heal-agent-auth / restart-login-expired-agents). Calling those a
    # divergence would train the reader to ignore the whole report.
    job = _job("sac.accounts-refresh")
    state = parse_show_output(REAL_NOT_FOUND)
    # Act
    finding = audit_job(job, state=state, intended=job.command)
    # Assert
    assert finding.verdict is ExecVerdict.NOT_INSTALLED


def test_unreachable_systemd_is_unknown_never_match() -> None:
    # Arrange — the container case reaching the verdict layer. It must NOT
    # fall through into MATCH; ordering of the branches is the safety
    # property being pinned here.
    job = _job("sac.accounts-refresh")
    state = UnitState(load_state=None, execstart=None, error="no systemd here")
    # Act
    finding = audit_job(job, state=state, intended=job.command)
    # Assert
    assert finding.verdict is ExecVerdict.UNKNOWN


def test_missing_generator_intent_is_unknown_never_match() -> None:
    # Arrange — an old scitex-dev cannot tell us what it intends. Absence of
    # an expectation is not evidence that the unit is correct.
    job = _job("sac.accounts-refresh")
    state = parse_show_output(REAL_LOADED)
    # Act
    finding = audit_job(job, state=state, intended=None)
    # Assert
    assert finding.verdict is ExecVerdict.UNKNOWN


def test_bare_head_job_is_unverifiable_not_falsely_diverged() -> None:
    # Arrange — a bare head makes the INTENT interpreter-dependent:
    # resolve_execstart resolves it against sys.executable's sibling bin and
    # then PATH, which need not be the ones that generated the unit. A
    # mismatch would prove nothing, so the check must refuse to compare.
    # sac.fleet-reconcile still declares a bare `sac` today.
    job = _job("sac.fleet-reconcile")
    state = parse_show_output(REAL_LOADED)
    # Act
    finding = audit_job(
        job, state=state, intended="/somewhere/else/sac agents reconcile"
    )
    # Assert
    assert finding.verdict is ExecVerdict.UNVERIFIABLE


def test_unverifiable_finding_names_the_absolute_head_fix() -> None:
    # Arrange — a refusal that does not say how to make the job checkable
    # leaves the reader stuck. The remedy belongs in the finding.
    job = _job("sac.fleet-reconcile")
    state = parse_show_output(REAL_LOADED)
    # Act
    finding = audit_job(
        job, state=state, intended="/somewhere/else/sac agents reconcile"
    )
    # Assert
    assert "absolute" in finding.detail


# ---------------------------------------------------------------------------
# commands_equal / unit_name_for
# ---------------------------------------------------------------------------


def test_quoting_that_does_not_change_the_exec_vector_is_not_a_divergence() -> None:
    # Arrange — only a real difference in what gets EXECUTED counts.
    quoted, bare = '/bin/x --flag "a"', "/bin/x --flag a"
    # Act
    equal = commands_equal(quoted, bare)
    # Assert
    assert equal


def test_a_different_binary_is_a_divergence() -> None:
    # Arrange — the case that matters on a host with seven sac installs at
    # five versions: same args, different binary.
    left, right = "/a/bin/sac x", "/b/bin/sac x"
    # Act
    equal = commands_equal(left, right)
    # Assert
    assert not equal


def test_unit_name_is_derived_verbatim_from_the_job_name() -> None:
    # Arrange — scitex-dev derives the unit name VERBATIM, which is exactly
    # why `sac listen` must never be federated (sac.listen.service would not
    # adopt the hand-written sac-listen.service). The detector must model
    # the same derivation or it would interrogate the wrong unit.
    job = _job("sac.accounts-refresh")
    # Act
    unit = unit_name_for(job)
    # Assert
    assert unit == "sac.accounts-refresh.service"


# ---------------------------------------------------------------------------
# report shape
# ---------------------------------------------------------------------------


def test_unknown_alone_does_not_fail_the_report() -> None:
    # Arrange — "could not ask" is not "found a problem". Making UNKNOWN red
    # would leave the check permanently failing anywhere systemd is absent
    # (every container, all of CI), which is how a check gets muted.
    report = ExecStartReport(
        findings=(
            ExecFinding(
                job="sac.x",
                unit="sac.x.service",
                verdict=ExecVerdict.UNKNOWN,
                detail="no systemd",
            ),
        )
    )
    # Act
    ok = report.ok
    # Assert
    assert ok


def test_unknown_is_still_rendered_loudly_so_it_is_never_a_silent_pass() -> None:
    # Arrange — ok=True must not mean "everything was checked". The reader
    # has to be told how much the run could not see.
    report = ExecStartReport(
        findings=(
            ExecFinding(
                job="sac.x",
                unit="sac.x.service",
                verdict=ExecVerdict.UNKNOWN,
                detail="no systemd",
            ),
        )
    )
    # Act
    rendered = report.render()
    # Assert
    assert "UNKNOWN" in rendered and "NOT a pass" in rendered


def test_a_divergence_fails_the_report() -> None:
    # Arrange — the one condition that must go red.
    report = ExecStartReport(
        findings=(
            ExecFinding(
                job="sac.x",
                unit="sac.x.service",
                verdict=ExecVerdict.DIVERGED,
                detail="d",
                intended="/a x",
                resolved="/b x",
            ),
        )
    )
    # Act
    ok = report.ok
    # Assert
    assert not ok


def test_a_diverged_finding_without_both_sides_refuses_to_construct() -> None:
    # Arrange — an unactionable divergence must crash HERE, not inside a
    # report someone acts on.
    kwargs = dict(
        job="sac.x",
        unit="sac.x.service",
        verdict=ExecVerdict.DIVERGED,
        detail="d",
        intended="/a x",
    )
    # Act
    construct = lambda: ExecFinding(**kwargs)  # noqa: E731
    # Assert
    with pytest.raises(ValueError):
        construct()


def test_a_finding_must_state_its_evidence() -> None:
    # Arrange — a verdict with no stated evidence is the postmortem-in-a-
    # comment this whole module exists to replace.
    kwargs = dict(
        job="sac.x", unit="sac.x.service", verdict=ExecVerdict.MATCH, detail=""
    )
    # Act
    construct = lambda: ExecFinding(**kwargs)  # noqa: E731
    # Assert
    with pytest.raises(ValueError):
        construct()


# ---------------------------------------------------------------------------
# end-to-end — the mutation proof, pinned permanently
# ---------------------------------------------------------------------------


def test_end_to_end_goes_red_against_the_historical_hazard(tmp_path: Path) -> None:
    # Arrange — THE mutation proof, kept in the suite rather than performed
    # once by hand: a real systemctl reporting the pre-fix
    # `/usr/bin/env sac ...` for every unit. accounts-refresh now declares an
    # absolute head, so its intent IS reproducible and the divergence is real.
    which = _fake_systemctl(tmp_path, stdout=REAL_BARE_ENV)
    # Act
    report = audit_execstart(which=which)
    # Assert
    assert not report.ok


def test_end_to_end_names_the_diverging_job(tmp_path: Path) -> None:
    # Arrange — going red is not enough: the report must name WHICH job, or
    # the operator cannot act on it.
    which = _fake_systemctl(tmp_path, stdout=REAL_BARE_ENV)
    # Act
    diverged = audit_execstart(which=which).diverged
    # Assert
    assert any(f.job == "sac.accounts-refresh" for f in diverged)


def test_end_to_end_is_green_when_every_unit_matches(tmp_path: Path) -> None:
    # Arrange — the other half of the mutation proof: same code path, same
    # fixture mechanism, every unit now agreeing with its declaration.
    which = _fake_systemctl_per_unit(tmp_path, _all_units_agreeing())
    # Act
    report = audit_execstart(which=which)
    # Assert
    assert report.ok


def test_end_to_end_match_is_a_positive_verdict_not_merely_absence_of_red(
    tmp_path: Path,
) -> None:
    # Arrange — ok=True could also mean "nothing was checked". Pin that
    # accounts-refresh reached a POSITIVE MATCH, so the green is evidence
    # the comparison actually RAN rather than evidence it was skipped.
    which = _fake_systemctl_per_unit(tmp_path, _all_units_agreeing())
    # Act
    findings = audit_execstart(which=which).findings
    # Assert
    assert any(
        f.job == "sac.accounts-refresh" and f.verdict is ExecVerdict.MATCH
        for f in findings
    )


def test_a_unit_absent_from_systemd_is_not_installed_end_to_end(
    tmp_path: Path,
) -> None:
    # Arrange — an empty response set: every unit answers not-found, the
    # measured shape for a job declared but never installed.
    which = _fake_systemctl_per_unit(tmp_path, {})
    # Act
    report = audit_execstart(which=which)
    # Assert — not-installed is not a divergence, so the report stays ok.
    assert report.ok and report.of(ExecVerdict.NOT_INSTALLED)


def test_end_to_end_without_systemd_is_all_unknown_and_not_a_pass_claim(
    tmp_path: Path,
) -> None:
    # Arrange — the container case, end to end. Every job must land in
    # UNKNOWN; none may be silently reported as MATCH.
    # Act
    report = audit_execstart(which=lambda _n: None)
    # Assert
    assert report.unknown and not report.of(ExecVerdict.MATCH)


# ---------------------------------------------------------------------------
# the detector's own hygiene — file-only, so it cannot flake
# ---------------------------------------------------------------------------


def _code_without_docstrings(path: Path) -> str:
    """Return ``path``'s source with module/class/function docstrings removed.

    Docstrings are stripped because this module's own PROSE discusses the
    forbidden patterns by name; a naive substring scan flags the warning
    text rather than the behaviour. Ordinary string literals are KEPT, so
    a genuine ``subprocess.run("... 2>/dev/null")`` is still caught.
    """
    tree = ast.parse(path.read_text())
    for node in ast.walk(tree):
        if not isinstance(
            node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
        ):
            continue
        body = getattr(node, "body", [])
        if (
            body
            and isinstance(body[0], ast.Expr)
            and isinstance(body[0].value, ast.Constant)
            and isinstance(body[0].value.value, str)
        ):
            body.pop(0)
    return ast.unparse(tree)


def test_the_detector_never_discards_stderr() -> None:
    # Arrange — a file-only assertion (no subprocess, no systemd, so it
    # cannot flake). Discarding stderr discards the only channel that
    # reports the failure you did not anticipate; that exact pattern hid a
    # dead cron job on this host for 49 days, so it must never appear in
    # the detector's own CODE.
    pkg = Path(__file__).resolve().parents[2] / (
        "src/scitex_agent_container/_execstart_audit"
    )
    sources = sorted(pkg.glob("*.py"))
    discards = ("2>/dev/null", "DEVNULL")
    # Act
    offenders = [
        p.name
        for p in sources
        if any(tok in _code_without_docstrings(p) for tok in discards)
    ]
    # Assert — and prove the glob actually found the package.
    assert sources and not offenders


def test_the_detector_has_no_write_path_to_host_state() -> None:
    # Arrange — "report, do not auto-repair" as an enforced property rather
    # than a promise in a docstring. Silently rewriting host state someone
    # else may own is worse than naming the divergence.
    pkg = Path(__file__).resolve().parents[2] / (
        "src/scitex_agent_container/_execstart_audit"
    )
    mutating = ("systemctl --user set-property", "daemon-reload", "write_text")
    # Act
    offenders = [
        p.name
        for p in sorted(pkg.glob("*.py"))
        if any(token in p.read_text() for token in mutating)
    ]
    # Assert
    assert not offenders
