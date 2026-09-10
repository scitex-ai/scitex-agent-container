"""Per-agent ``PGUSER`` injection — derived when absent, never clobbering.

Mirrors ``src/scitex_agent_container/runtimes/_pg_identity_env.py``.

The load-bearing property is the same precedence rule the module states: a
spec that declares ``PGUSER`` anywhere (``spec.env`` or ``raw_args``) wins,
and an agent that declares nothing still launches with the derived
``<host_user>__<name>`` — because after dotfiles PR #391 the DSNs carry no
userinfo, so an agent with neither would authenticate as the NOLOGIN umbrella
role and fail at start.

Real dicts, real ``getpass.getuser()``, ``SimpleNamespace`` configs like the
neighbouring ``test__fleet_env.py`` — no mocks (PA-306). One assert per test
(PA-307).
"""

from __future__ import annotations

import getpass
from types import SimpleNamespace

from scitex_agent_container.runtimes._fleet_env import effective_env
from scitex_agent_container.runtimes._pg_identity_env import (
    PG_USER_ENV,
    apply_pg_identity,
    derive_pg_role,
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


def test_a_declared_pguser_wins_over_the_unprovisioned_label_derivation() -> None:
    # Arrange — the scitex-agent-container-gui incident (2026-09-10): the DSN is
    # roleless, so without a declared PGUSER this agent would derive the role
    # <host_user>__<its label> = ywatanabe__scitex-agent-container-gui, which was
    # never provisioned (no cluster role / no .pgpass row) -> PgBouncer "FATAL:
    # bouncer config error" on every scitex-cards MCP list/comment. The fix is a
    # spec-declared PGUSER naming the PROVEN provisioned family role. This pins
    # that the declared role is honoured rather than re-derived from the label.
    env = {"SCITEX_CARDS_DB": "postgresql://scitex-primary:55432/scitex",
           "PGPASSFILE": "/home/agent/.pgpass",
           "PGUSER": "ywatanabe__scitex-agent-container"}
    # Act
    out = apply_pg_identity(env, agent_name="scitex-agent-container-gui", host_user="ywatanabe")
    # Assert — the provisioned family role survives; the label-derived role does not
    assert out[PG_USER_ENV] == "ywatanabe__scitex-agent-container"


def test_without_a_declared_pguser_the_label_role_is_derived() -> None:
    # Arrange — same agent, no declared PGUSER: the shared derivation still
    # applies (documents the failure path the declared role overrides).
    env = {"SCITEX_CARDS_DB": "postgresql://scitex-primary:55432/scitex",
           "PGPASSFILE": "/home/agent/.pgpass"}
    # Act
    out = apply_pg_identity(env, agent_name="scitex-agent-container-gui", host_user="ywatanabe")
    # Assert
    assert out[PG_USER_ENV] == "ywatanabe__scitex-agent-container-gui"


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
    # Arrange — apptainer --env is last-wins, so an injected value would be
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


def test_effective_env_respects_spec_declared_pguser() -> None:
    # Arrange
    config = SimpleNamespace(
        env={PG_USER_ENV: "svc_cards_sync"}, apptainer=None, name="scitex-io"
    )
    # Act
    out = effective_env(config, defaults={})
    # Assert
    assert out[PG_USER_ENV] == "svc_cards_sync"
