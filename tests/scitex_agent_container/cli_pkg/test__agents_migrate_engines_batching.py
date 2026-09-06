"""``migrate-engines`` batching, filtering, and what the report CLAIMS.

Split from ``test__agents_migrate_engines`` on the module line budget. Same
real ``CliRunner``, same real tmp fleet, every root pinned inside
``tmp_path``. What lives here is the half of the surface a review measured
as wrong rather than missing:

* ``--limit`` capped the specs EXAMINED, so batch two re-selected the same
  first N and wrote nothing while printing a completion message;
* ``--host`` silently DROPPED the specs it could not read — the batching
  flag disabling the guard that blocks an unsafe apply;
* the apply asserted the migration was COMPLETE whenever it wrote nothing,
  counting refused and held-back specs as done;
* the root was undocumented, unsettable and never printed, and the default
  is the untracked live copy;
* ``--diff`` was accepted and dropped on the apply path.

STX-NM002: no mocks. STX-TQ002 / TQ007: AAA markers, one fact per test.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import yaml
from click.testing import CliRunner

from scitex_agent_container.cli_pkg._agents_migrate_engines import migrate_engines
from tests.scitex_agent_container.cli_pkg.test__agents_migrate_engines import (
    _run,
    _write_settings,
    _write_spec,
    fleet,
)

__all__ = ["fleet"]


# ---------------------------------------------------------------------------
# --limit is a BATCH, which means it has to advance
# ---------------------------------------------------------------------------


def _engine_count(fleet: Path) -> int:
    return sum(
        1
        for spec in sorted(fleet.glob("*/spec.yaml"))
        if "engines" in (yaml.safe_load(spec.read_text())["spec"])
    )


def test_a_second_limited_batch_writes_the_next_specs(fleet: Path) -> None:
    # Arrange — the operator's stated condition for trusting a 119-file
    # rewrite is "apply in small batches". Slicing the sorted glob re-selected
    # the same first N forever, so batch two wrote nothing.
    for name in ("b1", "b2", "b3", "b4"):
        _write_spec(fleet, name)
    _run("--apply", "--no-diff", "--json", "--limit", "2")
    # Act
    payload = json.loads(_run("--apply", "--no-diff", "--json", "--limit", "2").stdout)
    # Assert
    assert payload["written"] == ["b3", "b4"]


def test_repeated_limited_batches_finish_the_fleet(fleet: Path) -> None:
    # Arrange
    for name in ("b1", "b2", "b3", "b4"):
        _write_spec(fleet, name)
    # Act
    for _ in range(2):
        _run("--apply", "--no-diff", "--json", "--limit", "2")
    # Assert
    assert _engine_count(fleet) == 4


def test_the_specs_past_the_limit_are_named_as_held_back(fleet: Path) -> None:
    # Arrange — deferred, not dropped: a batch that says nothing about what
    # it left reads exactly like a finished sweep.
    for name in ("b1", "b2", "b3"):
        _write_spec(fleet, name)
    # Act
    payload = json.loads(_run("--no-diff", "--json", "--limit", "1").stdout)
    # Assert
    assert payload["held_back"] == ["b2", "b3"]


def test_a_limited_run_does_not_claim_the_migration_is_complete(fleet: Path) -> None:
    # Arrange
    for name in ("b1", "b2"):
        _write_spec(fleet, name)
    # Act
    payload = json.loads(_run("--apply", "--no-diff", "--json", "--limit", "1").stdout)
    # Assert
    assert payload["migration_complete"] is False


def test_a_negative_limit_is_a_usage_error(fleet: Path) -> None:
    # Arrange — `picked[:-1]` accepted this and silently dropped the LAST
    # spec, so a typo turned "one spec" into "all but one".
    for name in ("b1", "b2", "b3"):
        _write_spec(fleet, name)
    # Act
    result = CliRunner().invoke(migrate_engines, ["--no-diff", "--json", "--limit", "-1"])
    # Assert
    assert result.exit_code == 2


def test_a_negative_limit_writes_nothing(fleet: Path) -> None:
    # Arrange
    spec = _write_spec(fleet, "b1")
    before = spec.read_text()
    # Act
    CliRunner().invoke(migrate_engines, ["--apply", "--no-diff", "--limit", "-1"])
    # Assert
    assert spec.read_text() == before


# ---------------------------------------------------------------------------
# No filter may make a spec vanish
# ---------------------------------------------------------------------------


def test_the_host_filter_keeps_a_spec_it_could_not_read(fleet: Path) -> None:
    # Arrange — --host reads each spec to decide. A spec it cannot read must
    # reach the PLAN as unreadable; excluding it makes the batching flag the
    # thing that disables the guard against an unsafe apply.
    _write_spec(fleet, "good", host="scitex-compute-04")
    broken = _write_spec(fleet, "broken", host="scitex-compute-04")
    broken.chmod(0o000)
    try:
        # Act
        payload = json.loads(
            _run("--no-diff", "--json", "--host", "scitex-compute-04").stdout
        )
    finally:
        broken.chmod(0o644)
    # Assert
    assert [o["agent"] for o in payload["unreadable"]] == ["broken"]


def test_the_host_filter_keeps_a_spec_that_does_not_parse(fleet: Path) -> None:
    # Arrange — it declares its host in the readable prefix and still does
    # not parse, so the filter cannot rule it out either way.
    _write_spec(fleet, "good", host="scitex-compute-04")
    broken = _write_spec(fleet, "broken", host="scitex-compute-04")
    broken.write_text(broken.read_text() + "  bad: [unclosed\n")
    # Act
    payload = json.loads(
        _run("--no-diff", "--json", "--host", "scitex-compute-04").stdout
    )
    # Assert
    assert [o["agent"] for o in payload["refused"]] == ["broken"]


def test_an_unreadable_spec_under_a_host_filter_still_blocks_the_apply(
    fleet: Path,
) -> None:
    # Arrange
    _write_spec(fleet, "good", host="scitex-compute-04")
    broken = _write_spec(fleet, "broken", host="scitex-compute-04")
    broken.chmod(0o000)
    try:
        # Act
        result = _run("--apply", "--no-diff", "--json", "--host", "scitex-compute-04")
    finally:
        broken.chmod(0o644)
    # Assert
    assert result.exit_code == 1


def test_an_unreadable_spec_blocks_the_apply_from_writing(fleet: Path) -> None:
    # Arrange — `safe_to_apply` exists for this; nothing asserted it.
    good = _write_spec(fleet, "good")
    broken = _write_spec(fleet, "broken")
    broken.chmod(0o000)
    try:
        # Act
        _run("--apply", "--no-diff", "--json")
    finally:
        broken.chmod(0o644)
    # Assert
    assert "engines" not in yaml.safe_load(good.read_text())["spec"]


def test_an_absent_roster_refuses_the_apply(tmp_path: Path) -> None:
    # Arrange — "0 specs" is equally true of a finished sweep and of a total
    # discovery failure. Only one of them licenses an apply.
    keys = {
        "SCITEX_AGENT_CONTAINER_AGENTS_DIR": str(tmp_path / "nowhere"),
        "SAC_SPEC_CACHE_DISABLE": "1",
    }
    saved = {k: os.environ.get(k) for k in keys}
    os.environ.update(keys)
    # Act
    try:
        result = _run("--apply", "--no-diff", "--json")
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
    # Assert
    assert result.exit_code == 1


# ---------------------------------------------------------------------------
# Where it writes, and saying so
# ---------------------------------------------------------------------------


def test_an_explicit_root_is_swept_instead_of_the_default(tmp_path: Path) -> None:
    # Arrange — the default is the untracked LIVE copy, and inside a
    # container it is not even the host's. Without --root the git-tracked
    # tree was reachable only through an undocumented env var.
    elsewhere = tmp_path / "tracked" / "agents"
    elsewhere.mkdir(parents=True)
    _write_settings(elsewhere / "_shared" / "to_home")
    _write_spec(elsewhere, "alpha")
    saved = os.environ.get("SAC_SPEC_CACHE_DISABLE")
    os.environ["SAC_SPEC_CACHE_DISABLE"] = "1"
    # Act
    try:
        payload = json.loads(
            _run("--no-diff", "--json", "--root", str(elsewhere)).stdout
        )
    finally:
        if saved is None:
            os.environ.pop("SAC_SPEC_CACHE_DISABLE", None)
        else:
            os.environ["SAC_SPEC_CACHE_DISABLE"] = saved
    # Assert
    assert payload["root"] == str(elsewhere)


def test_the_apply_names_the_root_it_wrote_into(fleet: Path) -> None:
    # Arrange — the success line never said where it had written.
    _write_spec(fleet, "alpha")
    # Act
    result = _run("--apply", "--no-diff")
    # Assert — console width wraps the path, so compare without the wrap.
    assert str(fleet) in result.stdout.replace("\n", "")


# ---------------------------------------------------------------------------
# The apply says what it actually did
# ---------------------------------------------------------------------------


def test_a_run_that_refused_everything_is_not_called_complete(fleet: Path) -> None:
    # Arrange — two specs neither of which can be migrated. "Nothing to
    # write" was printed as "this is what a completed one looks like".
    _write_spec(fleet, "alpha", model="")
    _write_spec(fleet, "beta", model="")
    # Act
    payload = json.loads(_run("--apply", "--no-diff", "--json").stdout)
    # Assert
    assert payload["migration_complete"] is False


def test_a_run_that_refused_everything_does_not_print_the_completion_line(
    fleet: Path,
) -> None:
    # Arrange
    _write_spec(fleet, "alpha", model="")
    # Act
    result = _run("--apply", "--no-diff")
    # Assert
    assert "completed one looks like" not in result.stdout


def test_the_apply_names_its_refusals_without_json(fleet: Path) -> None:
    # Arrange — only --json carried them, so the human path reported a clean
    # run over a fleet it could not migrate.
    _write_spec(fleet, "alpha", model="")
    # Act
    result = _run("--apply", "--no-diff")
    # Assert
    assert "alpha" in result.stdout


def test_a_finished_sweep_is_reported_as_complete(fleet: Path) -> None:
    # Arrange — the positive half: the claim must still be makeable.
    _write_spec(fleet, "alpha")
    _run("--apply", "--no-diff", "--json")
    # Act
    payload = json.loads(_run("--apply", "--no-diff", "--json").stdout)
    # Assert
    assert payload["migration_complete"] is True


def test_apply_prints_the_diff_it_is_writing(fleet: Path) -> None:
    # Arrange — --diff is ON by default and its help calls it "the whole
    # point"; the apply path accepted it and printed nothing.
    _write_spec(fleet, "alpha")
    # Act
    result = _run("--apply")
    # Assert
    assert "+  engines:" in result.stdout


# ---------------------------------------------------------------------------
# What the gate measures
# ---------------------------------------------------------------------------


def test_the_applied_spec_still_reports_the_same_top_level_model(fleet: Path) -> None:
    # Arrange — `spec.claude.model` is emptied by the migration, and the
    # loader derived AgentConfig.model from that raw field. 117 of 119 specs
    # flipped to the 'sonnet' default while the gate reported zero drift.
    spec = _write_spec(fleet, "alpha")
    # Act
    _run("--apply", "--no-diff", "--json")
    # Assert
    from scitex_agent_container.config import load_config

    assert load_config(spec).model == "opus[1m]"


def test_the_applied_spec_still_injects_the_same_model_env(fleet: Path) -> None:
    # Arrange — SCITEX_AGENT_CONTAINER_MODEL is injected into every container
    # and is what `sac whoami` prints.
    spec = _write_spec(fleet, "alpha")
    # Act
    _run("--apply", "--no-diff", "--json")
    # Assert
    from scitex_agent_container.config import load_config

    assert load_config(spec).env["SCITEX_AGENT_CONTAINER_MODEL"] == "Claude Opus (1M)"


def test_a_crlf_spec_keeps_its_line_endings(fleet: Path) -> None:
    # Arrange — Path.read_text does universal-newline translation, so a CRLF
    # spec was rewritten end to end: every line changed, which is exactly the
    # unreviewable whole-file diff this sweep exists to avoid.
    spec = _write_spec(fleet, "alpha")
    spec.write_bytes(spec.read_text().replace("\n", "\r\n").encode())
    # Act
    _run("--apply", "--no-diff", "--json")
    # Assert
    assert b"\r\n" in spec.read_bytes()


def test_a_crlf_spec_gains_no_bare_line_feed(fleet: Path) -> None:
    # Arrange
    spec = _write_spec(fleet, "alpha")
    spec.write_bytes(spec.read_text().replace("\n", "\r\n").encode())
    # Act
    _run("--apply", "--no-diff", "--json")
    # Assert
    body = spec.read_bytes()
    assert body.count(b"\n") == body.count(b"\r\n")
