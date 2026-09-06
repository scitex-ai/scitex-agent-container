"""``sac agents migrate-engines`` — the CLI surface of the spec.engines sweep.

Real ``CliRunner`` against the real click command, over a real tmp fleet of
real spec files. Every root is pinned inside ``tmp_path`` through the
documented env seams, so no test can read or rewrite the operator's live specs.

What only the CLI owns, and what these pin:

* **dry-run is the DEFAULT** — asserted on the file BYTES and on the file's
  MTIME, because "wrote the same content back" is still a write;
* batching, which is the operator's own condition for trusting a 119-file
  rewrite: ``--agent``, ``--host``, ``--limit``;
* that a refusal is NAMED and does NOT fail the exit code, while an
  unreadable spec does; and
* that the apply is behaviour-preserving in the only way that counts —
  measured, by re-loading every written spec and comparing the backend.

STX-NM002: no mocks. STX-TQ002 / TQ007: AAA markers, one fact per test.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
import yaml
from click.testing import CliRunner

from scitex_agent_container.cli_pkg._agents_migrate_engines import migrate_engines
from scitex_agent_container.config._qwen_gateway import QWEN_ENGINE_KEY
from tests.scitex_agent_container._helpers.explicit_spec import explicit_doc

_SETTINGS = {"hooks": {"PreToolUse": [{"hooks": [{"command": "guard.sh"}]}]}}


def _write_settings(to_home: Path) -> None:
    claude = to_home / ".claude"
    claude.mkdir(parents=True, exist_ok=True)
    (claude / "settings.json").write_text(json.dumps(_SETTINGS))


def _write_spec(agents_dir: Path, name: str, *, model="opus[1m]", **overrides) -> Path:
    agent_dir = agents_dir / name
    agent_dir.mkdir(parents=True)
    _write_settings(agent_dir / "to_home")
    spec = {"to_home": "./to_home", "claude": {"model": model}}
    spec.update(overrides)
    doc = explicit_doc(spec)
    path = agent_dir / "spec.yaml"
    path.write_text(yaml.safe_dump(doc, sort_keys=False))
    return path


@pytest.fixture
def fleet(tmp_path: Path):
    """A tmp fleet with every root pinned inside tmp_path."""
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

    ``Result.output`` is stdout and stderr COMBINED; the scheduled form of
    this command is ``--json`` piped to a parser, where they were never
    merged. A single log line on stderr would otherwise make ``json.loads``
    fail on a command that behaved perfectly.
    """
    return CliRunner().invoke(migrate_engines, list(args), catch_exceptions=False)


# ---------------------------------------------------------------------------
# Dry-run is the DEFAULT
# ---------------------------------------------------------------------------


def test_the_default_invocation_writes_nothing(fleet: Path) -> None:
    # Arrange — the single most important property of this verb.
    spec = _write_spec(fleet, "alpha")
    before = spec.read_text()
    # Act
    _run("--no-diff")
    # Assert
    assert spec.read_text() == before


def test_the_default_invocation_does_not_touch_the_mtime(fleet: Path) -> None:
    # Arrange — writing identical bytes back is still a write.
    spec = _write_spec(fleet, "alpha")
    before = spec.stat().st_mtime_ns
    # Act
    _run("--no-diff")
    # Assert
    assert spec.stat().st_mtime_ns == before


def test_the_default_invocation_reports_dry_run_mode(fleet: Path) -> None:
    # Arrange
    _write_spec(fleet, "alpha")
    # Act
    result = _run("--no-diff", "--json")
    # Assert
    assert json.loads(result.stdout)["mode"] == "dry-run"


def test_the_dry_run_counts_what_it_would_migrate(fleet: Path) -> None:
    # Arrange
    _write_spec(fleet, "alpha")
    _write_spec(fleet, "beta")
    # Act
    result = _run("--no-diff", "--json")
    # Assert
    assert json.loads(result.stdout)["would_migrate"] == 2


def test_the_dry_run_carries_a_unified_diff_per_spec(fleet: Path) -> None:
    # Arrange — a reviewable diff is the operator's stated condition.
    _write_spec(fleet, "alpha")
    # Act
    payload = json.loads(_run("--json").stdout)
    # Assert
    assert payload["diffs"]["alpha"].startswith("--- a/alpha/spec.yaml")


def test_the_dry_run_diff_shows_the_engines_block(fleet: Path) -> None:
    # Arrange
    _write_spec(fleet, "alpha")
    # Act
    payload = json.loads(_run("--json").stdout)
    # Assert
    assert "+  engines:" in payload["diffs"]["alpha"]


