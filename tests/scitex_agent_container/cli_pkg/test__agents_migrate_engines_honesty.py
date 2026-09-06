"""What the READABLE output says, and what a filtered run may claim.

Real ``CliRunner`` against the real command, real spec files under
``tmp_path``. Every fact here was measured on the live corpus and every one of
them is the same class of defect: the ``--json`` payload knew something the
human output did not, or the human output knew something the one boolean built
for a machine reader did not.

FOUR MEASURED SHAPES:

1. **A filter narrowed the census with no record of it.**
   ``migrate-engines --root <113 specs> -a business --apply`` printed, verbatim:
   "Nothing to write — all 1 spec(s) under <root> already declare spec.engines.
   The sweep is idempotent; this is what a completed one looks like." — the
   exact sentence the report module's own docstring identifies as the historic
   bug, over a root holding 100 unmigrated specs, while NAMING that root. The
   ``--json`` form reported ``specs: 1``, ``migration_complete: true`` and
   carried no key naming the ``-a`` filter.
2. **``--host-supports-engines`` lifted a MEASURED negative in silence.**
   The run went from "100 would be migrated; 12 REFUSED" to "109 would be
   migrated; 3 REFUSED" and ``grep -in 'spartan|override|lift'`` over the
   whole terminal output matched ZERO lines.
3. **One refusal paragraph, printed once per SPEC.** Twelve refusals rendered
   as twelve blocks, nine carrying the byte-identical ~400-character spartan
   paragraph. A whole-host refusal on ``scitex-laptop-01`` (60 specs) would
   have printed it sixty times.
4. **``--preflight`` was dropped on the ``--apply`` human path.** The probe
   still ran — a real network round trip — and no verdict was printed at all.

STX-NM002: no mocks. STX-TQ002 / TQ007: AAA markers, one fact per test.
"""

from __future__ import annotations

import json
from pathlib import Path

import yaml

from tests.scitex_agent_container.cli_pkg.test__agents_migrate_engines import (
    CAPABLE_HOST,
    _run,
    _write_spec,
    fleet,
)

__all__ = ["fleet"]

PREDATES = "spartan"
UNMEASURED = "scitex-compute-02"

#: A distinctive, unbroken slice of the spartan roster row. The renderer
#: prints refusal details with ``soft_wrap=True``, so it is never re-wrapped.
_SPARTAN_EVIDENCE = "3 roots hold scitex_agent_container and NONE has"


def _flat(result) -> str:
    """Terminal output with the console's line breaks removed."""
    return result.stdout.replace("\n", "")


# ---------------------------------------------------------------------------
# 1. A filtered run is a census of a SUBSET
# ---------------------------------------------------------------------------


def test_a_filtered_run_records_the_selector_in_the_payload(fleet: Path) -> None:
    # Arrange — no key anywhere named the -a filter, so a scheduled runner
    # could not tell a full census from a one-agent one.
    for name in ("alpha", "beta", "gamma"):
        _write_spec(fleet, name)
    # Act
    payload = json.loads(_run("--no-diff", "--json", "-a", "alpha").stdout)
    # Assert
    assert payload["selectors"] == ["--agent alpha"]


def test_a_filtered_run_is_marked_as_filtered(fleet: Path) -> None:
    # Arrange
    for name in ("alpha", "beta"):
        _write_spec(fleet, name)
    # Act
    payload = json.loads(_run("--no-diff", "--json", "--host", CAPABLE_HOST).stdout)
    # Assert
    assert payload["filtered"] is True


def test_a_filtered_apply_does_not_claim_the_migration_is_complete(
    fleet: Path,
) -> None:
    # Arrange — one agent named, two others left untouched under the same root.
    for name in ("alpha", "beta", "gamma"):
        _write_spec(fleet, name)
    # Act
    payload = json.loads(_run("--apply", "--no-diff", "--json", "-a", "alpha").stdout)
    # Assert
    assert payload["migration_complete"] is False


def test_a_filtered_apply_does_not_print_the_completion_sentence(
    fleet: Path,
) -> None:
    # Arrange — the measured shape: the one selected spec is ALREADY migrated,
    # so the apply writes nothing and reaches the "nothing to write" branch.
    _write_spec(fleet, "alpha")
    _write_spec(fleet, "beta")
    _run("--apply", "--no-diff", "--json", "-a", "alpha")
    # Act
    result = _run("--apply", "--no-diff", "-a", "alpha")
    # Assert
    assert "completed one looks like" not in result.stdout


def test_a_filtered_apply_names_the_filter_in_the_readable_output(
    fleet: Path,
) -> None:
    # Arrange
    _write_spec(fleet, "alpha")
    _write_spec(fleet, "beta")
    _run("--apply", "--no-diff", "--json", "-a", "alpha")
    # Act
    result = _run("--apply", "--no-diff", "-a", "alpha")
    # Assert
    assert "--agent alpha" in _flat(result)


