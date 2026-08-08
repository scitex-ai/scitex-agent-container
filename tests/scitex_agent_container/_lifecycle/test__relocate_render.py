#!/usr/bin/env python3
"""A dry run must not let DECLARED and OBSERVED share a column.

Operator, 2026-08-08: 「定義されているのと、今動いているのって違うんで」. The
fleet listing collapses "a spec exists" and "a process is alive" into one word,
which is how a running agent reads as `defined`. This renderer is the place that
mistake would repeat for relocation, so the separation is pinned by test.

The other property under test is that UNKNOWN survives to the last line. The
preflight is three-valued so "I could not tell" reaches the operator; a renderer
that prints it as a failure, or omits it, throws that away at the final step.

Real dataclasses throughout — the preflight types validate themselves, so
constructing them IS the check that the shapes are right. No mocks.
"""

from __future__ import annotations

from scitex_agent_container._lifecycle._relocate_preflight import (
    Check,
    PreflightReport,
    TargetFacts,
    preflight,
)
from scitex_agent_container._lifecycle._relocate_render import (
    VERDICT_GO,
    VERDICT_REFUSED,
    VERDICT_UNKNOWN,
    render_declared,
    render_dry_run,
    render_observed,
    verdict_line,
)

AGENT = "scitex-dev"
HOST = "scitex-compute-03"

ALL_GOOD = TargetFacts(
    reachable=True,
    image_present=True,
    missing_bind_sources=(),
    card_store_url="postgresql://localhost:5442/cards",
    card_store_reachable=True,
    credential_expires_in_s=3600.0,
    credential_refresh_token_present=True,
    supported_runtimes=("apptainer", "tui"),
    rejected_spec_keys=(),
    ports_in_use=(),
    hub_reachable_from_target=True,
)


def _report(facts: TargetFacts, runtime: str = "tui") -> PreflightReport:
    return preflight(agent=AGENT, to_host=HOST, facts=facts, runtime=runtime)


def _text(lines: list[str]) -> str:
    return "\n".join(lines)


def test_declared_section_says_it_is_unverified() -> None:
    # Arrange: the spec's own claims are inputs to the checks, not evidence.
    declared = {"runtime": "tui", "ports": (19013,)}  # stx-allow: STX-NL001
    # Act
    lines = render_declared(declared)
    # Assert
    assert "not verified" in lines[0]


def test_declared_and_observed_are_separate_sections() -> None:
    # Arrange: collapsing them is the `agents list` defect this guards against.
    # Act
    text = _text(render_dry_run(_report(ALL_GOOD), declared={"runtime": "tui"}))
    # Assert
    assert text.index("DECLARED") < text.index("OBSERVED")


def test_a_clean_target_reports_go() -> None:
    # Arrange: positive control. Without it, the refusal tests below could pass
    # because the fixture is malformed rather than because refusal works.
    # Act
    line = verdict_line(_report(ALL_GOOD))
    # Assert
    assert VERDICT_GO in line


def test_a_failed_check_refuses() -> None:
    # Arrange: /mnt/c is a Windows drive absent on the nas — the 2026-08-07 case.
    facts = TargetFacts(**{**ALL_GOOD.__dict__, "missing_bind_sources": ("/mnt/c",)})
    # Act
    line = verdict_line(_report(facts))
    # Assert
    assert line.startswith(f"VERDICT  {VERDICT_REFUSED}")


def test_an_unknown_refuses() -> None:
    # Arrange: nothing failed; one fact was never observed.
    facts = TargetFacts(**{**ALL_GOOD.__dict__, "credential_expires_in_s": None})
    # Act
    line = verdict_line(_report(facts))
    # Assert
    assert "could not be determined" in line


def test_an_unknown_is_labelled_undetermined_not_plainly_refused() -> None:
    # Arrange: same state, opposite half of the contract. A FAIL is something to
    # fix; an UNKNOWN is something to go and measure, so the two verdicts must
    # not share a label. (Asserting the word "failed" is absent would be the
    # wrong test — the honest sentence "nothing failed" contains it.)
    facts = TargetFacts(**{**ALL_GOOD.__dict__, "credential_expires_in_s": None})
    # Act
    line = verdict_line(_report(facts))
    # Assert
    assert VERDICT_UNKNOWN in line


