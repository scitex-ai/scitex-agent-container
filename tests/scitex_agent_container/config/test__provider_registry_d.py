"""Tests for ``config._provider_registry_d.load_merged_registry``.

Operator-extensible provider overlay: ``~/.scitex/agent-container/
providers.d/*.yaml`` files merge over the built-in PROVIDERS at
config-load time. Each test pins one observable fact (TQ007), AAA
markers (TQ002), descriptive name with >=3 tokens (TQ003).

Real seams (no mocks):
* ``tmp_path`` is passed via the ``providers_d_dir`` arg so no global
  state leaks between tests.
* A ``io.StringIO`` captures the loader's stderr override / dup notices.
"""

from __future__ import annotations

import io

import pytest

from scitex_agent_container.config._provider_registry_d import (
    ProviderRegistryDError,
    load_merged_registry,
)


def _write(path, text):
    path.write_text(text)


_QWEN_YAML = """
name: qwen-spartan
label: Qwen vLLM (Spartan)
endpoint:
  tunnel:
    jump_host: spartan-login
    target_host: spartan-gpgpu171
    remote_port: 4000
default_model: qwen36-35b-a3b
auth_token_env: CLEW_VLLM_TOKEN
"""

_BASE = {
    "deepseek": {
        "label": "DeepSeek",
        "endpoint": {"base_url": "https://api.deepseek.com/anthropic"},
        "default_model": "deepseek-chat",
        "auth_token_env": "DEEPSEEK_API_KEY",
    },
}


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


def test_missing_directory_returns_base_registry_unchanged(tmp_path):
    # Arrange — no providers.d/ dir exists.
    nonexistent = tmp_path / "no-such-dir"
    # Act
    merged = load_merged_registry(nonexistent, base_registry=dict(_BASE))
    # Assert
    assert merged == _BASE


def test_overlay_adds_new_provider_name(tmp_path):
    # Arrange
    _write(tmp_path / "qwen-spartan.yaml", _QWEN_YAML)
    # Act
    merged = load_merged_registry(tmp_path, base_registry=dict(_BASE))
    # Assert
    assert "qwen-spartan" in merged


def test_overlay_entry_carries_label_and_default_model(tmp_path):
    # Arrange
    _write(tmp_path / "qwen-spartan.yaml", _QWEN_YAML)
    # Act
    merged = load_merged_registry(tmp_path, base_registry=dict(_BASE))
    # Assert
    assert merged["qwen-spartan"]["label"] == "Qwen vLLM (Spartan)"
    assert merged["qwen-spartan"]["default_model"] == "qwen36-35b-a3b"


def test_overlay_tunnel_endpoint_preserves_jump_host(tmp_path):
    # Arrange
    _write(tmp_path / "qwen-spartan.yaml", _QWEN_YAML)
    # Act
    merged = load_merged_registry(tmp_path, base_registry=dict(_BASE))
    # Assert
    assert merged["qwen-spartan"]["endpoint"]["tunnel"]["jump_host"] == "spartan-login"


def test_built_in_entries_remain_after_overlay_merge(tmp_path):
    # Arrange
    _write(tmp_path / "qwen-spartan.yaml", _QWEN_YAML)
    # Act
    merged = load_merged_registry(tmp_path, base_registry=dict(_BASE))
    # Assert
    assert merged["deepseek"]["label"] == "DeepSeek"


# ---------------------------------------------------------------------------
# Conflict handling
# ---------------------------------------------------------------------------


def test_overlay_overriding_built_in_emits_stderr_notice(tmp_path):
    # Arrange — overlay reuses a built-in name; operator should see the
    # one-line NOTICE on stderr so an unintended shadow is visible.
    _write(
        tmp_path / "deepseek.yaml",
        "name: deepseek\nlabel: Custom DS\nendpoint: {base_url: https://custom}\n"
        "default_model: custom-model\nauth_token_env: CUSTOM_KEY\n",
    )
    log = io.StringIO()
    # Act
    merged = load_merged_registry(tmp_path, base_registry=dict(_BASE), log_stream=log)
    # Assert
    assert "overrides built-in provider 'deepseek'" in log.getvalue()
    assert merged["deepseek"]["label"] == "Custom DS"


def test_two_overlay_files_with_same_name_emit_dup_warning(tmp_path):
    # Arrange — second file wins; warning names BOTH files.
    _write(tmp_path / "a.yaml", _QWEN_YAML)
    _write(
        tmp_path / "z.yaml",
        "name: qwen-spartan\nlabel: Other\n"
        "endpoint: {base_url: https://other}\n"
        "default_model: other-model\nauth_token_env: OTHER\n",
    )
    log = io.StringIO()
    # Act
    merged = load_merged_registry(tmp_path, base_registry=dict(_BASE), log_stream=log)
    # Assert
    log_text = log.getvalue()
    assert "a.yaml" in log_text
    assert "z.yaml" in log_text
    assert merged["qwen-spartan"]["label"] == "Other"


# ---------------------------------------------------------------------------
# Fail-loud paths
# ---------------------------------------------------------------------------


def test_malformed_yaml_raises_with_file_path(tmp_path):
    # Arrange
    _write(tmp_path / "bad.yaml", ":\n:invalid\n")
    # Act
    ctx = pytest.raises(ProviderRegistryDError, match="bad.yaml")
    # Assert
    with ctx:
        load_merged_registry(tmp_path, base_registry=dict(_BASE))


def test_missing_name_key_raises_loudly(tmp_path):
    # Arrange
    _write(
        tmp_path / "no-name.yaml",
        "label: x\nendpoint: {base_url: y}\ndefault_model: m\nauth_token_env: K\n",
    )
    # Act
    ctx = pytest.raises(ProviderRegistryDError, match="missing required key")
    # Assert
    with ctx:
        load_merged_registry(tmp_path, base_registry=dict(_BASE))


def test_endpoint_with_both_base_url_and_tunnel_raises_loudly(tmp_path):
    # Arrange — XOR violation in the overlay file.
    _write(
        tmp_path / "bad-endpoint.yaml",
        """
name: bad
label: bad
endpoint:
  base_url: https://x
  tunnel:
    jump_host: j
    target_host: t
    remote_port: 4000
default_model: m
auth_token_env: K
""",
    )
    # Act
    ctx = pytest.raises(ProviderRegistryDError, match="exactly ONE")
    # Assert
    with ctx:
        load_merged_registry(tmp_path, base_registry=dict(_BASE))


def test_tunnel_missing_jump_host_raises_loudly(tmp_path):
    # Arrange
    _write(
        tmp_path / "bad-tunnel.yaml",
        """
name: bad
label: bad
endpoint:
  tunnel:
    target_host: t
    remote_port: 4000
default_model: m
auth_token_env: K
""",
    )
    # Act
    ctx = pytest.raises(ProviderRegistryDError, match="jump_host")
    # Assert
    with ctx:
        load_merged_registry(tmp_path, base_registry=dict(_BASE))


def test_env_var_overrides_default_overlay_dir(tmp_path, monkeypatch):
    # Arrange — the operator can pin the overlay dir via env var when
    # the per-user default is wrong (shared host, alternate XDG path).
    _write(tmp_path / "qwen-spartan.yaml", _QWEN_YAML)
    monkeypatch.setenv("SAC_PROVIDERS_D_DIR", str(tmp_path))
    # Act
    merged = load_merged_registry(base_registry=dict(_BASE))
    # Assert
    assert "qwen-spartan" in merged