def test_an_unfiltered_finished_sweep_is_still_reported_complete(
    fleet: Path,
) -> None:
    # Arrange — the positive control: the claim must still be makeable.
    _write_spec(fleet, "alpha")
    _run("--apply", "--no-diff", "--json")
    # Act
    payload = json.loads(_run("--apply", "--no-diff", "--json").stdout)
    # Assert
    assert payload["migration_complete"] is True


def test_a_selector_that_matched_nothing_is_named_in_the_payload(
    fleet: Path,
) -> None:
    # Arrange — measured: `-a business -a scitex-orochi -a NOSUCH-AGENT-TYPO`
    # reported specs:2, exit 0, and no field anywhere naming the typo.
    _write_spec(fleet, "alpha")
    # Act
    payload = json.loads(
        _run("--no-diff", "--json", "-a", "alpha", "-a", "NOSUCH-AGENT-TYPO").stdout
    )
    # Assert
    assert payload["unmatched_agents"] == ["NOSUCH-AGENT-TYPO"]


def test_a_selector_that_matched_nothing_is_named_in_the_readable_output(
    fleet: Path,
) -> None:
    # Arrange
    _write_spec(fleet, "alpha")
    # Act
    result = _run("--no-diff", "-a", "alpha", "-a", "NOSUCH-AGENT-TYPO")
    # Assert
    assert "NOSUCH-AGENT-TYPO" in _flat(result)


# ---------------------------------------------------------------------------
# 2. An override of a measured NO restates what it contradicts
# ---------------------------------------------------------------------------


def test_lifting_the_floor_names_the_host_in_the_readable_output(
    fleet: Path,
) -> None:
    # Arrange — nine specs moved from REFUSED into the migratable count and
    # the word "spartan" appeared nowhere in the output.
    _write_spec(fleet, "grounded", host=PREDATES)
    # Act
    result = _run("--no-diff", "--host-supports-engines", PREDATES)
    # Assert
    assert PREDATES in _flat(result)


def test_lifting_a_measured_negative_says_it_contradicts_a_measurement(
    fleet: Path,
) -> None:
    # Arrange
    _write_spec(fleet, "grounded", host=PREDATES)
    # Act
    result = _run("--no-diff", "--host-supports-engines", PREDATES)
    # Assert
    assert "CONTRADICTS" in _flat(result)


def test_lifting_a_measured_negative_restates_the_measurement(
    fleet: Path,
) -> None:
    # Arrange — an override that does not show the evidence it is overriding
    # is indistinguishable from one that had nothing to override.
    _write_spec(fleet, "grounded", host=PREDATES)
    # Act
    result = _run("--no-diff", "--host-supports-engines", PREDATES)
    # Assert
    assert _SPARTAN_EVIDENCE in _flat(result)


def test_lifting_an_unmeasured_host_does_not_claim_a_contradiction(
    fleet: Path,
) -> None:
    # Arrange — nobody measured compute-02, so there is nothing to contradict.
    # Reporting it the loud way would teach the reader to ignore the loud way.
    _write_spec(fleet, "unknown-ground", host=UNMEASURED)
    # Act
    result = _run("--no-diff", "--host-supports-engines", UNMEASURED)
    # Assert
    assert "CONTRADICTS" not in _flat(result)


def test_lifting_an_unmeasured_host_still_says_the_floor_was_lifted(
    fleet: Path,
) -> None:
    # Arrange — the control for the assertion above: quieter, not silent.
    _write_spec(fleet, "unknown-ground", host=UNMEASURED)
    # Act
    result = _run("--no-diff", "--host-supports-engines", UNMEASURED)
    # Assert
    assert "FLOOR LIFTED" in _flat(result)


def test_the_approving_side_names_the_host_it_judged_capable(fleet: Path) -> None:
    # Arrange — 100 writes and not a word about which hosts were judged
    # capable, on what, or how old the measurement was.
    _write_spec(fleet, "alpha", host=CAPABLE_HOST)
    # Act
    result = _run("--no-diff")
    # Assert
    assert "version floor" in _flat(result)


def test_the_approving_side_prints_the_roster_date(fleet: Path) -> None:
    # Arrange — HOST_SUPPORT has no expiry and nothing probes, so a stale row
    # makes a wrong run look exactly like a right one.
    _write_spec(fleet, "alpha", host=CAPABLE_HOST)
    # Act
    result = _run("--no-diff")
    # Assert
    assert "roster measured 2026-09-06" in _flat(result)


def test_the_payload_carries_the_floor_rows_the_writes_rest_on(
    fleet: Path,
) -> None:
    # Arrange
    _write_spec(fleet, "alpha", host=CAPABLE_HOST)
    # Act
    payload = json.loads(_run("--no-diff", "--json").stdout)
    # Assert
    assert [r["host"] for r in payload["engine_floor"]["hosts"]] == [CAPABLE_HOST]


# ---------------------------------------------------------------------------
# 3. One block per KIND, not one per spec
# ---------------------------------------------------------------------------


