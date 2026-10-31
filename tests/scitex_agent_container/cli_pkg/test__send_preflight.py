"""Selected local auth uses real synthetic v3 specs and expiry files."""

from __future__ import annotations

import json
import time
from contextlib import contextmanager
from pathlib import Path

import pytest
import yaml

from scitex_agent_container._mcp._tools._agent import agent_send
from scitex_agent_container.cli_pkg._send_preflight import preflight_send_creds
from tests.scitex_agent_container.config.test__explicit_validation import _explicit_doc
from tests.scitex_agent_container.config.test__explicit_validation_harness import (
    _selected_doc,
)


def target_spec(tmp_path, env_save_restore, doc):
    directory = tmp_path / "agents" / "owned"
    directory.mkdir(parents=True)
    doc["spec"]["host"] = "synthetic-host"
    path = directory / "spec.yaml"
    path.write_text(yaml.safe_dump(doc))
    env_save_restore.set("SCITEX_AGENT_CONTAINER_YAML_DIRS", str(directory.parent))
    return path


def expiry_file(path: Path, expiry: int):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"claudeAiOauth": {"expiresAt": expiry}}))
    return path


def guard(**kwargs):
    return preflight_send_creds(
        "owned",
        peer_host="synthetic-host",
        current_host="synthetic-host",
        now=2000,
        **kwargs,
    )


@pytest.mark.parametrize("harness", ("hermes", "codex", "opencode", "openai-agents"))
def test_selected_non_claude_does_not_read_expired_lead(
    tmp_path, env_save_restore, harness
):
    # Arrange
    target_spec(tmp_path, env_save_restore, _selected_doc(harness))
    expired = expiry_file(tmp_path / "unrelated-lead.json", 1000)
    # Act
    result = guard(lead_creds_path=expired)
    # Assert
    assert result is None


def test_missing_selected_spec_is_a_source_refusal(tmp_path, env_save_restore):
    # Arrange
    target_spec(tmp_path, env_save_restore, _selected_doc("hermes")).unlink()
    # Act
    result = guard()
    # Assert
    assert result["status"] == "error" and result["error"].startswith(
        "selected target spec:"
    )


def test_unknown_selected_spec_is_a_source_refusal(tmp_path, env_save_restore):
    # Arrange
    doc = _selected_doc("hermes")
    doc["spec"]["harness"] = "hermes-typo"
    target_spec(tmp_path, env_save_restore, doc)
    # Act
    result = guard()
    # Assert
    assert result["status"] == "error" and result["error"].startswith(
        "selected target spec:"
    )


def test_conflict_selected_spec_is_a_source_refusal(tmp_path, env_save_restore):
    # Arrange
    doc = _selected_doc("hermes")
    doc["spec"]["provider"] = "anthropic"
    target_spec(tmp_path, env_save_restore, doc)
    # Act
    result = guard()
    # Assert
    assert result["status"] == "error" and result["error"].startswith(
        "selected target spec:"
    )


def test_declared_claude_credential_is_checked_instead_of_lead(
    tmp_path, env_save_restore
):
    # Arrange
    selected = expiry_file(tmp_path / "selected.json", 5000)
    unrelated = expiry_file(tmp_path / "unrelated.json", 1000)
    doc = _explicit_doc()
    doc["spec"]["claude"]["credentials_file"] = str(selected)
    target_spec(tmp_path, env_save_restore, doc)
    # Act
    result = guard(lead_creds_path=unrelated)
    # Assert
    assert result is None


def test_expired_selected_claude_stays_refused(tmp_path, env_save_restore):
    # Arrange
    selected = expiry_file(tmp_path / "selected.json", 1000)
    doc = _explicit_doc()
    doc["spec"]["claude"]["credentials_file"] = str(selected)
    target_spec(tmp_path, env_save_restore, doc)
    # Act
    result = guard()
    # Assert
    assert result["status"] == "creds-expired" and "selected.json" in result["error"]


def test_provider_selected_claude_does_not_read_oauth(tmp_path, env_save_restore):
    # Arrange
    doc = _explicit_doc()
    doc["spec"]["claude"]["provider"] = {
        "base_url": "https://synthetic.invalid/anthropic",
        "auth_token_env": "SYNTHETIC_PROVIDER_API_KEY",
    }
    doc["spec"]["claude"]["model"] = "synthetic-model"
    target_spec(tmp_path, env_save_restore, doc)
    # Act
    result = guard()
    # Assert
    assert result is None


def test_local_endpoint_refuses_a_conflicting_declared_host(tmp_path, env_save_restore):
    # Arrange
    path = target_spec(tmp_path, env_save_restore, _selected_doc("hermes"))
    doc = yaml.safe_load(path.read_text())
    doc["spec"]["host"] = "foreign-peer"
    path.write_text(yaml.safe_dump(doc))
    # Act
    result = guard()
    # Assert
    assert result["status"] == "error" and "host conflicts" in result["error"]


def test_foreign_transport_never_probes_guessed_claude_files(tmp_path):
    # Arrange
    expired = expiry_file(tmp_path / "unrelated-lead.json", 1000)

    def forbidden_probe(*_args):
        raise AssertionError("foreign guessed Claude probe must not run")

    # Act
    result = preflight_send_creds(
        "owned",
        peer_host="foreign-peer",
        current_host="synthetic-host",
        lead_creds_path=expired,
        ssh_runner=forbidden_probe,
    )
    # Assert
    assert result is None


