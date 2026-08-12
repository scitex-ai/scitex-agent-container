"""``sac agents cct-audit`` — the fleet sweep for a silently mute Telegram rail.

Covers ``cli_pkg/_agents_cct_audit`` (card
``sac-cct-rail-loud-when-no-slot-resolves-20260812``). The start-time alarm
closes the class going forward; this verb answers it for the agents ALREADY
running, which is the population the 2026-08-12 outage was drawn from.

Real on-disk v3 specs under a real redirected ``$SCITEX_DIR``, a real temp
secrets pool sourced by a real bash, and the real Click runner — no mocks
(PA-306). STX-TQ002 AAA markers, STX-TQ007 one assert per test. Slot names use
a ``ZZ_``-prefixed namespace so an operator shell's real pool vars can never
collide with the fixtures.

Named ``test__agents_cct_audit.py`` for the PS-202/PS-204 mirror against
``src/scitex_agent_container/cli_pkg/_agents_cct_audit.py``.
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterator
from pathlib import Path

import pytest
import yaml as _yaml
from click.testing import CliRunner

from scitex_agent_container.cli_pkg._agents_cct_audit import (
    _short_pool_label,
    cct_audit,
)
from tests.scitex_agent_container._helpers.explicit_spec import explicit_spec

_CHANNEL = "server:claude-code-telegrammer"
_SECRETS_VAR = "SAC_SECRETS_ENVRC"
# A value-shaped string. No audit row or rendering may contain it.
_SECRET = "zz-secret-value-must-never-be-echoed"


@pytest.fixture
def fleet(tmp_path: Path) -> Iterator[Path]:
    """A real redirected ``$SCITEX_DIR`` with an empty agents dir."""
    saved_dir = os.environ.get("SCITEX_DIR")
    saved_pool = os.environ.get(_SECRETS_VAR)
    agents = tmp_path / "scitex" / "agent-container" / "agents"
    agents.mkdir(parents=True)
    os.environ["SCITEX_DIR"] = str(tmp_path / "scitex")
    try:
        yield agents
    finally:
        for var, val in ((("SCITEX_DIR"), saved_dir), (_SECRETS_VAR, saved_pool)):
            if val is None:
                os.environ.pop(var, None)
            else:
                os.environ[var] = val


def _write_spec(agents: Path, name: str, *, channel: bool, env: dict | None = None):
    """A REAL fully-explicit v3 ``<name>/spec.yaml`` the loader accepts.

    Dir-as-SSoT: the AGENT NAME comes from the directory, exactly as on a real
    host. ``explicit_spec`` deep-merges the two blocks this suite is about
    (``claude.channels`` and ``apptainer.env``) onto the production
    paste-defaults, so the other ~73 required fields stay present.
    """
    body = explicit_spec(
        {
            "host": "${HOSTNAME}",
            "workdir": str(agents.parent),
            "claude": {"channels": [_CHANNEL] if channel else []},
            "apptainer": {"env": dict(env or {})},
        }
    )
    target = agents / name
    target.mkdir(parents=True, exist_ok=True)
    (target / "spec.yaml").write_text(
        _yaml.safe_dump(
            {
                "apiVersion": "scitex-agent-container/v3",
                "kind": "Agent",
                "metadata": {"labels": {}},
                "spec": body,
            }
        ),
        encoding="utf-8",
    )


def _pool(tmp_path: Path, body: str) -> None:
    """A REAL secrets file, pointed at by SAC_SECRETS_ENVRC."""
    path = tmp_path / "zz-pool.src"
    path.write_text(body, encoding="utf-8")
    os.environ[_SECRETS_VAR] = str(path)


def _run(*args: str):
    return CliRunner().invoke(cct_audit, list(args))


def _payload(result) -> dict:
    return json.loads(result.output)


# ---------------------------------------------------------------------------
# the sweep
# ---------------------------------------------------------------------------


def test_an_empty_fleet_is_clean(fleet: Path, tmp_path: Path) -> None:
    # Arrange
    _pool(tmp_path, "export CCT_BOT_TOKEN_ZZ_UNUSED=zz\n")
    # Act
    result = _run("--json")
    # Assert
    assert result.exit_code == 0


def test_an_agent_with_a_resolving_slot_is_up(fleet: Path, tmp_path: Path) -> None:
    # Arrange
    _pool(tmp_path, "export CCT_BOT_TOKEN_ZZ_HASBOT=" + _SECRET + "\n")
    _write_spec(fleet, "zz-hasbot", channel=True)
    # Act
    result = _run("--json")
    # Assert
    assert _payload(result)["agents"][0]["state"] == "up"


def test_a_declared_slot_that_does_not_exist_is_down(
    fleet: Path, tmp_path: Path
) -> None:
    # Arrange — the misconfiguration shape: somebody typed a mapping and it
    # does not work.
    _pool(tmp_path, "export CCT_BOT_TOKEN_ZZ_REAL=" + _SECRET + "\n")
    _write_spec(
        fleet, "zz-typo", channel=True, env={"CCT_BOT_TOKEN_SLOT": "ZZ_MISSING"}
    )
    # Act
    result = _run("--json")
    # Assert
    assert _payload(result)["agents"][0]["state"] == "down"


def test_a_mute_agent_makes_the_sweep_fail(fleet: Path, tmp_path: Path) -> None:
    # Arrange — the exit code is what lets a timer or a relocation preflight
    # gate on this, which is the gap the 2026-08-12 relocation went through.
    _pool(tmp_path, "export CCT_BOT_TOKEN_ZZ_OTHER=" + _SECRET + "\n")
    _write_spec(fleet, "zz-mute", channel=True)
    # Act
    result = _run("--json")
    # Assert
    assert result.exit_code == 1


def test_an_agent_that_never_asked_for_a_rail_is_omitted(
    fleet: Path, tmp_path: Path
) -> None:
    # Arrange — bot-less BY DECLARATION is not a finding, and listing it would
    # bury the agents that are actually broken.
    _pool(tmp_path, "export CCT_BOT_TOKEN_ZZ_UNUSED=zz\n")
    _write_spec(fleet, "zz-norail", channel=False)
    # Act
    result = _run("--json")
    # Assert
    assert _payload(result)["agents"] == []


def test_an_unrequested_rail_is_listed_under_all(fleet: Path, tmp_path: Path) -> None:
    # Arrange
    _pool(tmp_path, "export CCT_BOT_TOKEN_ZZ_UNUSED=zz\n")
    _write_spec(fleet, "zz-norail", channel=False)
    # Act
    result = _run("--json", "--all")
    # Assert
    assert _payload(result)["agents"][0]["state"] == "not-requested"


# ---------------------------------------------------------------------------
# an unreadable spec is a finding, not a dropped row
# ---------------------------------------------------------------------------


def test_an_unloadable_spec_becomes_an_unknown_row(fleet: Path, tmp_path: Path) -> None:
    # Arrange — a spec sac cannot read is exactly the kind of thing a sweep
    # exists to surface; silently skipping it would be the same blindness in
    # a new place.
    _pool(tmp_path, "export CCT_BOT_TOKEN_ZZ_UNUSED=zz\n")
    (fleet / "zz-broken").mkdir(parents=True)
    (fleet / "zz-broken" / "spec.yaml").write_text("{{{ not yaml", encoding="utf-8")
    # Act
    result = _run("--json")
    # Assert
    assert _payload(result)["agents"][0]["state"] == "unknown"


def test_an_unloadable_spec_is_named(fleet: Path, tmp_path: Path) -> None:
    # Arrange
    _pool(tmp_path, "export CCT_BOT_TOKEN_ZZ_UNUSED=zz\n")
    (fleet / "zz-broken").mkdir(parents=True)
    (fleet / "zz-broken" / "spec.yaml").write_text("{{{ not yaml", encoding="utf-8")
    # Act
    result = _run("--json")
    # Assert
    assert _payload(result)["agents"][0]["agent"] == "zz-broken"


# ---------------------------------------------------------------------------
# the pool vantage point, and the token that must never appear
# ---------------------------------------------------------------------------


def test_an_inconclusive_pool_makes_every_row_unknown(
    fleet: Path, tmp_path: Path
) -> None:
    # Arrange — the var is set but points nowhere: the caller cannot see the
    # pool, so nothing it concludes about absence is worth anything.
    os.environ[_SECRETS_VAR] = str(tmp_path / "absent.src")
    _write_spec(fleet, "zz-blind", channel=True)
    # Act
    result = _run("--json")
    # Assert
    assert _payload(result)["agents"][0]["state"] == "unknown"


def test_the_report_names_the_pool_source(fleet: Path, tmp_path: Path) -> None:
    # Arrange — vantage point is part of the measurement, so every run must
    # say where it looked.
    _pool(tmp_path, "export CCT_BOT_TOKEN_ZZ_UNUSED=zz\n")
    # Act
    result = _run("--json")
    # Assert
    assert "zz-pool.src" in _payload(result)["pool_source"]


def test_the_report_never_carries_a_token_value(fleet: Path, tmp_path: Path) -> None:
    # Arrange — a resolving slot, so the value is in hand while rows are built.
    _pool(tmp_path, "export CCT_BOT_TOKEN_ZZ_HASBOT=" + _SECRET + "\n")
    _write_spec(fleet, "zz-hasbot", channel=True)
    # Act
    result = _run("--json")
    # Assert
    assert _SECRET not in result.output


def test_a_long_pool_label_is_condensed_for_the_console() -> None:
    # Arrange — a real fleet host lists ~28 absolute paths, which wraps to a
    # dozen lines and buries the counts under it.
    label = "SAC_SECRETS_ENVRC=/s/010_scitex/a.src:/s/010_scitex/b.src"
    # Act
    short = _short_pool_label(label)
    # Assert
    assert short == "SAC_SECRETS_ENVRC=2 secret file(s) under /s/010_scitex"


def test_a_pool_label_that_is_not_a_path_list_is_left_alone() -> None:
    # Arrange — the "unset, no default found" label is prose, not paths, and
    # condensing it would destroy the only thing it says.
    label = "SAC_SECRETS_ENVRC is UNSET and no canonical default pool files were found"
    # Act
    short = _short_pool_label(label)
    # Assert
    assert short == label


def test_the_table_rendering_never_carries_a_token_value(
    fleet: Path, tmp_path: Path
) -> None:
    # Arrange — the human rendering is a separate code path from the JSON one.
    _pool(tmp_path, "export CCT_BOT_TOKEN_ZZ_HASBOT=" + _SECRET + "\n")
    _write_spec(fleet, "zz-hasbot", channel=True)
    # Act
    result = _run()
    # Assert
    assert _SECRET not in result.output