def test_a_whole_host_refusal_prints_its_evidence_once(fleet: Path) -> None:
    # Arrange — three specs, one host, one measurement. The paragraph is
    # per-HOST; printing it per-SPEC buried the review under copies.
    for name in ("g1", "g2", "g3"):
        _write_spec(fleet, name, host=PREDATES)
    # Act
    result = _run("--no-diff")
    # Assert
    assert _flat(result).count(_SPARTAN_EVIDENCE) == 1


def test_the_grouped_refusal_still_names_every_agent(fleet: Path) -> None:
    # Arrange — grouping must not cost the names; a refusal that does not say
    # WHICH spec is a refusal nobody can act on.
    for name in ("g1", "g2", "g3"):
        _write_spec(fleet, name, host=PREDATES)
    # Act
    flat = _flat(_run("--no-diff"))
    # Assert
    assert all(name in flat for name in ("g1", "g2", "g3"))


def test_two_different_refusal_kinds_stay_two_blocks(fleet: Path) -> None:
    # Arrange — the control: grouping by (reason, detail) must not collapse
    # a pre-engines host and an unmeasured one into one claim.
    _write_spec(fleet, "g1", host=PREDATES)
    _write_spec(fleet, "u1", host=UNMEASURED)
    # Act
    flat = _flat(_run("--no-diff"))
    # Assert
    assert (_SPARTAN_EVIDENCE in flat) and ("absent from the measured roster" in flat)


# ---------------------------------------------------------------------------
# 4. --preflight is rendered wherever it was paid for
# ---------------------------------------------------------------------------


def test_preflight_prints_its_verdict_on_the_apply_path(fleet: Path) -> None:
    # Arrange — `--preflight --apply` is the natural "check the gateway, then
    # write" invocation. The probe ran and printed no line at all.
    _write_spec(fleet, "alpha")
    # Act
    result = _run("--apply", "--no-diff", "--preflight")
    # Assert
    assert "gateway preflight" in _flat(result)


def test_preflight_prints_its_verdict_on_the_dry_run_path(fleet: Path) -> None:
    # Arrange — the control: the path that always worked still works.
    _write_spec(fleet, "alpha")
    # Act
    result = _run("--no-diff", "--preflight")
    # Assert
    assert "gateway preflight" in _flat(result)


def test_preflight_prints_its_verdict_when_the_apply_is_refused(
    fleet: Path,
) -> None:
    # Arrange — an unreadable spec makes the plan unsafe, and that branch
    # printed no preflight either.
    _write_spec(fleet, "alpha")
    broken = _write_spec(fleet, "broken")
    broken.chmod(0o000)
    # Act
    try:
        result = _run("--apply", "--no-diff", "--preflight")
    finally:
        broken.chmod(0o644)
    # Assert
    assert "gateway preflight" in _flat(result)


def test_without_the_flag_the_apply_path_prints_no_preflight(fleet: Path) -> None:
    # Arrange — the control: a sweep must not dial the network unasked.
    _write_spec(fleet, "alpha")
    # Act
    result = _run("--apply", "--no-diff")
    # Assert
    assert "gateway preflight" not in _flat(result)


# ---------------------------------------------------------------------------
# The templates a completed sweep leaves behind
# ---------------------------------------------------------------------------


def test_an_apply_that_skips_a_template_is_not_complete(fleet: Path) -> None:
    # Arrange — `sac agents create` copies the template, so every agent minted
    # after this "finished" sweep re-introduces the legacy shape.
    _write_spec(fleet, "real-agent")
    _write_spec(fleet, "_template_generalist")
    # Act
    payload = json.loads(_run("--apply", "--no-diff", "--json").stdout)
    # Assert
    assert payload["migration_complete"] is False


def test_the_skipped_template_really_kept_the_legacy_shape(fleet: Path) -> None:
    # Arrange — the positive control for the claim above.
    _write_spec(fleet, "real-agent")
    template = _write_spec(fleet, "_template_generalist")
    # Act
    _run("--apply", "--no-diff", "--json")
    # Assert
    assert "engines" not in yaml.safe_load(template.read_text())["spec"]


def test_migrating_the_templates_too_reports_the_sweep_complete(
    fleet: Path,
) -> None:
    # Arrange — the positive control: --templates leaves nothing behind.
    _write_spec(fleet, "real-agent")
    _write_spec(fleet, "_template_generalist")
    # Act
    payload = json.loads(_run("--apply", "--no-diff", "--json", "--templates").stdout)
    # Assert
    assert payload["migration_complete"] is True


def test_the_apply_reports_the_paths_it_wrote(fleet: Path) -> None:
    # Arrange — `written` carries agent NAMES, and a name cannot say which
    # root the write landed in. The default sweep searches several.
    spec = _write_spec(fleet, "alpha")
    # Act
    payload = json.loads(_run("--apply", "--no-diff", "--json").stdout)
    # Assert
    assert payload["written_paths"] == [str(spec)]
