"""CLI tests for ``sac creds`` — the loud, honest credential-state answer.

No mocks and no ``monkeypatch``. ``CliRunner`` drives the real click
commands; the DSN is supplied by real ``os.environ`` writes in
``yield``-based fixtures that restore on teardown.

The store-backed verbs run against a REAL postgres when one is reachable
and SKIP with the reason spelled out otherwise — a skipped database test
and a green one must not look alike.

The unreachable-store tests need no database at all, which is the point:
they pin that an unreachable store is reported as "nothing was checked",
never as a clean bill of health.

Each test: AAA markers (TQ002), one assertion (TQ007), 3+-word name.
"""

from __future__ import annotations

import json
import os

import pytest
from click.testing import CliRunner

from scitex_agent_container._credstate import _store
from scitex_agent_container.cli_pkg.creds_cmds import creds

TEST_DSN = os.environ.get(
    "SAC_CREDSTATE_TEST_DSN",
    "postgresql://scitex_cards@127.0.0.1:55432/scitex_state_test_credstate",
)

NODE = "test-node-cli"
#: A port nothing listens on, so the connect fails for real.
DEAD_DSN = "postgresql://nobody@127.0.0.1:55599/nothing"


def _set_dsn(value: str | None):
    """Set/clear the real env var, returning the prior value."""
    saved = os.environ.get(_store.DSN_ENV)
    if value is None:
        os.environ.pop(_store.DSN_ENV, None)
    else:
        os.environ[_store.DSN_ENV] = value
    return saved


def _restore_dsn(saved: str | None) -> None:
    if saved is None:
        os.environ.pop(_store.DSN_ENV, None)
    else:
        os.environ[_store.DSN_ENV] = saved


@pytest.fixture
def dead_store():
    """A DSN that resolves but cannot connect — a real failed connection."""
    saved = _set_dsn(DEAD_DSN)
    try:
        yield DEAD_DSN
    finally:
        _restore_dsn(saved)


@pytest.fixture
def unconfigured_store():
    """No DSN at all — the refuse-to-guess path."""
    saved = _set_dsn(None)
    try:
        yield None
    finally:
        _restore_dsn(saved)


@pytest.fixture
def store_env():
    """Point the CLI at a real, truncated store, or skip loudly."""
    try:
        import psycopg  # noqa: F401
    except ModuleNotFoundError:
        pytest.skip("psycopg (v3) absent; the store-backed verbs were NOT run.")
    try:
        conn = _store.open_store(TEST_DSN)
    except Exception as exc:  # noqa: BLE001 - the reason must reach the operator
        pytest.skip(
            f"no postgres at {TEST_DSN} ({type(exc).__name__}); the "
            f"store-backed verbs were NOT run. This is a skip, not a pass."
        )
    with conn.cursor() as cur:
        for table in (
            "credential_observation",
            "credential_placement",
            "credential_descriptor",
        ):
            cur.execute(f"TRUNCATE {table}")
    conn.commit()
    conn.close()
    saved = _set_dsn(TEST_DSN)
    try:
        yield TEST_DSN
    finally:
        _restore_dsn(saved)


def _declare(runner, tmp_path, **overrides):
    args = [
        "declare",
        overrides.get("key", "cred:one"),
        "--account",
        "acct",
        "--locator",
        overrides.get("locator", f"file:{tmp_path / 'missing.json'}"),
        "--node",
        NODE,
    ]
    for flag, key in (
        ("--primary", "primary"),
        ("--tier", "tier"),
        ("--obtain-command", "obtain"),
    ):
        if key in overrides:
            args += [flag, overrides[key]]
    return runner.invoke(creds, args)


def test_an_unreachable_store_is_not_reported_as_clean(dead_store):
    # Arrange
    runner = CliRunner()
    # Act
    result = runner.invoke(creds, ["status", "--node", NODE])
    # Assert
    assert "nothing was checked" in result.output.lower()


def test_an_unreachable_store_exits_non_zero(dead_store):
    # Arrange
    runner = CliRunner()
    # Act
    result = runner.invoke(creds, ["status", "--node", NODE])
    # Assert
    assert result.exit_code != 0