def test_the_dry_run_exits_zero_on_a_sound_plan(fleet: Path) -> None:
    # Arrange
    _write_spec(fleet, "alpha")
    # Act
    result = _run("--no-diff", "--json")
    # Assert
    assert result.exit_code == 0


def test_apply_and_dry_run_together_is_a_usage_error(fleet: Path) -> None:
    # Arrange
    _write_spec(fleet, "alpha")
    # Act
    result = CliRunner().invoke(migrate_engines, ["--apply", "--dry-run"])
    # Assert
    assert result.exit_code == 2


# ---------------------------------------------------------------------------
# Batching — nobody has to rewrite 119 files to rewrite one
# ---------------------------------------------------------------------------


def test_a_batch_by_agent_selects_only_that_agent(fleet: Path) -> None:
    # Arrange
    _write_spec(fleet, "alpha")
    _write_spec(fleet, "beta")
    # Act
    payload = json.loads(_run("--no-diff", "--json", "-a", "alpha").stdout)
    # Assert
    assert payload["specs"] == 1


def test_a_batch_by_host_selects_only_that_hosts_specs(fleet: Path) -> None:
    # Arrange
    _write_spec(fleet, "alpha", host="scitex-compute-01")
    _write_spec(fleet, "beta", host="scitex-compute-04")
    # Act
    payload = json.loads(
        _run("--no-diff", "--json", "--host", "scitex-compute-04").stdout
    )
    # Assert
    assert payload["would_migrate"] == 1


def test_limit_caps_the_batch(fleet: Path) -> None:
    # Arrange
    for name in ("alpha", "beta", "gamma"):
        _write_spec(fleet, name)
    # Act
    payload = json.loads(_run("--no-diff", "--json", "--limit", "2").stdout)
    # Assert
    assert payload["specs"] == 2


def test_template_specs_are_named_as_not_searched(fleet: Path) -> None:
    # Arrange — `agents create` copies these; leaving them silently behind
    # re-introduces the legacy shape on every new agent.
    _write_spec(fleet, "_template_handyman")
    _write_spec(fleet, "alpha")
    # Act
    payload = json.loads(_run("--no-diff", "--json").stdout)
    # Assert
    assert payload["skipped_templates"] == ["_template_handyman"]


def test_templates_flag_includes_them(fleet: Path) -> None:
    # Arrange
    _write_spec(fleet, "_template_handyman")
    _write_spec(fleet, "alpha")
    # Act
    payload = json.loads(_run("--no-diff", "--json", "--templates").stdout)
    # Assert
    assert payload["specs"] == 2


# ---------------------------------------------------------------------------
# Refusals are named, and are not failures
# ---------------------------------------------------------------------------


def test_a_spec_with_no_model_is_named_in_the_refusals(fleet: Path) -> None:
    # Arrange — the explicit-spec default is model: '' , so this is real.
    _write_spec(fleet, "alpha", model="")
    # Act
    payload = json.loads(_run("--no-diff", "--json").stdout)
    # Assert
    assert payload["refused"][0]["agent"] == "alpha"


def test_a_refusal_carries_its_reason(fleet: Path) -> None:
    # Arrange
    _write_spec(fleet, "alpha", model="")
    # Act
    payload = json.loads(_run("--no-diff", "--json").stdout)
    # Assert
    assert "no model" in payload["refused"][0]["reason"]


def test_a_refusal_does_not_fail_the_exit_code(fleet: Path) -> None:
    # Arrange — a named refusal is an outcome a human resolves.
    _write_spec(fleet, "alpha", model="")
    # Act
    result = _run("--no-diff", "--json")
    # Assert
    assert result.exit_code == 0


def test_a_refused_spec_is_not_counted_as_migrated(fleet: Path) -> None:
    # Arrange
    _write_spec(fleet, "alpha", model="")
    # Act
    payload = json.loads(_run("--no-diff", "--json").stdout)
    # Assert
    assert payload["would_migrate"] == 0


def test_an_empty_roster_is_reported_as_unsearched(tmp_path: Path) -> None:
    # Arrange — a root that does not exist is NOT a fleet with nothing to do.
    keys = {
        "SCITEX_AGENT_CONTAINER_AGENTS_DIR": str(tmp_path / "nowhere"),
        "SAC_SPEC_CACHE_DISABLE": "1",
    }
    saved = {k: os.environ.get(k) for k in keys}
    os.environ.update(keys)
    # Act
    try:
        payload = json.loads(_run("--no-diff", "--json").stdout)
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
    # Assert
    assert payload["roster"] == "absent"


