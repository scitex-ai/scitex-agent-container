"""The WRITING half of the ``spec.engines`` sweep — and its undo.

Mutation testing over the previous suite found that every SAFETY mechanism
here survived: deleting the before/after measurement, making the rollback
unreachable, and dropping the pre-write "could not be loaded" refusal each
left 94 tests green. Those mechanisms are the reason a 119-file rewrite was
considered safe to run, so they are what this module exercises.

No mocks and no sabotage of production code. Where a test needs a write that
must be rolled back, it hands ``apply_engines_migration`` a REAL plan whose
``new_text`` is a real spec declaring a different backend — which is exactly
what a bulk-replacement bug would produce, and the failure the gate exists
to catch.

STX-NM002: no mocks. STX-TQ002 / TQ007: AAA markers, one fact per test.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
import yaml

from scitex_agent_container._maintenance._engines_apply import (
    _backend_snapshot,
    apply_engines_migration,
)
from scitex_agent_container._maintenance._engines_migration import (
    STATE_MIGRATED,
    EnginesPlan,
    SpecOutcome,
    plan_engines_migration,
    select_spec_paths,
)
from tests.scitex_agent_container._helpers.explicit_spec import explicit_doc

_SETTINGS = '{"hooks": {}}'


def _write_settings(to_home: Path) -> None:
    claude = to_home / ".claude"
    claude.mkdir(parents=True, exist_ok=True)
    (claude / "settings.json").write_text(_SETTINGS)


def _write_spec(root: Path, name: str, *, model="opus[1m]", **overrides) -> Path:
    agent_dir = root / name
    agent_dir.mkdir(parents=True)
    _write_settings(agent_dir / "to_home")
    spec = {"to_home": "./to_home", "claude": {"model": model}}
    spec.update(overrides)
    path = agent_dir / "spec.yaml"
    path.write_text(yaml.safe_dump(explicit_doc(spec), sort_keys=False))
    return path


@pytest.fixture
def fleet(tmp_path: Path):
    """A tmp fleet with every root pinned inside tmp_path."""
    agents = tmp_path / "agents"
    agents.mkdir()
    _write_settings(agents / "_shared" / "to_home")
    user_shared = tmp_path / "user-baseline" / "to_home"
    _write_settings(user_shared)
    keys = {
        "SCITEX_AGENT_CONTAINER_AGENTS_DIR": str(agents),
        "SAC_USER_TO_HOME_BASELINE": str(user_shared),
        "SAC_SPEC_CACHE_DISABLE": "1",
    }
    saved = {k: os.environ.get(k) for k in keys}
    os.environ.update(keys)
    try:
        yield agents
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def _plan(fleet: Path, **kwargs) -> EnginesPlan:
    paths, skipped = select_spec_paths(fleet)
    return plan_engines_migration(
        paths, root=fleet, skipped_templates=skipped, **kwargs
    )


def _drifting_plan(fleet: Path, path: Path) -> EnginesPlan:
    """A REAL plan whose written text declares a DIFFERENT model.

    Not a mock and not a patched renderer: a spec body a broken bulk edit
    would plausibly emit. The gate's whole claim is that it catches this.
    """
    new_text = path.read_text().replace("model: opus[1m]", "model: haiku")
    return EnginesPlan(
        outcomes=(
            SpecOutcome(
                path.parent.name, path, STATE_MIGRATED, new_text=new_text
            ),
        ),
        roster=None,
    )


# ---------------------------------------------------------------------------
# The gate, and the rollback behind it
# ---------------------------------------------------------------------------


def test_a_backend_change_is_not_applied(fleet: Path, tmp_path: Path) -> None:
    # Arrange
    spec = _write_spec(fleet, "alpha")
    plan = _drifting_plan(fleet, spec)
    # Act
    result = apply_engines_migration(plan, tmp_path / "archive")
    # Assert
    assert result.applied is False


def test_a_backend_change_is_named_as_drift(fleet: Path, tmp_path: Path) -> None:
    # Arrange
    spec = _write_spec(fleet, "alpha")
    plan = _drifting_plan(fleet, spec)
    # Act
    result = apply_engines_migration(plan, tmp_path / "archive")
    # Assert
    assert any("alpha" in entry for entry in result.drift)


def test_a_backend_change_restores_the_original_bytes(
    fleet: Path, tmp_path: Path
) -> None:
    # Arrange — the rollback must be a copy-back, not a reconstruction.
    spec = _write_spec(fleet, "alpha")
    original = spec.read_bytes()
    plan = _drifting_plan(fleet, spec)
    # Act
    apply_engines_migration(plan, tmp_path / "archive")
    # Assert
    assert spec.read_bytes() == original


def test_a_rolled_back_apply_says_where_the_archive_is(
    fleet: Path, tmp_path: Path
) -> None:
    # Arrange
    spec = _write_spec(fleet, "alpha")
    plan = _drifting_plan(fleet, spec)
    # Act
    result = apply_engines_migration(plan, tmp_path / "archive")
    # Assert
    assert str(result.archive_dir) in result.rolled_back


def test_the_snapshot_measures_the_top_level_model(fleet: Path) -> None:
    # Arrange — measuring only `claude.model` (the ENGINE-RESOLVED field) is
    # what let 117 specs flip their reported model under a zero-drift
    # certificate.
    spec = _write_spec(fleet, "alpha")
    # Act
    snapshot = _backend_snapshot(spec)
    # Assert
    assert "opus[1m]" in snapshot


def test_the_snapshot_measures_the_injected_model_env(fleet: Path) -> None:
    # Arrange
    spec = _write_spec(fleet, "alpha")
    # Act
    snapshot = _backend_snapshot(spec)
    # Assert
    assert "Claude Opus (1M)" in snapshot


def test_the_gate_passes_on_the_real_migration(fleet: Path, tmp_path: Path) -> None:
    # Arrange — the positive control: the gate must still let the real edit
    # through, or the tests above would pass with the gate stuck shut.
    _write_spec(fleet, "alpha")
    plan = _plan(fleet)
    # Act
    result = apply_engines_migration(plan, tmp_path / "archive")
    # Assert
    assert result.written == ("alpha",)


def test_the_real_migration_leaves_the_backend_identical(
    fleet: Path, tmp_path: Path
) -> None:
    # Arrange — measured through the production loader, both sides.
    spec = _write_spec(fleet, "alpha")
    before = _backend_snapshot(spec)
    # Act
    apply_engines_migration(_plan(fleet), tmp_path / "archive")
    # Assert
    assert _backend_snapshot(spec) == before


# ---------------------------------------------------------------------------
# A spec that will not load is refused BEFORE the first byte
# ---------------------------------------------------------------------------


def _unloadable_plan(fleet: Path, path: Path) -> EnginesPlan:
    """A spec the editor migrates and the LOADER rejects (real, not forced).

    ``to_home_layers`` as a mapping is a declaration the loader raises on,
    while ``validate_raw`` reports nothing new for it, so the edit itself
    verifies clean. That asymmetry is exactly the case the pre-write refusal
    exists for.
    """
    doc = yaml.safe_load(path.read_text())
    doc["spec"]["to_home_layers"] = {"unusable": True}
    path.write_text(yaml.safe_dump(doc, sort_keys=False))
    return _plan(fleet)


def test_an_unloadable_spec_refuses_the_apply(fleet: Path, tmp_path: Path) -> None:
    # Arrange
    spec = _write_spec(fleet, "alpha")
    plan = _unloadable_plan(fleet, spec)
    # Act
    result = apply_engines_migration(plan, tmp_path / "archive")
    # Assert
    assert result.refused


def test_an_unloadable_spec_is_named_in_the_refusal(
    fleet: Path, tmp_path: Path
) -> None:
    # Arrange
    spec = _write_spec(fleet, "alpha")
    plan = _unloadable_plan(fleet, spec)
    # Act
    result = apply_engines_migration(plan, tmp_path / "archive")
    # Assert
    assert "alpha" in result.refused


def test_an_unloadable_spec_is_not_written_at_all(
    fleet: Path, tmp_path: Path
) -> None:
    # Arrange — the mtime, not the bytes: a rollback restores the bytes, so
    # only the mtime distinguishes "refused before writing" from "written
    # and undone".
    spec = _write_spec(fleet, "alpha")
    plan = _unloadable_plan(fleet, spec)
    mtime = spec.stat().st_mtime_ns
    # Act
    apply_engines_migration(plan, tmp_path / "archive")
    # Assert
    assert spec.stat().st_mtime_ns == mtime


# ---------------------------------------------------------------------------
# A failed write rolls the batch back instead of aborting the process
# ---------------------------------------------------------------------------


def _readonly_dir_fleet(fleet: Path) -> "tuple[Path, Path, Path]":
    """Three specs where the LAST one's directory refuses a new file."""
    first = _write_spec(fleet, "a1")
    second = _write_spec(fleet, "a2")
    third = _write_spec(fleet, "a3")
    third.parent.chmod(0o555)
    return first, second, third