def test_an_unconfigured_store_refuses_to_guess_a_target(unconfigured_store):
    # Arrange — servers must not guess (ADR-0022 §4).
    runner = CliRunner()
    # Act
    result = runner.invoke(creds, ["status", "--node", NODE])
    # Assert
    assert "refusing to guess" in result.output.lower()


def test_declaring_a_credential_reports_the_primary(store_env, tmp_path):
    # Arrange
    runner = CliRunner()
    # Act
    result = _declare(runner, tmp_path, primary="other-node")
    # Assert
    assert "primary: other-node" in result.output


def test_a_node_with_no_declarations_is_not_reported_as_healthy(store_env):
    # Arrange — no rows means nothing was recorded, not that nothing is
    # missing.
    runner = CliRunner()
    # Act
    result = runner.invoke(creds, ["status", "--node", "node-with-nothing"])
    # Assert
    assert "never been recorded" in result.output


def test_a_missing_required_credential_is_reported_absent(store_env, tmp_path):
    # Arrange
    runner = CliRunner()
    _declare(runner, tmp_path, primary="other-node", tier="distributable")
    # Act
    result = runner.invoke(creds, ["status", "--node", NODE])
    # Assert
    assert "ABSENT" in result.output


def test_a_missing_credential_names_where_it_comes_from(store_env, tmp_path):
    # Arrange — the materialize answer, not just the complaint.
    runner = CliRunner()
    _declare(
        runner,
        tmp_path,
        primary="other-node",
        tier="distributable",
        obtain="sac accounts keepalive --to here",
    )
    # Act
    result = runner.invoke(creds, ["status", "--node", NODE])
    # Assert
    assert "sac accounts keepalive --to here" in result.output


def test_a_primary_secret_is_never_offered_for_copying(store_env, tmp_path):
    # Arrange — the two-tier model holding, expressed as a refusal.
    runner = CliRunner()
    _declare(runner, tmp_path, primary="other-node", tier="primary_secret")
    # Act
    result = runner.invoke(creds, ["status", "--node", NODE])
    # Assert
    assert "do NOT copy" in result.output


def test_a_fault_exits_non_zero(store_env, tmp_path):
    # Arrange — the "loud" half of the deliverable.
    runner = CliRunner()
    _declare(runner, tmp_path, primary="other-node", tier="distributable")
    # Act
    result = runner.invoke(creds, ["status", "--node", NODE])
    # Assert
    assert result.exit_code == 1


def test_status_emits_machine_readable_json_on_request(store_env, tmp_path):
    # Arrange
    runner = CliRunner()
    _declare(runner, tmp_path, primary="other-node", tier="distributable")
    # Act
    result = runner.invoke(creds, ["status", "--node", NODE, "--json"])
    # Assert
    assert json.loads(result.output)["severity"] == "fault"


def test_a_present_world_readable_credential_is_flagged(store_env, tmp_path):
    # Arrange — a real file at a real permissive mode.
    artifact = tmp_path / "creds.json"
    artifact.write_text(json.dumps({"accessToken": "FAKE0000"}), encoding="utf-8")
    artifact.chmod(0o644)
    runner = CliRunner()
    _declare(
        runner,
        tmp_path,
        key="cred:mode",
        locator=f"file:{artifact}",
        tier="distributable",
    )
    # Act
    result = runner.invoke(creds, ["status", "--node", NODE])
    # Assert
    assert "WORLD_READABLE" in result.output


def test_check_reports_a_credential_no_node_can_renew(store_env, tmp_path):
    # Arrange
    runner = CliRunner()
    _declare(runner, tmp_path, primary="other-node", tier="distributable")
    # Act
    result = runner.invoke(creds, ["check"])
    # Assert
    assert "NO node holds refresh material" in result.output


def test_check_exits_non_zero_when_an_invariant_is_broken(store_env, tmp_path):
    # Arrange
    runner = CliRunner()
    _declare(runner, tmp_path, primary="other-node", tier="distributable")
    # Act
    result = runner.invoke(creds, ["check"])
    # Assert
    assert result.exit_code == 1


def test_declaring_material_in_a_command_field_is_refused(store_env, tmp_path):
    # Arrange — the guard sits on the CLI path too.
    runner = CliRunner()
    # Act
    result = _declare(runner, tmp_path, key="cred:bad", obtain="echo sk-ant-" + "A" * 40)
    # Assert
    assert result.exit_code != 0
