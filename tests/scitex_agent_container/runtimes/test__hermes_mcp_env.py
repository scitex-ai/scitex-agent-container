"""Hermes MCP env binding honours per-entry ``inherit_env``.

Reproduces live pilot 3 (2026-11-19, paper-scitex-clew): a sealed Hermes spec
whose ``spec.env`` carries ``SCITEX_STORE_DSN`` could not declare a store-free
MCP server. SAC baked the DSN into every stdio entry and then refused the
launch twice:

1. ``materialize_project_pgpass`` demanded a host ``.pgpass`` row for the
   project role ("has no complete credential for the declared store targets");
2. ``_validate_mcp_pg_credentials`` refused the entry once it declared its own
   ``PGPASSFILE`` ("roleless PostgreSQL DSN without both PGUSER and
   PGPASSFILE").

Every passfile here is a disposable tmp file; the runner's real ``~/.pgpass``
is never read (``PGPASSFILE`` is pinned to a tmp path for each test).
"""

from __future__ import annotations

import os
from contextlib import contextmanager
from pathlib import Path

import pytest
import yaml

from scitex_agent_container._sandbox import parse_sandbox
from scitex_agent_container.config import AgentConfig
from scitex_agent_container.config._provider_types import ProviderSpec
from scitex_agent_container.runtimes import _fleet_env
from scitex_agent_container.runtimes import _hermes_profile as profile
from scitex_agent_container.runtimes._operator_secret_env import (
    OPERATOR_SECRET_ENV_NAMES,
)
from scitex_agent_container.runtimes._pg_identity_env import (
    PgIdentityCredentialError,
)

_DSN = "postgresql://fixture.invalid:65432/fixture_db"
_REVIEW = {
    "command": "scitex-live-paper",
    "args": ["mcp", "start"],
    "alwaysLoad": True,
}


@pytest.fixture
def isolated_secrets(tmp_path):
    """Empty operator-secret pool + a row-less tmp PGPASSFILE (save/restore)."""
    pool = tmp_path / "empty-pool.src"
    pool.write_text("", encoding="utf-8")
    passfile = tmp_path / "host.pgpass"
    passfile.write_text("", encoding="utf-8")
    passfile.chmod(0o600)
    pinned = {"SAC_SECRETS_ENVRC": str(pool), "PGPASSFILE": str(passfile)}
    saved = {key: os.environ.get(key) for key in pinned}
    ambient = {
        n: os.environ.pop(n) for n in OPERATOR_SECRET_ENV_NAMES if n in os.environ
    }
    os.environ.update(pinned)
    try:
        yield passfile
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        os.environ.update(ambient)


@contextmanager
def _replace_attributes(replacements):
    originals = [(t, n, getattr(t, n)) for t, n, _ in replacements]
    try:
        for target, name, value in replacements:
            setattr(target, name, value)
        yield
    finally:
        for target, name, value in originals:
            setattr(target, name, value)


def _sealed_config(servers: dict) -> AgentConfig:
    config = AgentConfig(
        name="pscl-reader",
        harness="hermes",
        runtime="tui",
        workdir="/work",
        env={"SCITEX_STORE_DSN": _DSN, "PGUSER": "fixture__pscl-reader"},
        mcp_servers=servers,
    )
    config.engine_key = "qwen"
    config.model = "qwen-model"
    config.claude.provider = ProviderSpec(
        base_url="http://qwen.example:8000/v1", auth_token_env="QWEN_KEY"
    )
    config.sandbox = parse_sandbox(
        {
            "sandbox": {
                "egress": {"allow": ["qwen.example:8000"]},
                "env": {
                    "allow": ["SCITEX_STORE_DSN", "PGUSER"],
                    "model_key": "QWEN_KEY",
                },
                "run": {"root": "/scratch/runs", "size": "1G", "after": "remove"},
            }
        }
    )
    return config


def _materialize(config: AgentConfig, state_dir: Path) -> dict:
    replacements = [
        (_fleet_env, "declared_fleet_defaults", lambda: {}),
        (profile, "resolve_provider_api_key", lambda value: "engine-secret"),
        (profile, "deploy_to_home", lambda value, target: None),
        (profile, "deploy_to_home_overlay", lambda value: None),
        (profile, "resolve_overlay_upper_home", lambda value: None),
        (profile, "_verified_instruction_text", lambda config, targets: "rules"),
    ]
    with _replace_attributes(replacements):
        targets = profile.materialize_hermes_tui_profile(config, state_dir=state_dir)
    path = targets[0] / ".hermes" / "config.yaml"
    return yaml.safe_load(path.read_text(encoding="utf-8"))["mcp_servers"]


