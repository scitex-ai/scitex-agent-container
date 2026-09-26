"""Per-agent ``PGUSER`` injection — derived when absent, never clobbering.

Mirrors ``src/scitex_agent_container/runtimes/_pg_identity_env.py``.

The load-bearing property is the same precedence rule the module states: a
spec that declares ``PGUSER`` anywhere (``spec.env`` or ``raw_args``) wins,
and an agent that declares nothing still launches with the derived project
role (agent name for project-less legacy configs) — because after dotfiles PR
#391 the DSNs carry no userinfo, so an agent with neither would authenticate as
the NOLOGIN umbrella role and fail at start.

Real dicts, real ``getpass.getuser()``, ``SimpleNamespace`` configs like the
neighbouring ``test__fleet_env.py`` — no mocks (PA-306). One assert per test
(PA-307).
"""

from __future__ import annotations

import getpass
from pathlib import Path
from types import SimpleNamespace

import pytest

from scitex_agent_container.config import load_config
from scitex_agent_container.runtimes._fleet_env import effective_env
from scitex_agent_container.runtimes._pg_identity_env import (
    PG_USER_ENV,
    PgIdentityCredentialError,
    apply_pg_identity,
    derive_pg_role,
    pgpass_has_role,
    require_pgpass_role,
)

# ----------------------------------------------------------------------
# Derivation.
# ----------------------------------------------------------------------


def test_derive_pg_role_joins_host_user_and_agent_name() -> None:
    # Arrange
    agent_name = "scitex-io"
    # Act
    role = derive_pg_role(agent_name, host_user="ywatanabe")
    # Assert
    assert role == "ywatanabe__scitex-io"


def test_derive_pg_role_defaults_to_the_invoking_os_user() -> None:
    # Arrange
    agent_name = "scitex-io"
    # Act
    role = derive_pg_role(agent_name)
    # Assert
    assert role == f"{getpass.getuser()}__scitex-io"


def test_derive_pg_role_uses_project_identity_for_variant_agent() -> None:
    # Arrange
    agent_name = "scitex-agent-container-gui"
    # Act
    role = derive_pg_role(
        agent_name,
        project_name="scitex-agent-container",
        host_user="ywatanabe",
    )
    # Assert
    assert role == "ywatanabe__scitex-agent-container"


def test_project_identity_stays_exact_when_worktree_distribution_differs(
    tmp_path,
) -> None:
    # Arrange
    # Project labels describe the logical project, not the worktree package.
    # Python distribution checked out in a task-specific worktree.
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "scitex-cards"\n', encoding="utf-8"
    )
    config = SimpleNamespace(
        env={},
        apptainer=None,
        name="scitex-cards-qwen-fix",
        labels={"project": "scitex-cards-qwen-fix"},
        workdir=str(tmp_path),
    )
    # Act
    out = effective_env(config, defaults={})
    # Assert
    assert out[PG_USER_ENV] == f"{getpass.getuser()}__scitex-cards-qwen-fix"


def test_a_declared_pguser_wins_over_project_derivation() -> None:
    # Arrange
    env = {
        "SCITEX_CARDS_DB": "postgresql://scitex-primary:55432/scitex",
        "PGPASSFILE": "/home/agent/.pgpass",
        "PGUSER": "svc_agent_container_gui",
    }
    # Act
    out = apply_pg_identity(
        env,
        agent_name="scitex-agent-container-gui",
        project_name="scitex-agent-container",
        host_user="ywatanabe",
    )
    # Assert
    assert out[PG_USER_ENV] == "svc_agent_container_gui"


def test_without_a_declared_pguser_the_project_role_is_derived() -> None:
    # Arrange
    env = {
        "SCITEX_CARDS_DB": "postgresql://scitex-primary:55432/scitex",
        "PGPASSFILE": "/home/agent/.pgpass",
    }
    # Act
    out = apply_pg_identity(
        env,
        agent_name="scitex-agent-container-gui",
        project_name="scitex-agent-container",
        host_user="ywatanabe",
    )
    # Assert
    assert out[PG_USER_ENV] == "ywatanabe__scitex-agent-container"


def test_pgpass_role_validation_ignores_password_and_handles_escapes(tmp_path) -> None:
    # Arrange
    passfile = tmp_path / ".pgpass"
    passfile.write_text(
        "# comment\n*:*:*:other:secret\n"
        "scitex-primary:55432:scitex:owner__project:pa\\:ss\n",
        encoding="utf-8",
    )
    passfile.chmod(0o600)
    # Act
    result = pgpass_has_role(passfile, "owner__project")
    # Assert
    assert result is True


def test_pgpass_role_validation_refuses_unprovisioned_identity(tmp_path) -> None:
    # Arrange
    passfile = tmp_path / ".pgpass"
    passfile.write_text("*:*:*:owner__project:secret\n", encoding="utf-8")
    passfile.chmod(0o600)
    # Act
    ctx = pytest.raises(PgIdentityCredentialError, match="owner__project-gui")
    # Assert
    with ctx:
        require_pgpass_role(passfile, "owner__project-gui")


def test_pgpass_role_validation_requires_the_declared_store_target(tmp_path) -> None:
    # Arrange
    passfile = tmp_path / ".pgpass"
    passfile.write_text(
        "other-primary:55432:scitex:owner__project:secret\n", encoding="utf-8"
    )
    passfile.chmod(0o600)
    # Act
    result = pgpass_has_role(
        passfile,
        "owner__project",
        dsns=("postgresql://scitex-primary:55432/scitex",),
    )
    # Assert
    assert result is False