# ---------------------------------------------------------------------------
# The apply, and the measurement that gates it
# ---------------------------------------------------------------------------


def test_apply_writes_the_engines_block(fleet: Path) -> None:
    # Arrange
    spec = _write_spec(fleet, "alpha")
    # Act
    _run("--apply", "--no-diff", "--json")
    # Assert
    assert "engines" in yaml.safe_load(spec.read_text())["spec"]


def test_apply_writes_both_engines(fleet: Path) -> None:
    # Arrange
    spec = _write_spec(fleet, "alpha")
    # Act
    _run("--apply", "--no-diff", "--json")
    # Assert
    assert list(yaml.safe_load(spec.read_text())["spec"]["engines"]) == [
        "claude",
        QWEN_ENGINE_KEY,
    ]


def test_apply_reports_what_it_wrote(fleet: Path) -> None:
    # Arrange
    _write_spec(fleet, "alpha")
    # Act
    payload = json.loads(_run("--apply", "--no-diff", "--json").stdout)
    # Assert
    assert payload["written"] == ["alpha"]


def test_apply_archives_the_originals_first(fleet: Path) -> None:
    # Arrange — a rollback must be a copy-back, not a reconstruction.
    _write_spec(fleet, "alpha")
    # Act
    payload = json.loads(_run("--apply", "--no-diff", "--json").stdout)
    # Assert
    assert (Path(payload["archive_dir"]) / "alpha.spec.yaml").is_file()


def test_the_applied_spec_still_starts_on_the_same_model(fleet: Path) -> None:
    # Arrange — measured through the production loader, not argued.
    spec = _write_spec(fleet, "alpha")
    # Act
    _run("--apply", "--no-diff", "--json")
    # Assert
    from scitex_agent_container.config import load_config

    assert load_config(spec).claude.model == "opus[1m]"


def test_the_applied_spec_selects_its_default_engine(fleet: Path) -> None:
    # Arrange
    spec = _write_spec(fleet, "alpha")
    # Act
    _run("--apply", "--no-diff", "--json")
    # Assert
    from scitex_agent_container.config import load_config

    assert load_config(spec).engine_key == "claude"


def test_a_second_apply_writes_nothing_more(fleet: Path) -> None:
    # Arrange
    _write_spec(fleet, "alpha")
    _run("--apply", "--no-diff", "--json")
    # Act
    payload = json.loads(_run("--apply", "--no-diff", "--json").stdout)
    # Assert
    assert payload["written"] == []


def test_a_second_run_reports_the_spec_as_already_migrated(fleet: Path) -> None:
    # Arrange
    _write_spec(fleet, "alpha")
    _run("--apply", "--no-diff", "--json")
    # Act
    payload = json.loads(_run("--no-diff", "--json").stdout)
    # Assert
    assert payload["already_migrated"] == ["alpha"]


def test_apply_exits_zero_when_the_gate_passes(fleet: Path) -> None:
    # Arrange
    _write_spec(fleet, "alpha")
    # Act
    result = _run("--apply", "--no-diff", "--json")
    # Assert
    assert result.exit_code == 0


# ---------------------------------------------------------------------------
# The gateway preflight, three-valued
# ---------------------------------------------------------------------------


def test_the_preflight_reports_a_named_state_not_a_boolean(fleet: Path) -> None:
    # Arrange — the address may or may not be reachable from the test host;
    # what is asserted is that the answer is a NAME either way.
    _write_spec(fleet, "alpha")
    # Act
    payload = json.loads(_run("--no-diff", "--json", "--preflight").stdout)
    # Assert
    assert isinstance(payload["preflight"]["state"], str)


def test_the_preflight_states_which_gateway_it_asked(fleet: Path) -> None:
    # Arrange
    _write_spec(fleet, "alpha")
    # Act
    payload = json.loads(_run("--no-diff", "--json", "--preflight").stdout)
    # Assert
    assert payload["preflight"]["url"].startswith("http")


def test_without_the_flag_no_preflight_is_run(fleet: Path) -> None:
    # Arrange — a sweep must not dial the network unless asked.
    _write_spec(fleet, "alpha")
    # Act
    payload = json.loads(_run("--no-diff", "--json").stdout)
    # Assert
    assert payload["preflight"] is None