@contextmanager
def local_mcp_boundary(env_save_restore, *, peer="synthetic-host"):
    """Synthetic endpoint metadata, real wrapper/spec/auth, no live registry."""
    from scitex_agent_container.cli_pkg import _send, _send_resolve

    calls = []
    env_save_restore.set("SAC_HOST", "synthetic-host")
    env_save_restore.delete("SCITEX_AGENT_CONTAINER_HOST")
    env_save_restore.delete("SAC_LISTEN_BASE_URL")
    env_save_restore.delete("SCITEX_AGENT_CONTAINER_LISTEN_BASE_URL")
    env_save_restore.delete("APPTAINER_CONTAINER")
    env_save_restore.delete("SINGULARITY_CONTAINER")
    saved_resolve, saved_post = _send_resolve.resolve_send_endpoint, _send._post_turn

    def endpoint(_name, *, current_host):
        return _send_resolve.ResolvedEndpoint(
            19001, peer, "instance_row", {"host": peer}
        )

    def runner(url, text, *, timeout_s):
        calls.append((url, text))
        return "synthetic runner response", {}

    _send_resolve.resolve_send_endpoint, _send._post_turn = endpoint, runner
    try:
        yield calls
    finally:
        _send_resolve.resolve_send_endpoint, _send._post_turn = (
            saved_resolve,
            saved_post,
        )


@pytest.mark.parametrize("harness", ("hermes", "codex", "opencode", "openai-agents"))
def test_mcp_selected_non_claude_dispatches_without_lead_oauth(
    tmp_path, env_save_restore, harness
):
    # Arrange
    target_spec(tmp_path, env_save_restore, _selected_doc(harness))
    with local_mcp_boundary(env_save_restore) as calls:
        # Act
        result = agent_send("owned", prompt="synthetic work", wait=True)
    # Assert
    assert (result["status"], calls) == (
        "ok",
        [("http://127.0.0.1:19001/v1/turn", "synthetic work")],
    )


def test_mcp_missing_selected_target_never_dispatches(tmp_path, env_save_restore):
    # Arrange
    target_spec(tmp_path, env_save_restore, _selected_doc("hermes")).unlink()
    with local_mcp_boundary(env_save_restore) as calls:
        # Act
        result = agent_send("owned", prompt="synthetic work", wait=True)
    # Assert
    assert (result["status"], calls) == ("error", [])


def test_mcp_unknown_selected_target_never_dispatches(tmp_path, env_save_restore):
    # Arrange
    doc = _selected_doc("hermes")
    doc["spec"]["harness"] = "hermes-typo"
    target_spec(tmp_path, env_save_restore, doc)
    with local_mcp_boundary(env_save_restore) as calls:
        # Act
        result = agent_send("owned", prompt="synthetic work", wait=True)
    # Assert
    assert (result["status"], calls) == ("error", [])


def test_mcp_conflict_selected_target_never_dispatches(tmp_path, env_save_restore):
    # Arrange
    doc = _selected_doc("hermes")
    doc["spec"]["provider"] = "anthropic"
    target_spec(tmp_path, env_save_restore, doc)
    with local_mcp_boundary(env_save_restore) as calls:
        # Act
        result = agent_send("owned", prompt="synthetic work", wait=True)
    # Assert
    assert (result["status"], calls) == ("error", [])


def test_mcp_foreign_host_selected_target_never_dispatches(tmp_path, env_save_restore):
    # Arrange
    doc = _selected_doc("hermes")
    path = target_spec(tmp_path, env_save_restore, doc)
    doc["spec"]["host"] = "foreign-peer"
    path.write_text(yaml.safe_dump(doc))
    with local_mcp_boundary(env_save_restore) as calls:
        # Act
        result = agent_send("owned", prompt="synthetic work", wait=True)
    # Assert
    assert (result["status"], calls) == ("error", [])


@pytest.mark.parametrize("valid", (True, False))
def test_mcp_claude_requires_its_selected_credential(tmp_path, env_save_restore, valid):
    # Arrange
    selected = expiry_file(
        tmp_path / "selected.json", int(time.time()) + 3600 if valid else 1
    )
    doc = _explicit_doc()
    doc["spec"]["claude"]["credentials_file"] = str(selected)
    target_spec(tmp_path, env_save_restore, doc)
    with local_mcp_boundary(env_save_restore) as calls:
        # Act
        result = agent_send("owned", prompt="synthetic work", wait=True)
    # Assert
    assert (result["status"], len(calls)) == (
        ("ok", 1) if valid else ("creds-expired", 0)
    )


def test_mcp_provider_claude_keeps_provider_auth_authority(tmp_path, env_save_restore):
    # Arrange
    doc = _explicit_doc()
    doc["spec"]["claude"]["provider"] = {
        "base_url": "https://synthetic.invalid/anthropic",
        "auth_token_env": "SYNTHETIC_PROVIDER_API_KEY",
    }
    doc["spec"]["claude"]["model"] = "synthetic-model"
    target_spec(tmp_path, env_save_restore, doc)
    with local_mcp_boundary(env_save_restore) as calls:
        # Act
        result = agent_send("owned", prompt="synthetic work", wait=True)
    # Assert
    assert (result["status"], len(calls)) == ("ok", 1)


def test_mcp_foreign_runner_keeps_existing_ssh_authority_without_local_spec(
    env_save_restore,
):
    # Arrange
    with local_mcp_boundary(env_save_restore, peer="foreign-peer") as calls:
        # Act
        agent_send("owned", prompt="synthetic work", wait=True)
    # Assert
    assert calls == [("ssh://foreign-peer:19001/v1/turn", "synthetic work")]
