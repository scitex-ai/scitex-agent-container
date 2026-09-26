"""``sac agents scratch-migrate`` — the CLI surface of the ADR-0024 sweep.

Real ``CliRunner`` against the real click command, over a real tmp fleet of
real spec files, with a real ``config.yaml`` declaring a real scratch root —
every seam pinned inside ``tmp_path`` so no test can read or delete the
operator's overlays.

What only the CLI owns, and what these pin:

* **dry-run is the DEFAULT** — the safety property of a verb that deletes
  gigabytes, measured by asserting the overlay tree is still on disk;
* the exit-code contract: a named REFUSAL (this host keeps ``/uvwork`` in the
  overlay) is 2, a plan that cannot describe the sweep is 1, and a sound
  preview is 0; and
* that ``--json`` names the scratch root and how it was reached, because an
  operator reading the preview is deciding on THAT directory.

Liveness is the REAL runtime adapter here, not a seam: an agent that has
never started has no ``apptainer_pid`` under its sandboxed state dir, so
``is_running`` answers False from disk — deterministic, and the same code
path ``sac agents status`` walks.

That is only true from a HOST vantage, so the fleet fixture clears the two
apptainer container markers: this suite frequently runs inside an agent
container, where the vantage guard (rightly) abstains for every agent and no
row could ever be movable. The guard's own behaviour is pinned here too, by
setting a marker back — and in full in
``_maintenance/test__scratch_migrate_liveness.py``.

No mocks (PA-306). STX-TQ002 AAA markers; one fact per test (PA-307).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml
from click.testing import CliRunner

from scitex_agent_container._maintenance._scratch_migrate_liveness import (
    CONTAINER_MARKER_ENV,
)
from scitex_agent_container.cli_pkg._agents_scratch_migrate import (
    human_bytes,
    scratch_migrate,
)
from tests.scitex_agent_container._helpers.explicit_spec import explicit_doc


@pytest.fixture
def fleet(tmp_path: Path, env_save_restore) -> Path:
    """A tmp fleet roster plus a config.yaml declaring a tmp scratch root."""
    agents_dir = tmp_path / "agents"
    agents_dir.mkdir()
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    cfg = tmp_path / "config.yaml"
    cfg.write_text(f"scratch_root: {scratch}\n", encoding="utf-8")
    env_save_restore.set("SCITEX_AGENT_CONTAINER_CONFIG", str(cfg))
    env_save_restore.set("SCITEX_AGENT_CONTAINER_AGENTS_DIR", str(agents_dir))
    env_save_restore.set("SCITEX_AGENT_CONTAINER_RUNTIME_DIR", str(tmp_path / "rt"))
    for key in CONTAINER_MARKER_ENV:
        env_save_restore.set(key, "")
    return agents_dir


@pytest.fixture
def overlay_host(tmp_path: Path, env_save_restore) -> Path:
    """A host whose written decision is to keep ``/uvwork`` in the overlay."""
    agents_dir = tmp_path / "agents-none"
    agents_dir.mkdir()
    cfg = tmp_path / "config-none.yaml"
    cfg.write_text(
        "scratch_root: none\nscratch_root_reason: root LV is 8T\n", encoding="utf-8"
    )
    env_save_restore.set("SCITEX_AGENT_CONTAINER_CONFIG", str(cfg))
    env_save_restore.set("SCITEX_AGENT_CONTAINER_AGENTS_DIR", str(agents_dir))
    return agents_dir


def _write_agent(agents_dir: Path, name: str, files: dict[str, str]) -> Path:
    """A real spec with a real directory overlay holding a real uvwork tree."""
    agent_dir = agents_dir / name
    agent_dir.mkdir(parents=True)
    overlay = agent_dir / "overlay"
    doc = explicit_doc(
        {
            "runtime": "tui",
            "workdir": str(agent_dir),
            "apptainer": {"image": "/x.sif", "binds": [], "overlay": str(overlay)},
        }
    )
    (agent_dir / "spec.yaml").write_text(yaml.safe_dump(doc, sort_keys=False))
    source = overlay / "upper" / "uvwork"
    source.mkdir(parents=True)
    for rel, text in files.items():
        path = source / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    return source


def _run(*args):
    """Assert on ``result.stdout``, never ``result.output``.

    ``Result.output`` merges stderr into stdout; the scheduled form of this
    command is ``--json`` piped to a parser, where a single log line on
    stderr would break ``json.loads`` on a command that behaved perfectly.
    """
    return CliRunner().invoke(scratch_migrate, list(args), catch_exceptions=False)


# ---------------------------------------------------------------------------
# Dry-run is the DEFAULT
# ---------------------------------------------------------------------------


def test_the_default_invocation_moves_nothing(fleet: Path) -> None:
    # Arrange — the single most important property of a verb that deletes.
    source = _write_agent(fleet, "alpha", {"bin/uv": "payload"})
    # Act
    _run()
    # Assert
    assert (source / "bin" / "uv").read_text(encoding="utf-8") == "payload"


def test_the_default_invocation_creates_no_destination(fleet: Path) -> None:
    # Arrange
    _write_agent(fleet, "alpha", {"bin/uv": "payload"})
    # Act
    _run()
    # Assert
    assert not (fleet.parent / "scratch" / "sac").exists()


def test_the_default_invocation_reports_dry_run_mode(fleet: Path) -> None:
    # Arrange
    _write_agent(fleet, "alpha", {"bin/uv": "payload"})
    # Act
    result = _run("--json")
    # Assert
    assert json.loads(result.stdout)["mode"] == "dry-run"


def test_the_dry_run_lists_the_agent_it_would_move(fleet: Path) -> None:
    # Arrange
    _write_agent(fleet, "alpha", {"bin/uv": "payload"})
    # Act
    result = _run("--json")
    # Assert
    assert json.loads(result.stdout)["would_move"] == ["alpha"]


def test_the_dry_run_totals_the_bytes_it_would_move(fleet: Path) -> None:
    # Arrange
    _write_agent(fleet, "alpha", {"bin/uv": "x" * 32})
    # Act
    result = _run("--json")
    # Assert
    assert json.loads(result.stdout)["total_bytes"] == 32


def test_the_dry_run_names_the_scratch_root_it_would_write_to(fleet: Path) -> None:
    # Arrange — the operator is deciding about THAT directory.
    _write_agent(fleet, "alpha", {"bin/uv": "payload"})
    # Act
    result = _run("--json")
    # Assert
    assert json.loads(result.stdout)["scratch_root"] == str(fleet.parent / "scratch")


def test_the_dry_run_says_how_the_root_was_reached(fleet: Path) -> None:
    # Arrange — config, default probe or written decision are not the same
    # fact, and the preview must not blur them.
    _write_agent(fleet, "alpha", {"bin/uv": "payload"})
    # Act
    result = _run("--json")
    # Assert
    assert json.loads(result.stdout)["scratch_source"] == "config"


def test_a_sound_preview_exits_zero(fleet: Path) -> None:
    # Arrange
    _write_agent(fleet, "alpha", {"bin/uv": "payload"})
    # Act
    result = _run("--json")
    # Assert
    assert result.exit_code == 0


def test_only_the_named_agent_is_previewed(fleet: Path) -> None:
    # Arrange
    _write_agent(fleet, "alpha", {"bin/uv": "payload"})
    _write_agent(fleet, "beta", {"bin/uv": "payload"})
    # Act
    result = _run("--agent", "alpha", "--json")
    # Assert
    assert [r["agent"] for r in json.loads(result.stdout)["rows"]] == ["alpha"]


def test_the_human_preview_tells_the_operator_it_moved_nothing(fleet: Path) -> None:
    # Arrange
    _write_agent(fleet, "alpha", {"bin/uv": "payload"})
    # Act
    result = _run()
    # Assert
    assert "Nothing was moved" in result.stdout


# ---------------------------------------------------------------------------
# Exit codes — a REFUSAL is not an unsound plan
# ---------------------------------------------------------------------------


def test_a_host_that_keeps_uvwork_in_the_overlay_is_refused(
    overlay_host: Path,
) -> None:
    # Arrange — there is nowhere to migrate TO, in writing.
    # Act
    result = _run("--json")
    # Assert
    assert result.exit_code == 2


def test_the_overlay_host_refusal_states_the_written_reason(
    overlay_host: Path,
) -> None:
    # Arrange
    # Act
    result = _run("--json")
    # Assert
    assert "root LV is 8T" in json.loads(result.stdout)["apply_refused"]


def test_the_overlay_host_refusal_moves_nothing_even_with_apply(
    overlay_host: Path,
) -> None:
    # Arrange — the positive control: --apply does not get past the refusal.
    source = _write_agent(overlay_host, "alpha", {"bin/uv": "payload"})
    # Act
    _run("--apply", "--json")
    # Assert
    assert (source / "bin" / "uv").read_text(encoding="utf-8") == "payload"


def test_an_unknown_agent_makes_the_plan_unsound(fleet: Path) -> None:
    # Arrange — a plan that cannot describe every selected spec is not a
    # description of the sweep.
    _write_agent(fleet, "alpha", {"bin/uv": "payload"})
    # Act
    result = _run("--agent", "ghost", "--json")
    # Assert
    assert result.exit_code == 1


def test_an_unknown_agent_is_named_in_the_payload(fleet: Path) -> None:
    # Arrange
    _write_agent(fleet, "alpha", {"bin/uv": "payload"})
    # Act
    result = _run("--agent", "ghost", "--json")
    # Assert
    assert json.loads(result.stdout)["unknown"] == ["ghost"]


def test_an_unsound_plan_is_not_applied(fleet: Path) -> None:
    # Arrange
    source = _write_agent(fleet, "alpha", {"bin/uv": "payload"})
    # Act
    _run("--agent", "alpha", "--agent", "ghost", "--apply", "--json")
    # Assert
    assert (source / "bin" / "uv").read_text(encoding="utf-8") == "payload"


def test_a_roster_that_was_never_searched_is_not_a_sound_plan(
    fleet: Path, env_save_restore
) -> None:
    # Arrange — the container-vs-host $HOME bug: 0 specs because the root
    # does not exist, which is NOT an empty fleet.
    env_save_restore.set(
        "SCITEX_AGENT_CONTAINER_AGENTS_DIR", str(fleet.parent / "nowhere")
    )
    # Act
    result = _run("--json")
    # Assert
    assert result.exit_code == 1


# ---------------------------------------------------------------------------
# A vantage that cannot read liveness moves nothing, and says why once
# ---------------------------------------------------------------------------


def test_a_host_vantage_reports_no_blindness(fleet: Path) -> None:
    # Arrange — the positive control for the two rows below.
    _write_agent(fleet, "alpha", {"bin/uv": "payload"})
    # Act
    result = _run("--json")
    # Assert
    assert json.loads(result.stdout)["liveness_vantage"] == ""


def test_inside_a_container_nothing_is_movable(
    fleet: Path, env_save_restore
) -> None:
    # Arrange — MEASURED 2026-09-03: from inside an agent container the pid
    # probe called a RUNNING agent "stopped" and offered its 10.3 GiB.
    env_save_restore.set("APPTAINER_CONTAINER", "/x/sac-base.sif")
    _write_agent(fleet, "alpha", {"bin/uv": "payload"})
    # Act
    result = _run("--json")
    # Assert
    assert json.loads(result.stdout)["would_move"] == []


def test_inside_a_container_apply_moves_nothing(
    fleet: Path, env_save_restore
) -> None:
    # Arrange — the abstention has to survive the deliberate act, not only
    # the preview.
    env_save_restore.set("APPTAINER_CONTAINER", "/x/sac-base.sif")
    source = _write_agent(fleet, "alpha", {"bin/uv": "payload"})
    # Act
    _run("--apply", "--json")
    # Assert
    assert (source / "bin" / "uv").read_text(encoding="utf-8") == "payload"


def test_the_container_vantage_is_named_once_in_the_preview(
    fleet: Path, env_save_restore
) -> None:
    # Arrange — 17 identical row reasons are not a headline.
    env_save_restore.set("APPTAINER_CONTAINER", "/x/sac-base.sif")
    _write_agent(fleet, "alpha", {"bin/uv": "payload"})
    # Act
    result = _run()
    # Assert
    assert "LIVENESS UNREADABLE FROM HERE" in result.stdout


# ---------------------------------------------------------------------------
# The unit the operator measured in
# ---------------------------------------------------------------------------


def test_gigabytes_are_reported_in_the_unit_the_overlays_were_measured_in() -> None:
    # Arrange — 11.7 GB of sac's overlay is the number in the incident.
    raw = int(11.7 * 1024**3)
    # Act
    rendered = human_bytes(raw)
    # Assert
    assert rendered == "11.7 GiB"


def test_small_trees_are_reported_in_bytes() -> None:
    # Arrange
    raw = 3
    # Act
    rendered = human_bytes(raw)
    # Assert
    assert rendered == "3 B"