def test_a_failed_write_does_not_raise_out_of_the_apply(
    fleet: Path, tmp_path: Path
) -> None:
    # Arrange — measured: this raised through the Click callback and exited 1
    # on a traceback, leaving two specs rewritten and four legacy.
    _readonly_dir_fleet(fleet)
    plan = _plan(fleet)
    # Act
    try:
        result = apply_engines_migration(plan, tmp_path / "archive")
    finally:
        (fleet / "a3").chmod(0o755)
    # Assert
    assert result.applied is False


def test_a_failed_write_restores_the_specs_already_written(
    fleet: Path, tmp_path: Path
) -> None:
    # Arrange
    first, _, _ = _readonly_dir_fleet(fleet)
    original = first.read_bytes()
    plan = _plan(fleet)
    # Act
    try:
        apply_engines_migration(plan, tmp_path / "archive")
    finally:
        (fleet / "a3").chmod(0o755)
    # Assert
    assert first.read_bytes() == original


def test_a_failed_write_leaves_no_half_migrated_fleet(
    fleet: Path, tmp_path: Path
) -> None:
    # Arrange
    _readonly_dir_fleet(fleet)
    plan = _plan(fleet)
    # Act
    try:
        apply_engines_migration(plan, tmp_path / "archive")
    finally:
        (fleet / "a3").chmod(0o755)
    # Assert
    assert not [
        p
        for p in sorted(fleet.glob("*/spec.yaml"))
        if "engines" in yaml.safe_load(p.read_text())["spec"]
    ]