def test_store_free_entry_needs_no_host_pgpass_row(tmp_path, isolated_secrets):
    """Pilot refusal 1 (materialize_project_pgpass) must not fire."""
    # Arrange
    config = _sealed_config({"review": {**_REVIEW, "inherit_env": False}})
    # Act
    servers = _materialize(config, tmp_path / "state")
    # Assert
    assert "review" in servers


def test_store_free_entry_receives_only_declared_env(tmp_path, isolated_secrets):
    # Arrange
    entry = {**_REVIEW, "inherit_env": False, "env": {"REPORT": "/work/r.json"}}
    config = _sealed_config({"review": entry})
    # Act
    servers = _materialize(config, tmp_path / "state")
    # Assert
    assert servers["review"]["env"] == {"REPORT": "/work/r.json"}


def test_store_free_entry_with_own_passfile_passes_validation(
    tmp_path, isolated_secrets
):
    """Pilot refusal 2 (_validate_mcp_pg_credentials, roleless DSN)."""
    # Arrange
    entry = {**_REVIEW, "inherit_env": False, "env": {"PGPASSFILE": "/dev/null"}}
    config = _sealed_config({"review": entry})
    config.env.pop("PGUSER")
    servers = _materialize(config, tmp_path / "state")
    # Act
    result = profile._validate_mcp_pg_credentials(
        servers, launch_argv=("apptainer", "exec")
    )
    # Assert
    assert result is None


def test_inheriting_entry_still_demands_a_pgpass_row(tmp_path, isolated_secrets):
    # Arrange
    config = _sealed_config({"store-user": dict(_REVIEW)})
    # Act
    ctx = pytest.raises(PgIdentityCredentialError, match="no complete credential")
    # Assert
    with ctx:
        _materialize(config, tmp_path / "state")


def test_allowlisted_entry_receives_only_listed_spec_env(tmp_path, isolated_secrets):
    # Arrange
    entry = {**_REVIEW, "inherit_env": ["PGUSER"]}
    config = _sealed_config({"review": entry})
    # Act
    servers = _materialize(config, tmp_path / "state")
    # Assert
    assert servers["review"]["env"] == {"PGUSER": "fixture__pscl-reader"}


def test_inherit_env_key_never_reaches_hermes_config(tmp_path, isolated_secrets):
    # Arrange
    config = _sealed_config({"review": {**_REVIEW, "inherit_env": False}})
    # Act
    servers = _materialize(config, tmp_path / "state")
    # Assert
    assert "inherit_env" not in servers["review"]


#: sha256 of ``yaml.safe_dump(mcp_servers, sort_keys=False)`` that
#: origin/develop (before ``inherit_env``) rendered for ``_unsealed_fixture``.
_DEVELOP_MCP_SHA256 = "e136fa4e045941cf0a6a4790b027def82f944660269bd8ebcb40296babae15a6"


def _unsealed_fixture() -> AgentConfig:
    config = _sealed_config(
        {
            "scitex-cards": {
                "command": "scitex-cards",
                "args": ["mcp", "start"],
                "alwaysLoad": True,
            },
            "browser": {"command": "browser-mcp", "env": {"X": "1"}, "alwaysLoad": True},
            "remote": {"type": "http", "url": "http://example.invalid/mcp"},
        }
    )
    config.sandbox = None
    config.name = "fixture-agent"
    config.env = {"SCITEX_STORE_DSN": _DSN, "PGUSER": "fixture__x", "FOO": "bar"}
    return config


def test_default_hermes_mcp_servers_are_byte_identical_to_develop(
    tmp_path, isolated_secrets
):
    # Arrange
    import hashlib

    isolated_secrets.write_text(
        "fixture.invalid:65432:fixture_db:fixture__x:pw\n", encoding="utf-8"
    )
    config = _unsealed_fixture()
    # Act
    servers = _materialize(config, tmp_path / "state")
    # Assert
    dumped = yaml.safe_dump(servers, sort_keys=False).encode()
    assert hashlib.sha256(dumped).hexdigest() == _DEVELOP_MCP_SHA256