def test_unknown_is_labelled_unknown_not_fail() -> None:
    # Arrange: the label is what a skimming reader acts on.
    facts = TargetFacts(**{**ALL_GOOD.__dict__, "image_present": None})
    # Act
    lines = render_observed(_report(facts))
    row = next(ln for ln in lines if "image_present" in ln)
    # Assert
    assert "UNKNOWN" in row and "FAIL" not in row


def test_the_probe_error_is_printed_beside_the_unknown() -> None:
    # Arrange: "missing" without "why" turns a 5-second fix into an investigation.
    facts = TargetFacts(**{**ALL_GOOD.__dict__, "credential_expires_in_s": None})
    errors = {"credentials_valid": "SSHTimeout: no route to host"}
    # Act
    text = _text(render_observed(_report(facts), errors))
    # Assert
    assert "SSHTimeout: no route to host" in text


def test_passing_checks_are_printed_too() -> None:
    # Arrange: showing only problems makes "passed" and "never ran"
    # indistinguishable — the ambiguity the three outcomes exist to remove.
    # Act
    text = _text(render_observed(_report(ALL_GOOD)))
    # Assert
    assert text.count("PASS") == len(_report(ALL_GOOD).checks)


THREE_BROKEN = TargetFacts(
    **{
        **ALL_GOOD.__dict__,
        "missing_bind_sources": ("/mnt/c",),
        "card_store_reachable": False,
        "hub_reachable_from_target": False,
    }
)


def test_every_problem_is_listed_not_just_the_first() -> None:
    # Arrange: the operator asked for a dry run that does not need N runs to
    # find N problems. Counted in the OBSERVED section alone — the BLOCKING
    # summary repeats each one by design, so counting the whole document would
    # measure the repetition rather than the coverage.
    # Act
    text = _text(render_observed(_report(THREE_BROKEN)))
    # Assert
    assert text.count("FAIL") == 3


def test_multiple_failures_produce_a_blocking_section() -> None:
    # Arrange: the summary at the end is what gets pasted into chat.
    # Act
    text = _text(render_dry_run(_report(THREE_BROKEN)))
    # Assert
    assert "BLOCKING" in text


def test_a_clean_run_has_no_blocking_section() -> None:
    # Arrange: a GO must not print an empty scary heading.
    # Act
    text = _text(render_dry_run(_report(ALL_GOOD)))
    # Assert
    assert "BLOCKING" not in text


def test_the_header_names_both_agent_and_target() -> None:
    # Arrange: a dry run pasted into chat must be unambiguous on its own.
    # Act
    head = render_dry_run(_report(ALL_GOOD))[0]
    # Assert
    assert AGENT in head and HOST in head


def test_the_header_says_nothing_was_touched() -> None:
    # Arrange: the whole point of the verb is that it is safe to run.
    # Act
    head = render_dry_run(_report(ALL_GOOD))[0]
    # Assert
    assert "nothing was touched" in head


def test_a_hint_follows_every_non_passing_check() -> None:
    # Arrange: the preflight validator already refuses a hintless failure; this
    # asserts the renderer actually SHOWS it, which is a separate thing.
    facts = TargetFacts(**{**ALL_GOOD.__dict__, "card_store_reachable": False})
    # Act
    lines = render_observed(_report(facts))
    idx = next(i for i, ln in enumerate(lines) if "card_store_reachable" in ln)
    # Assert
    assert lines[idx + 1].lstrip().startswith("->")


def test_empty_declared_says_so_rather_than_printing_a_bare_heading() -> None:
    # Arrange: a heading with nothing under it reads as a rendering bug.
    # Act
    lines = render_declared({})
    # Assert
    assert any("nothing declared" in ln for ln in lines)


def test_render_observed_tolerates_a_report_with_one_check() -> None:
    # Arrange: the column width is computed from the checks; a single short
    # name must not break the formatting path.
    report = PreflightReport(
        agent=AGENT,
        to_host=HOST,
        checks=(Check(name="x", ok=True, detail="fine"),),
    )
    # Act
    lines = render_observed(report)
    # Assert
    assert any("PASS" in ln and "fine" in ln for ln in lines)