def test_a_failed_write_names_the_spec_that_failed(
    fleet: Path, tmp_path: Path
) -> None:
    # Arrange
    _readonly_dir_fleet(fleet)
    plan = _plan(fleet)
    # Act
    try:
        result = apply_engines_migration(plan, tmp_path / "archive")
    finally:
        (fleet / "a3").chmod(0o755)
    # Assert
    assert any("a3" in entry for entry in result.errors)


def test_a_failed_write_says_where_the_archive_is(
    fleet: Path, tmp_path: Path
) -> None:
    # Arrange — the archive was taken and never mentioned, so the operator
    # was not told where the undo lived.
    _readonly_dir_fleet(fleet)
    plan = _plan(fleet)
    # Act
    try:
        result = apply_engines_migration(plan, tmp_path / "archive")
    finally:
        (fleet / "a3").chmod(0o755)
    # Assert
    assert str(tmp_path / "archive") in result.rolled_back


def test_an_unarchivable_batch_writes_nothing(fleet: Path, tmp_path: Path) -> None:
    # Arrange — no archive means no copy-back, so no write may happen.
    spec = _write_spec(fleet, "alpha")
    original = spec.read_bytes()
    plan = _plan(fleet)
    blocked = tmp_path / "blocked"
    blocked.mkdir()
    blocked.chmod(0o555)
    # Act
    try:
        apply_engines_migration(plan, blocked / "archive")
    finally:
        blocked.chmod(0o755)
    # Assert
    assert spec.read_bytes() == original


def test_an_unarchivable_batch_is_refused_by_name(
    fleet: Path, tmp_path: Path
) -> None:
    # Arrange
    _write_spec(fleet, "alpha")
    plan = _plan(fleet)
    blocked = tmp_path / "blocked"
    blocked.mkdir()
    blocked.chmod(0o555)
    # Act
    try:
        result = apply_engines_migration(plan, blocked / "archive")
    finally:
        blocked.chmod(0o755)
    # Assert
    assert "could not be archived" in result.refused


def test_the_write_preserves_the_specs_file_mode(fleet: Path, tmp_path: Path) -> None:
    # Arrange — an atomic replace brings its own temp file's mode with it
    # unless the original's is copied across.
    spec = _write_spec(fleet, "alpha")
    spec.chmod(0o640)
    plan = _plan(fleet)
    # Act
    apply_engines_migration(plan, tmp_path / "archive")
    # Assert
    assert spec.stat().st_mode & 0o777 == 0o640
