"""``sac agents migrate-layers`` — the CLI surface of the to_home_layers sweep.

Real ``CliRunner`` against the real click command, over a real tmp fleet of
real spec files. The whole cascade is pinned inside ``tmp_path`` through the
documented env seams, so no test can read or rewrite the operator's live specs.

What only the CLI owns, and what these pin:

* **dry-run is the DEFAULT** — the single most important safety property of a
  verb that edits 100+ hand-maintained files;
* the exit-code contract, in particular that a named REFUSAL is NOT a failure
  while a malformed or unreadable spec IS; and
* that the sweep is behaviour-preserving in the only way that counts —
  measured, by re-deriving what every agent arms after the write.

STX-NM002: no mocks. STX-TQ002 / TQ007: AAA markers, one fact per test.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
import yaml
from click.testing import CliRunner

from scitex_agent_container._maintenance._layers_migration_gate import (
    fleet_arming_snapshot,
)
from scitex_agent_container.cli_pkg._agents_migrate_layers import migrate_layers
from tests.scitex_agent_container._helpers.explicit_spec import explicit_doc

_SETTINGS = {"hooks": {"PreToolUse": [{"hooks": [{"command": "guard.sh"}]}]}}


def _write_settings(to_home: Path) -> None:
    claude = to_home / ".claude"
    claude.mkdir(parents=True, exist_ok=True)
    (claude / "settings.json").write_text(json.dumps(_SETTINGS))


def _write_spec(
    agents_dir: Path, name: str, *, flow: bool = False, **overrides
) -> Path:
    agent_dir = agents_dir / name
    agent_dir.mkdir(parents=True)
    _write_settings(agent_dir / "to_home")
    doc = explicit_doc({"to_home": "./to_home", **overrides})
    spec = agent_dir / "spec.yaml"
    spec.write_text(yaml.safe_dump(doc, sort_keys=False, default_flow_style=flow))
    return spec


@pytest.fixture
def fleet(tmp_path: Path):
    """A tmp fleet with every cascade root pinned inside tmp_path."""
    agents_dir = tmp_path / "agents"
    agents_dir.mkdir()
    _write_settings(agents_dir / "_shared" / "to_home")
    user_shared = tmp_path / "user-baseline" / "to_home"
    _write_settings(user_shared)

    keys = {
        "SCITEX_AGENT_CONTAINER_AGENTS_DIR": str(agents_dir),
        "SCITEX_AGENT_CONTAINER_RUNTIME_DIR": str(tmp_path / "runtime"),
        "SAC_USER_TO_HOME_BASELINE": str(user_shared),
        "SAC_SPEC_CACHE_DISABLE": "1",
    }
    saved = {k: os.environ.get(k) for k in keys}
    os.environ.update(keys)
    try:
        yield agents_dir
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def _run(*args) -> "object":
    """Assert on ``result.stdout``, never ``result.output``.

    ``Result.output`` is stdout and stderr COMBINED; the scheduled form of this
    command is ``--json`` piped to a parser, where they were never merged. A
    single log line on stderr would otherwise make ``json.loads`` fail on a
    command that behaved perfectly.
    """
    return CliRunner().invoke(migrate_layers, list(args), catch_exceptions=False)


# ---------------------------------------------------------------------------
# Dry-run is the DEFAULT
# ---------------------------------------------------------------------------


def test_the_default_invocation_writes_nothing(fleet: Path) -> None:
    # Arrange — the single most important property of this verb.
    spec = _write_spec(fleet, "alpha")
    before = spec.read_text()
    # Act
    _run()
    # Assert
    assert spec.read_text() == before


def test_the_default_invocation_reports_dry_run_mode(fleet: Path) -> None:
    # Arrange
    _write_spec(fleet, "alpha")
    # Act
    result = _run("--json")
    # Assert
    assert json.loads(result.stdout)["mode"] == "dry-run"


def test_apply_and_dry_run_together_are_rejected(fleet: Path) -> None:
    # Arrange — contradictory intent must never be silently resolved.
    _write_spec(fleet, "alpha")
    # Act
    result = CliRunner().invoke(migrate_layers, ["--apply", "--dry-run"])
    # Assert
    assert "contradictory" in result.output


def test_the_dry_run_counts_the_writable_specs(fleet: Path) -> None:
    # Arrange
    _write_spec(fleet, "alpha")
    _write_spec(fleet, "beta")
    # Act
    result = _run("--json")
    # Assert
    assert json.loads(result.stdout)["writable"] == 2


def test_the_dry_run_reports_the_resolved_layer_sets(fleet: Path) -> None:
    # Arrange — the histogram is the line an operator actually reads.
    _write_spec(fleet, "alpha")
    # Act
    payload = json.loads(_run("--json").stdout)
    # Assert
    assert payload["layer_sets"] == {"user-shared, project-shared, per-agent": 1}


# ---------------------------------------------------------------------------
# Exit codes — a REFUSAL is not a failure, a malformed spec is
# ---------------------------------------------------------------------------


def test_a_clean_plan_exits_zero(fleet: Path) -> None:
    # Arrange
    _write_spec(fleet, "alpha")
    # Act
    result = _run()
    # Assert
    assert result.exit_code == 0


def test_a_refusal_alone_still_exits_zero(fleet: Path) -> None:
    # Arrange — "no anchor" is an expected outcome the report names, not a
    # failure. Exiting non-zero on it teaches readers to ignore the code.
    _write_spec(fleet, "alpha")
    _write_spec(fleet, "flowed", flow=True)
    # Act
    result = _run()
    # Assert
    assert result.exit_code == 0


def test_a_refusal_is_named_in_the_report(fleet: Path) -> None:
    # Arrange — never silent about a decision.
    _write_spec(fleet, "flowed", flow=True)
    # Act
    payload = json.loads(_run("--json").stdout)
    # Assert
    assert payload["refused"][0]["agent"] == "flowed"


def test_an_unreadable_spec_exits_non_zero(fleet: Path) -> None:
    # Arrange
    broken = fleet / "broken"
    broken.mkdir()
    (broken / "spec.yaml").write_text("this: is not an agent spec\n")
    # Act
    result = _run()
    # Assert
    assert result.exit_code == 1


def test_an_apply_refusal_does_not_overwrite_the_refused_list(fleet: Path) -> None:
    # Arrange — `refused` is the per-spec list the EDITOR declined; the
    # apply-level refusal is a sentence. One key meaning both would let a
    # consumer read "the sweep refused nothing" straight off a refusal.
    _write_spec(fleet, "flowed", flow=True)
    broken = fleet / "broken"
    broken.mkdir()
    (broken / "spec.yaml").write_text("this: is not an agent spec\n")
    # Act
    payload = json.loads(_run("--apply", "--json").stdout)
    # Assert
    assert payload["refused"][0]["agent"] == "flowed"


def test_an_unreadable_spec_refuses_the_apply(fleet: Path) -> None:
    # Arrange — a plan that cannot describe every spec must write nothing.
    spec = _write_spec(fleet, "alpha")
    broken = fleet / "broken"
    broken.mkdir()
    (broken / "spec.yaml").write_text("this: is not an agent spec\n")
    before = spec.read_text()
    # Act
    _run("--apply")
    # Assert
    assert spec.read_text() == before


# ---------------------------------------------------------------------------
# Apply — and the measurement that lets it stand
# ---------------------------------------------------------------------------


def test_the_apply_writes_the_declaration(fleet: Path) -> None:
    # Arrange
    spec = _write_spec(fleet, "alpha")
    # Act
    _run("--apply")
    # Assert
    assert "to_home_layers:" in spec.read_text()


def test_the_apply_exits_zero_when_verified(fleet: Path) -> None:
    # Arrange
    _write_spec(fleet, "alpha")
    # Act
    result = _run("--apply")
    # Assert
    assert result.exit_code == 0


def test_the_apply_adds_exactly_one_line_per_spec(fleet: Path) -> None:
    # Arrange
    spec = _write_spec(fleet, "alpha")
    before = len(spec.read_text().splitlines())
    # Act
    _run("--apply")
    # Assert
    assert len(spec.read_text().splitlines()) == before + 1


def test_the_apply_leaves_hook_arming_unchanged(fleet: Path) -> None:
    # Arrange — requirement 6, MEASURED rather than argued: the sweep must not
    # alter what any agent inherits. Re-derived from the specs, not read back
    # from a manifest that no deploy has written.
    spec = _write_spec(fleet, "alpha")
    before = fleet_arming_snapshot([spec]).origins
    # Act
    _run("--apply")
    # Assert
    assert fleet_arming_snapshot([spec]).origins == before


def test_the_verified_apply_reports_a_met_floor(fleet: Path) -> None:
    # Arrange — `safe` on the diff alone would pass over an empty population.
    _write_spec(fleet, "alpha")
    _write_spec(fleet, "beta")
    # Act
    payload = json.loads(_run("--apply", "--json").stdout)
    # Assert
    assert payload["gate"]["floor_met"] is True


def test_the_gate_compares_the_whole_population(fleet: Path) -> None:
    # Arrange — a refused spec is still measured: it was not written, and
    # proving that is exactly what the comparison is for.
    _write_spec(fleet, "alpha")
    _write_spec(fleet, "flowed", flow=True)
    # Act
    payload = json.loads(_run("--apply", "--json").stdout)
    # Assert
    assert payload["gate"]["agents_compared"] == 2


def test_a_second_apply_writes_nothing_more(fleet: Path) -> None:
    # Arrange — idempotent: a completed sweep re-run must not duplicate a key.
    spec = _write_spec(fleet, "alpha")
    _run("--apply")
    after_first = spec.read_text()
    # Act
    _run("--apply")
    # Assert
    assert spec.read_text() == after_first


def test_a_completed_sweep_re_run_exits_zero(fleet: Path) -> None:
    # Arrange
    _write_spec(fleet, "alpha")
    _run("--apply")
    # Act
    result = _run("--apply")
    # Assert
    assert result.exit_code == 0


# ---------------------------------------------------------------------------
# Registration — the verb has to actually be reachable
# ---------------------------------------------------------------------------


def test_the_verb_resolves_through_the_main_lazy_registry() -> None:
    # Arrange — `agents` is `_main.py`'s lazy entry; a leaf that never resolves
    # through it half-exists, which is the failure the three-touch-point
    # registry is prone to.
    from click import Context

    from scitex_agent_container.cli_pkg._main import main

    # Act
    with Context(main) as ctx:
        group = main.get_command(ctx, "agents")
        with Context(group) as sub:
            command = group.get_command(sub, "migrate-layers")
    # Assert
    assert command is not None


def test_the_verb_is_listed_under_maintenance() -> None:
    # Arrange — a command absent from the categories renders in no help
    # section, so it exists but cannot be found.
    from scitex_agent_container.cli_pkg.agent_group import _AgentsGroup

    categories = dict(_AgentsGroup.COMMAND_CATEGORIES)
    # Act
    maintenance = categories["Maintenance"]
    # Assert
    assert "migrate-layers" in maintenance