def test_pgpass_role_validation_rejects_world_readable_source(tmp_path) -> None:
    # Arrange
    passfile = tmp_path / ".pgpass"
    passfile.write_text("*:*:*:owner__project:secret\n", encoding="utf-8")
    passfile.chmod(0o644)
    # Act
    result = pgpass_has_role(passfile, "owner__project")
    # Assert
    assert result is False


def test_pgpass_role_validation_rejects_symlink_source(tmp_path) -> None:
    # Arrange
    target = tmp_path / "private.pgpass"
    target.write_text("*:*:*:owner__project:secret\n", encoding="utf-8")
    target.chmod(0o600)
    passfile = tmp_path / ".pgpass"
    passfile.symlink_to(target)
    # Act
    result = pgpass_has_role(passfile, "owner__project")
    # Assert
    assert result is False


def test_pgpass_role_validation_rejects_dsn_userinfo_override(tmp_path) -> None:
    # Arrange
    passfile = tmp_path / ".pgpass"
    passfile.write_text("*:*:*:owner__project:secret\n", encoding="utf-8")
    passfile.chmod(0o600)
    # Act
    result = pgpass_has_role(
        passfile,
        "owner__project",
        dsns=("postgresql://different_role@scitex-primary:55432/scitex",),
    )
    # Assert
    assert result is False


# ----------------------------------------------------------------------
# Injection and precedence.
# ----------------------------------------------------------------------


def test_injects_derived_pguser_when_nothing_declares_one() -> None:
    # Arrange
    env: dict[str, str] = {"OTHER": "x"}
    # Act
    out = apply_pg_identity(env, agent_name="scitex-io", host_user="ywatanabe")
    # Assert
    assert out[PG_USER_ENV] == "ywatanabe__scitex-io"


def test_spec_env_pguser_wins_over_derivation() -> None:
    # Arrange
    env = {PG_USER_ENV: "svc_notifyd"}
    # Act
    out = apply_pg_identity(env, agent_name="scitex-io", host_user="ywatanabe")
    # Assert
    assert out[PG_USER_ENV] == "svc_notifyd"


def test_raw_args_pguser_suppresses_injection() -> None:
    # Arrange
    # Apptainer --env is last-wins, so an injected value would be
    # overridden in the argv while this function believed it decided.
    raw_args = ["--env", f"{PG_USER_ENV}=svc_gui"]
    # Act
    out = apply_pg_identity({}, raw_args=raw_args, agent_name="scitex-io")
    # Assert
    assert PG_USER_ENV not in out


def test_no_agent_name_means_no_injection() -> None:
    # Arrange
    env: dict[str, str] = {}
    # Act
    out = apply_pg_identity(env, agent_name=None)
    # Assert
    assert PG_USER_ENV not in out


def test_input_mapping_is_not_mutated() -> None:
    # Arrange
    env: dict[str, str] = {}
    # Act
    apply_pg_identity(env, agent_name="scitex-io")
    # Assert
    assert env == {}


# ----------------------------------------------------------------------
# End-to-end through effective_env (the entry point argv rendering uses).
# ----------------------------------------------------------------------


def test_effective_env_carries_derived_pguser() -> None:
    # Arrange
    config = SimpleNamespace(env={}, apptainer=None, name="scitex-io")
    # Act
    out = effective_env(config, defaults={})
    # Assert
    assert out[PG_USER_ENV] == f"{getpass.getuser()}__scitex-io"


def test_non_hermes_effective_env_does_not_claim_a_generated_passfile() -> None:
    # Arrange
    config = SimpleNamespace(env={}, apptainer=None, name="anthropic-worker")
    # Act
    out = effective_env(
        config,
        defaults={"SCITEX_STORE_DSN": "postgresql://primary:55432/scitex"},
    )
    # Assert
    assert "PGPASSFILE" not in out


def test_effective_env_carries_project_pguser_for_variant_agent() -> None:
    # Arrange
    config = SimpleNamespace(
        env={},
        apptainer=None,
        name="scitex-hub-deepseek",
        labels={"project": "scitex-hub"},
    )
    # Act
    out = effective_env(config, defaults={})
    # Assert
    assert out[PG_USER_ENV] == f"{getpass.getuser()}__scitex-hub"


def test_current_v3_spec_project_label_selects_project_pguser() -> None:
    # Arrange
    # Load the tracked current-syntax fixture; its directory-derived
    # agent name is deliberately not its declared project identity.
    fixture = (
        Path(__file__).parents[3]
        / "tests"
        / "integration"
        / "fixtures"
        / "engines_tracked_spec.yaml"
    )
    config = load_config(fixture)
    # Act
    out = effective_env(config, defaults={})
    # Assert
    assert out[PG_USER_ENV] == f"{getpass.getuser()}__business"


def test_effective_env_respects_spec_declared_pguser() -> None:
    # Arrange
    config = SimpleNamespace(
        env={PG_USER_ENV: "svc_cards_sync"}, apptainer=None, name="scitex-io"
    )
    # Act
    out = effective_env(config, defaults={})
    # Assert
    assert out[PG_USER_ENV] == "svc_cards_sync"
