"""Tests for ``sac dev`` group — extract-apikey + GitHub secret upload."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from click.testing import CliRunner

from scitex_agent_container.cli_pkg import dev_group as dg
from scitex_agent_container.cli_pkg.dev_group import dev_group


@pytest.fixture(autouse=True)
def sandbox_home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    # Override the module-level _CREDENTIALS_PATH (was bound at import time).
    monkeypatch.setattr(dg, "_CREDENTIALS_PATH", home / ".claude" / ".credentials.json")
    return home


@pytest.fixture
def patch_scitex_git(monkeypatch):
    """Mark scitex_git as available with mock implementations."""
    monkeypatch.setattr(dg, "_SCITEX_GIT_OK", True)
    monkeypatch.setattr(dg, "format_age", lambda ts: "1 hour", raising=False)
    monkeypatch.setattr(dg, "get_variable", MagicMock(return_value=None), raising=False)
    monkeypatch.setattr(dg, "list_secrets", MagicMock(return_value={}), raising=False)
    monkeypatch.setattr(dg, "set_secret_with_sha_sidecar", MagicMock(), raising=False)
    monkeypatch.setattr(dg, "sha256_hex", lambda s: "deadbeef" * 8, raising=False)


def _write_creds(path: Path, token="sk-ant-oat-xyz"):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"claudeAiOauth": {"accessToken": token}}))


# ---------------------------------------------------------------------------
# extract-apikey-from-credentials
# ---------------------------------------------------------------------------


def test_extract_apikey_bare(sandbox_home):
    creds = sandbox_home / ".claude" / ".credentials.json"
    _write_creds(creds, "sk-ant-oat-token-123")
    runner = CliRunner()
    result = runner.invoke(dev_group, ["extract-apikey-from-credentials"])
    assert result.exit_code == 0, result.output
    assert result.output.strip() == "sk-ant-oat-token-123"


def test_extract_apikey_export(sandbox_home):
    creds = sandbox_home / ".claude" / ".credentials.json"
    _write_creds(creds, "tok")
    runner = CliRunner()
    result = runner.invoke(dev_group, ["extract-apikey-from-credentials", "--export"])
    assert result.exit_code == 0
    assert "export SAC_ANTHROPIC_API_KEY=tok" in result.output


def test_extract_apikey_custom_path(tmp_path):
    creds = tmp_path / "custom.json"
    _write_creds(creds, "abc")
    runner = CliRunner()
    result = runner.invoke(
        dev_group,
        ["extract-apikey-from-credentials", "--path", str(creds)],
    )
    assert result.exit_code == 0
    assert "abc" in result.output


def test_extract_apikey_missing_file(sandbox_home):
    runner = CliRunner()
    result = runner.invoke(dev_group, ["extract-apikey-from-credentials"])
    assert result.exit_code != 0
    assert "not found" in result.output


def test_extract_apikey_bad_json(sandbox_home):
    creds = sandbox_home / ".claude" / ".credentials.json"
    creds.parent.mkdir(parents=True)
    creds.write_text("not json")
    runner = CliRunner()
    result = runner.invoke(dev_group, ["extract-apikey-from-credentials"])
    assert result.exit_code != 0
    assert "could not parse" in result.output


def test_extract_apikey_no_token_key(sandbox_home):
    creds = sandbox_home / ".claude" / ".credentials.json"
    creds.parent.mkdir(parents=True)
    creds.write_text(json.dumps({"other": "stuff"}))
    runner = CliRunner()
    result = runner.invoke(dev_group, ["extract-apikey-from-credentials"])
    assert result.exit_code != 0
    assert "no .claudeAiOauth.accessToken" in result.output


# ---------------------------------------------------------------------------
# upload-apikey-from-credentials-to-github
# ---------------------------------------------------------------------------


def _patch_repo_detect(monkeypatch, repo="owner/repo"):
    monkeypatch.setattr(dg, "_detect_repo", lambda: repo)


def _patch_gh_present(monkeypatch, present=True):
    monkeypatch.setattr(
        dg.shutil, "which", lambda cmd: "/usr/bin/gh" if present else None
    )


def test_upload_apikey_requires_scitex_git(monkeypatch):
    monkeypatch.setattr(dg, "_SCITEX_GIT_OK", False)
    runner = CliRunner()
    result = runner.invoke(
        dev_group, ["upload-apikey-from-credentials-to-github", "--dry-run"]
    )
    assert result.exit_code != 0
    assert "[dev] extra" in result.output


def test_upload_apikey_requires_gh(monkeypatch, patch_scitex_git):
    _patch_gh_present(monkeypatch, present=False)
    runner = CliRunner()
    result = runner.invoke(
        dev_group, ["upload-apikey-from-credentials-to-github", "--dry-run"]
    )
    assert result.exit_code != 0
    assert "'gh' CLI not found" in result.output


def test_upload_apikey_dry_run_credentials_source(
    sandbox_home, monkeypatch, patch_scitex_git
):
    _patch_gh_present(monkeypatch, present=True)
    _patch_repo_detect(monkeypatch)
    _write_creds(sandbox_home / ".claude" / ".credentials.json", "sk-ant-oat-abc")
    # Ensure no env var so OAuth file path is used.
    monkeypatch.setattr(dg, "_sac_env", lambda name: None)

    runner = CliRunner()
    result = runner.invoke(
        dev_group, ["upload-apikey-from-credentials-to-github", "--dry-run"]
    )
    assert result.exit_code == 0, result.output
    assert "dry-run" in result.output
    assert "owner/repo" in result.output
    assert "oauth" in result.output


def test_upload_apikey_env_var_source(monkeypatch, patch_scitex_git):
    _patch_gh_present(monkeypatch, present=True)
    _patch_repo_detect(monkeypatch)
    monkeypatch.setattr(dg, "_sac_env", lambda name: "sk-ant-api-xxxx")

    runner = CliRunner()
    result = runner.invoke(
        dev_group, ["upload-apikey-from-credentials-to-github", "--dry-run"]
    )
    assert result.exit_code == 0, result.output
    assert "api-key" in result.output
    assert "env:" in result.output


def test_upload_apikey_no_local_source(sandbox_home, monkeypatch, patch_scitex_git):
    _patch_gh_present(monkeypatch, present=True)
    _patch_repo_detect(monkeypatch)
    monkeypatch.setattr(dg, "_sac_env", lambda name: None)

    runner = CliRunner()
    result = runner.invoke(
        dev_group, ["upload-apikey-from-credentials-to-github", "--dry-run"]
    )
    assert result.exit_code != 0
    assert "no Anthropic auth" in result.output


def test_upload_apikey_remote_slot_present_matching(
    sandbox_home, monkeypatch, patch_scitex_git
):
    _patch_gh_present(monkeypatch, present=True)
    _patch_repo_detect(monkeypatch)
    monkeypatch.setattr(dg, "_sac_env", lambda name: "sk-ant-oat-xyz")
    # remote_sha matches → "yes"
    monkeypatch.setattr(
        dg, "list_secrets", lambda repo: {dg._ANTHROPIC_SLOT: "2026-01-01"}
    )
    monkeypatch.setattr(dg, "get_variable", lambda repo, name: "deadbeef" * 8)

    runner = CliRunner()
    result = runner.invoke(
        dev_group, ["upload-apikey-from-credentials-to-github", "--dry-run"]
    )
    assert result.exit_code == 0, result.output
    assert "match:       yes" in result.output


def test_upload_apikey_remote_mismatch(monkeypatch, patch_scitex_git):
    _patch_gh_present(monkeypatch, present=True)
    _patch_repo_detect(monkeypatch)
    monkeypatch.setattr(dg, "_sac_env", lambda name: "sk-ant-oat-xyz")
    monkeypatch.setattr(
        dg, "list_secrets", lambda repo: {dg._ANTHROPIC_SLOT: "2026-01-01"}
    )
    monkeypatch.setattr(dg, "get_variable", lambda repo, name: "differenthash")

    runner = CliRunner()
    result = runner.invoke(
        dev_group, ["upload-apikey-from-credentials-to-github", "--dry-run"]
    )
    assert result.exit_code == 0
    assert "local differs" in result.output


def test_upload_apikey_refuses_without_yes(monkeypatch, patch_scitex_git):
    _patch_gh_present(monkeypatch, present=True)
    _patch_repo_detect(monkeypatch)
    monkeypatch.setattr(dg, "_sac_env", lambda name: "sk-ant-api-xxxxxxxxxxxxxxxxxxxx")

    runner = CliRunner()
    result = runner.invoke(dev_group, ["upload-apikey-from-credentials-to-github"])
    assert result.exit_code == 2
    assert "Refusing" in result.output


def test_upload_apikey_yes_rotates(monkeypatch, patch_scitex_git):
    _patch_gh_present(monkeypatch, present=True)
    _patch_repo_detect(monkeypatch)
    monkeypatch.setattr(
        dg, "_sac_env", lambda name: "sk-ant-api-xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"
    )
    fake = MagicMock()
    monkeypatch.setattr(dg, "set_secret_with_sha_sidecar", fake)

    runner = CliRunner()
    result = runner.invoke(
        dev_group, ["upload-apikey-from-credentials-to-github", "--yes"]
    )
    assert result.exit_code == 0, result.output
    fake.assert_called_once()
    assert "rotated" in result.output


# ---------------------------------------------------------------------------
# upload-credentials-to-github
# ---------------------------------------------------------------------------


def test_upload_credentials_requires_scitex_git(monkeypatch):
    monkeypatch.setattr(dg, "_SCITEX_GIT_OK", False)
    runner = CliRunner()
    result = runner.invoke(dev_group, ["upload-credentials-to-github", "--dry-run"])
    assert result.exit_code != 0
    assert "[dev] extra" in result.output


def test_upload_credentials_requires_gh(monkeypatch, patch_scitex_git):
    _patch_gh_present(monkeypatch, present=False)
    runner = CliRunner()
    result = runner.invoke(dev_group, ["upload-credentials-to-github", "--dry-run"])
    assert result.exit_code != 0
    assert "'gh' CLI not found" in result.output


def test_upload_credentials_missing_file(monkeypatch, patch_scitex_git, sandbox_home):
    _patch_gh_present(monkeypatch, present=True)
    runner = CliRunner()
    result = runner.invoke(dev_group, ["upload-credentials-to-github", "--dry-run"])
    assert result.exit_code != 0
    assert "not found" in result.output


def test_upload_credentials_bad_json(monkeypatch, patch_scitex_git, sandbox_home):
    _patch_gh_present(monkeypatch, present=True)
    creds = sandbox_home / ".claude" / ".credentials.json"
    creds.parent.mkdir(parents=True)
    creds.write_text("not json")
    runner = CliRunner()
    result = runner.invoke(dev_group, ["upload-credentials-to-github", "--dry-run"])
    assert result.exit_code != 0
    assert "not valid JSON" in result.output


def test_upload_credentials_wrong_shape(monkeypatch, patch_scitex_git, sandbox_home):
    _patch_gh_present(monkeypatch, present=True)
    creds = sandbox_home / ".claude" / ".credentials.json"
    creds.parent.mkdir(parents=True)
    creds.write_text('{"foo": "bar"}')
    runner = CliRunner()
    result = runner.invoke(dev_group, ["upload-credentials-to-github", "--dry-run"])
    assert result.exit_code != 0
    assert "no .claudeAiOauth" in result.output


def test_upload_credentials_dry_run(monkeypatch, patch_scitex_git, sandbox_home):
    _patch_gh_present(monkeypatch, present=True)
    _patch_repo_detect(monkeypatch)
    _write_creds(sandbox_home / ".claude" / ".credentials.json")
    runner = CliRunner()
    result = runner.invoke(dev_group, ["upload-credentials-to-github", "--dry-run"])
    assert result.exit_code == 0, result.output
    assert "dry-run" in result.output


def test_upload_credentials_remote_present_matching(
    monkeypatch, patch_scitex_git, sandbox_home
):
    _patch_gh_present(monkeypatch, present=True)
    _patch_repo_detect(monkeypatch)
    _write_creds(sandbox_home / ".claude" / ".credentials.json")
    monkeypatch.setattr(
        dg, "list_secrets", lambda repo: {dg._CREDENTIALS_SLOT: "2026-01-01"}
    )
    monkeypatch.setattr(dg, "get_variable", lambda repo, name: "deadbeef" * 8)
    runner = CliRunner()
    result = runner.invoke(dev_group, ["upload-credentials-to-github", "--dry-run"])
    assert result.exit_code == 0, result.output
    assert "match:       yes" in result.output


def test_upload_credentials_remote_mismatch(
    monkeypatch, patch_scitex_git, sandbox_home
):
    _patch_gh_present(monkeypatch, present=True)
    _patch_repo_detect(monkeypatch)
    _write_creds(sandbox_home / ".claude" / ".credentials.json")
    monkeypatch.setattr(
        dg, "list_secrets", lambda repo: {dg._CREDENTIALS_SLOT: "2026-01-01"}
    )
    monkeypatch.setattr(dg, "get_variable", lambda repo, name: "otherhash")
    runner = CliRunner()
    result = runner.invoke(dev_group, ["upload-credentials-to-github", "--dry-run"])
    assert result.exit_code == 0
    assert "local differs" in result.output


def test_upload_credentials_refuses_without_yes(
    monkeypatch, patch_scitex_git, sandbox_home
):
    _patch_gh_present(monkeypatch, present=True)
    _patch_repo_detect(monkeypatch)
    _write_creds(sandbox_home / ".claude" / ".credentials.json")
    runner = CliRunner()
    result = runner.invoke(dev_group, ["upload-credentials-to-github"])
    assert result.exit_code == 2
    assert "Refusing" in result.output


def test_upload_credentials_yes_uploads(monkeypatch, patch_scitex_git, sandbox_home):
    _patch_gh_present(monkeypatch, present=True)
    _patch_repo_detect(monkeypatch)
    _write_creds(sandbox_home / ".claude" / ".credentials.json")
    fake = MagicMock()
    monkeypatch.setattr(dg, "set_secret_with_sha_sidecar", fake)
    runner = CliRunner()
    result = runner.invoke(dev_group, ["upload-credentials-to-github", "--yes"])
    assert result.exit_code == 0, result.output
    fake.assert_called_once()
    assert "uploaded" in result.output


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def test_detect_repo_ssh(monkeypatch):
    monkeypatch.setattr(
        dg.subprocess,
        "check_output",
        lambda *a, **k: "git@github.com:owner/repo.git\n",
    )
    assert dg._detect_repo() == "owner/repo"


def test_detect_repo_https(monkeypatch):
    monkeypatch.setattr(
        dg.subprocess,
        "check_output",
        lambda *a, **k: "https://github.com/foo/bar\n",
    )
    assert dg._detect_repo() == "foo/bar"


def test_detect_repo_failure(monkeypatch):
    def boom(*a, **k):
        raise subprocess.CalledProcessError(1, "git")

    monkeypatch.setattr(dg.subprocess, "check_output", boom)
    with pytest.raises(Exception):
        dg._detect_repo()


def test_classify_token():
    assert dg._classify_token("sk-ant-oat-abc") == "oauth"
    assert dg._classify_token("sk-ant-api-abc") == "api-key"
    assert dg._classify_token("anything-else") == "unknown"
