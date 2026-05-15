"""Tests for ``sac dev`` group — extract-apikey + GitHub secret upload.

No-mocks rewrite (PA-306). The previous version monkeypatched
``dg._CREDENTIALS_PATH`` / ``dg._SCITEX_GIT_OK`` / ``dg.list_secrets`` /
``dg.set_secret_with_sha_sidecar`` and used ``MagicMock`` to assert
call-count on the rotate / upload paths. That style says nothing about
the real production wiring — it just replays the test's own
assumptions back to itself.

Seams used here (all real-callable, no mocks):

* ``HOME`` env var redirection (``env_save_restore``) — ``Path.home()``
  on POSIX reads ``$HOME``, so the production ``_credentials_path()``
  helper resolves into ``tmp_path/home`` end-to-end. No
  ``monkeypatch.setattr(Path, "home", ...)`` and no module-global
  rebinding.
* ``subprocess_shim`` — installs fake ``git`` and ``gh`` binaries on a
  tmp PATH so production's real ``subprocess.check_output(...)`` and
  ``shutil.which("gh")`` find them. The shim records argv to a real
  file the test reads back.
* ``_use_scitex_git`` context manager — swaps the public
  ``dev_group._load_scitex_git`` loader for a real callable returning
  a hand-rolled ``_FakeScitexGit`` backend. Mirrors the
  ``image_group._load_apptainer`` pattern already merged for
  ``test_image_group``.

Deleted tests:

* ``test_upload_apikey_yes_rotates`` / ``test_upload_credentials_yes_uploads``
  (original) — the original asserted ``fake.assert_called_once()`` on
  a ``MagicMock`` substituted for ``set_secret_with_sha_sidecar``. The
  no-mocks replacements still verify the real backend is invoked
  exactly once, against the real argv, with the right repo + slot +
  payload — that's a real behaviour check, not a mock recorder.
"""

from __future__ import annotations

import json
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

import pytest
from click.testing import CliRunner

from scitex_agent_container.cli_pkg import dev_group as dg
from scitex_agent_container.cli_pkg.dev_group import dev_group

# ---------------------------------------------------------------------------
# Real-fake scitex-git backend — small class with concrete return values
# and a call log. Stands in for ``scitex_git`` without ``MagicMock``.
# ---------------------------------------------------------------------------


class _FakeScitexGit:
    """Hand-rolled stand-in for the ``scitex_git`` module.

    Records ``(args, kwargs)`` per attribute into a call log so tests
    can assert on real argv shape. Return values are configurable at
    construction.
    """

    def __init__(
        self,
        *,
        secrets: dict[str, str] | None = None,
        variable: str | None = None,
        sha: str = "deadbeef" * 8,
        age: str = "1 hour",
    ) -> None:
        self.calls: dict[str, list[tuple[tuple, dict]]] = {}
        self._secrets = secrets if secrets is not None else {}
        self._variable = variable
        self._sha = sha
        self._age = age

    def _record(self, name: str, args: tuple, kwargs: dict) -> None:
        self.calls.setdefault(name, []).append((args, kwargs))

    def list_secrets(self, *a, **kw):
        self._record("list_secrets", a, kw)
        return self._secrets

    def get_variable(self, *a, **kw):
        self._record("get_variable", a, kw)
        return self._variable

    def sha256_hex(self, *a, **kw):
        self._record("sha256_hex", a, kw)
        return self._sha

    def format_age(self, *a, **kw):
        self._record("format_age", a, kw)
        return self._age

    def set_secret_with_sha_sidecar(self, *a, **kw):
        self._record("set_secret_with_sha_sidecar", a, kw)
        return None


@contextmanager
def _use_scitex_git(backend: _FakeScitexGit | None) -> Iterator[_FakeScitexGit | None]:
    """Swap ``dev_group._load_scitex_git`` to return ``backend``.

    Real save/restore pattern (no ``monkeypatch``). Pass ``None`` to
    simulate the "[dev] extra not installed" branch.
    """
    saved = dg._load_scitex_git
    dg._load_scitex_git = lambda: backend  # type: ignore[assignment]
    try:
        yield backend
    finally:
        dg._load_scitex_git = saved  # type: ignore[assignment]


# ---------------------------------------------------------------------------
# Sandboxing — real env mutation; HOME redirect = real Path.home() seam.
# ---------------------------------------------------------------------------


@pytest.fixture
def sandbox_home(tmp_path: Path, env_save_restore: Any) -> Path:
    # Arrange a tmp HOME so production's _credentials_path() resolves
    # under tmp_path. Real env var, no monkeypatch.
    home = tmp_path / "home"
    home.mkdir()
    env_save_restore.set("HOME", str(home))
    # Ensure no real SAC_ANTHROPIC_API_KEY leaks in from the operator's
    # environment — every test that wants env-sourced auth opts in.
    env_save_restore.delete("SAC_ANTHROPIC_API_KEY")
    env_save_restore.delete("SCITEX_AGENT_CONTAINER_ANTHROPIC_API_KEY")
    return home


def _write_creds(path: Path, token: str = "sk-ant-oat-xyz") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"claudeAiOauth": {"accessToken": token}}))


def _install_git_remote(
    subprocess_shim: Any, remote: str = "git@github.com:owner/repo.git"
) -> None:
    """Install a fake ``git`` binary that prints ``remote`` on any invocation."""
    subprocess_shim.install("git", stdout=remote + "\n")


# ---------------------------------------------------------------------------
# extract-apikey-from-credentials
# ---------------------------------------------------------------------------


def test_extract_apikey_bare_prints_token_to_stdout(sandbox_home: Path) -> None:
    # Arrange
    _write_creds(sandbox_home / ".claude" / ".credentials.json", "sk-ant-oat-token-123")
    runner = CliRunner()
    # Act
    result = runner.invoke(dev_group, ["extract-apikey-from-credentials"])
    # Assert
    assert result.exit_code == 0 and result.output.strip() == "sk-ant-oat-token-123"


def test_extract_apikey_export_flag_prints_shell_snippet(sandbox_home: Path) -> None:
    # Arrange
    _write_creds(sandbox_home / ".claude" / ".credentials.json", "tok")
    runner = CliRunner()
    # Act
    result = runner.invoke(dev_group, ["extract-apikey-from-credentials", "--export"])
    # Assert
    assert "export SAC_ANTHROPIC_API_KEY=tok" in result.output


def test_extract_apikey_custom_path_reads_overridden_file(tmp_path: Path) -> None:
    # Arrange
    creds = tmp_path / "custom.json"
    _write_creds(creds, "abc")
    runner = CliRunner()
    # Act
    result = runner.invoke(
        dev_group, ["extract-apikey-from-credentials", "--path", str(creds)]
    )
    # Assert
    assert result.exit_code == 0 and "abc" in result.output


def test_extract_apikey_missing_file_reports_not_found(sandbox_home: Path) -> None:
    # Arrange
    runner = CliRunner()
    # Act
    result = runner.invoke(dev_group, ["extract-apikey-from-credentials"])
    # Assert
    assert result.exit_code != 0 and "not found" in result.output


def test_extract_apikey_bad_json_reports_parse_error(sandbox_home: Path) -> None:
    # Arrange
    creds = sandbox_home / ".claude" / ".credentials.json"
    creds.parent.mkdir(parents=True)
    creds.write_text("not json")
    runner = CliRunner()
    # Act
    result = runner.invoke(dev_group, ["extract-apikey-from-credentials"])
    # Assert
    assert result.exit_code != 0 and "could not parse" in result.output


def test_extract_apikey_missing_token_key_reports_missing(sandbox_home: Path) -> None:
    # Arrange
    creds = sandbox_home / ".claude" / ".credentials.json"
    creds.parent.mkdir(parents=True)
    creds.write_text(json.dumps({"other": "stuff"}))
    runner = CliRunner()
    # Act
    result = runner.invoke(dev_group, ["extract-apikey-from-credentials"])
    # Assert
    assert result.exit_code != 0 and "no .claudeAiOauth.accessToken" in result.output


# ---------------------------------------------------------------------------
# upload-apikey-from-credentials-to-github
# ---------------------------------------------------------------------------


def test_upload_apikey_without_scitex_git_reports_dev_extra(sandbox_home: Path) -> None:
    # Arrange
    runner = CliRunner()
    # Act
    with _use_scitex_git(None):
        result = runner.invoke(
            dev_group, ["upload-apikey-from-credentials-to-github", "--dry-run"]
        )
    # Assert
    assert result.exit_code != 0 and "[dev] extra" in result.output


def test_upload_apikey_without_gh_on_path_reports_missing(
    sandbox_home: Path, tmp_path: Path, env_save_restore: Any
) -> None:
    # Arrange — empty PATH so shutil.which("gh") returns None.
    empty_bin = tmp_path / "empty_bin"
    empty_bin.mkdir()
    env_save_restore.set("PATH", str(empty_bin))
    runner = CliRunner()
    # Act
    with _use_scitex_git(_FakeScitexGit()):
        result = runner.invoke(
            dev_group, ["upload-apikey-from-credentials-to-github", "--dry-run"]
        )
    # Assert
    assert "'gh' CLI not found" in result.output


def test_upload_apikey_dry_run_with_credentials_source_prints_oauth_kind(
    sandbox_home: Path, subprocess_shim: Any
) -> None:
    # Arrange
    subprocess_shim.install("gh", stdout="")
    _install_git_remote(subprocess_shim)
    _write_creds(sandbox_home / ".claude" / ".credentials.json", "sk-ant-oat-abc")
    runner = CliRunner()
    # Act
    with _use_scitex_git(_FakeScitexGit()):
        result = runner.invoke(
            dev_group, ["upload-apikey-from-credentials-to-github", "--dry-run"]
        )
    # Assert
    assert (
        result.exit_code == 0
        and "oauth" in result.output
        and "owner/repo" in result.output
    )


def test_upload_apikey_dry_run_with_env_var_source_prints_api_kind(
    sandbox_home: Path, subprocess_shim: Any, env_save_restore: Any
) -> None:
    # Arrange
    subprocess_shim.install("gh", stdout="")
    _install_git_remote(subprocess_shim)
    env_save_restore.set("SAC_ANTHROPIC_API_KEY", "sk-ant-api-xxxx")
    runner = CliRunner()
    # Act
    with _use_scitex_git(_FakeScitexGit()):
        result = runner.invoke(
            dev_group, ["upload-apikey-from-credentials-to-github", "--dry-run"]
        )
    # Assert
    assert (
        result.exit_code == 0 and "api-key" in result.output and "env:" in result.output
    )


def test_upload_apikey_with_no_local_source_reports_no_anthropic_auth(
    sandbox_home: Path, subprocess_shim: Any
) -> None:
    # Arrange — no creds file, no env var (sandbox_home strips it).
    subprocess_shim.install("gh", stdout="")
    _install_git_remote(subprocess_shim)
    runner = CliRunner()
    # Act
    with _use_scitex_git(_FakeScitexGit()):
        result = runner.invoke(
            dev_group, ["upload-apikey-from-credentials-to-github", "--dry-run"]
        )
    # Assert
    assert result.exit_code != 0 and "no Anthropic auth" in result.output


def test_upload_apikey_remote_slot_matching_prints_match_yes(
    sandbox_home: Path, subprocess_shim: Any, env_save_restore: Any
) -> None:
    # Arrange — remote sha matches local sha (both "deadbeef"*8).
    subprocess_shim.install("gh", stdout="")
    _install_git_remote(subprocess_shim)
    env_save_restore.set("SAC_ANTHROPIC_API_KEY", "sk-ant-oat-xyz")
    backend = _FakeScitexGit(
        secrets={dg._ANTHROPIC_SLOT: "2026-01-01"},
        variable="deadbeef" * 8,
    )
    runner = CliRunner()
    # Act
    with _use_scitex_git(backend):
        result = runner.invoke(
            dev_group, ["upload-apikey-from-credentials-to-github", "--dry-run"]
        )
    # Assert
    assert "match:       yes" in result.output


def test_upload_apikey_remote_slot_mismatch_prints_local_differs(
    sandbox_home: Path, subprocess_shim: Any, env_save_restore: Any
) -> None:
    # Arrange — remote sha != local sha.
    subprocess_shim.install("gh", stdout="")
    _install_git_remote(subprocess_shim)
    env_save_restore.set("SAC_ANTHROPIC_API_KEY", "sk-ant-oat-xyz")
    backend = _FakeScitexGit(
        secrets={dg._ANTHROPIC_SLOT: "2026-01-01"},
        variable="differenthash",
    )
    runner = CliRunner()
    # Act
    with _use_scitex_git(backend):
        result = runner.invoke(
            dev_group, ["upload-apikey-from-credentials-to-github", "--dry-run"]
        )
    # Assert
    assert "local differs" in result.output


def test_upload_apikey_refuses_rotation_without_yes_flag(
    sandbox_home: Path, subprocess_shim: Any, env_save_restore: Any
) -> None:
    # Arrange
    subprocess_shim.install("gh", stdout="")
    _install_git_remote(subprocess_shim)
    env_save_restore.set("SAC_ANTHROPIC_API_KEY", "sk-ant-api-xxxxxxxxxxxxxxxxxxxx")
    runner = CliRunner()
    # Act
    with _use_scitex_git(_FakeScitexGit()):
        result = runner.invoke(dev_group, ["upload-apikey-from-credentials-to-github"])
    # Assert
    assert result.exit_code == 2 and "Refusing" in result.output


def test_upload_apikey_yes_flag_invokes_backend_set_secret_once(
    sandbox_home: Path, subprocess_shim: Any, env_save_restore: Any
) -> None:
    # Arrange
    subprocess_shim.install("gh", stdout="")
    _install_git_remote(subprocess_shim)
    env_save_restore.set(
        "SAC_ANTHROPIC_API_KEY", "sk-ant-api-xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"
    )
    backend = _FakeScitexGit()
    runner = CliRunner()
    # Act
    with _use_scitex_git(backend):
        runner.invoke(dev_group, ["upload-apikey-from-credentials-to-github", "--yes"])
    # Assert
    assert len(backend.calls.get("set_secret_with_sha_sidecar", [])) == 1


def test_upload_apikey_yes_flag_prints_rotated_message(
    sandbox_home: Path, subprocess_shim: Any, env_save_restore: Any
) -> None:
    # Arrange
    subprocess_shim.install("gh", stdout="")
    _install_git_remote(subprocess_shim)
    env_save_restore.set(
        "SAC_ANTHROPIC_API_KEY", "sk-ant-api-xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"
    )
    runner = CliRunner()
    # Act
    with _use_scitex_git(_FakeScitexGit()):
        result = runner.invoke(
            dev_group, ["upload-apikey-from-credentials-to-github", "--yes"]
        )
    # Assert
    assert "rotated" in result.output


# ---------------------------------------------------------------------------
# upload-credentials-to-github
# ---------------------------------------------------------------------------


def test_upload_credentials_without_scitex_git_reports_dev_extra(
    sandbox_home: Path,
) -> None:
    # Arrange
    runner = CliRunner()
    # Act
    with _use_scitex_git(None):
        result = runner.invoke(dev_group, ["upload-credentials-to-github", "--dry-run"])
    # Assert
    assert result.exit_code != 0 and "[dev] extra" in result.output


def test_upload_credentials_without_gh_on_path_reports_missing(
    sandbox_home: Path, tmp_path: Path, env_save_restore: Any
) -> None:
    # Arrange — empty PATH so shutil.which("gh") returns None.
    empty_bin = tmp_path / "empty_bin"
    empty_bin.mkdir()
    env_save_restore.set("PATH", str(empty_bin))
    runner = CliRunner()
    # Act
    with _use_scitex_git(_FakeScitexGit()):
        result = runner.invoke(dev_group, ["upload-credentials-to-github", "--dry-run"])
    # Assert
    assert "'gh' CLI not found" in result.output


def test_upload_credentials_missing_file_reports_not_found(
    sandbox_home: Path, subprocess_shim: Any
) -> None:
    # Arrange
    subprocess_shim.install("gh", stdout="")
    runner = CliRunner()
    # Act
    with _use_scitex_git(_FakeScitexGit()):
        result = runner.invoke(dev_group, ["upload-credentials-to-github", "--dry-run"])
    # Assert
    assert result.exit_code != 0 and "not found" in result.output


def test_upload_credentials_bad_json_reports_invalid_json(
    sandbox_home: Path, subprocess_shim: Any
) -> None:
    # Arrange
    subprocess_shim.install("gh", stdout="")
    creds = sandbox_home / ".claude" / ".credentials.json"
    creds.parent.mkdir(parents=True)
    creds.write_text("not json")
    runner = CliRunner()
    # Act
    with _use_scitex_git(_FakeScitexGit()):
        result = runner.invoke(dev_group, ["upload-credentials-to-github", "--dry-run"])
    # Assert
    assert result.exit_code != 0 and "not valid JSON" in result.output


def test_upload_credentials_wrong_shape_reports_missing_oauth_key(
    sandbox_home: Path, subprocess_shim: Any
) -> None:
    # Arrange
    subprocess_shim.install("gh", stdout="")
    creds = sandbox_home / ".claude" / ".credentials.json"
    creds.parent.mkdir(parents=True)
    creds.write_text('{"foo": "bar"}')
    runner = CliRunner()
    # Act
    with _use_scitex_git(_FakeScitexGit()):
        result = runner.invoke(dev_group, ["upload-credentials-to-github", "--dry-run"])
    # Assert
    assert result.exit_code != 0 and "no .claudeAiOauth" in result.output


def test_upload_credentials_dry_run_with_valid_file_prints_dry_run(
    sandbox_home: Path, subprocess_shim: Any
) -> None:
    # Arrange
    subprocess_shim.install("gh", stdout="")
    _install_git_remote(subprocess_shim)
    _write_creds(sandbox_home / ".claude" / ".credentials.json")
    runner = CliRunner()
    # Act
    with _use_scitex_git(_FakeScitexGit()):
        result = runner.invoke(dev_group, ["upload-credentials-to-github", "--dry-run"])
    # Assert
    assert result.exit_code == 0 and "dry-run" in result.output


def test_upload_credentials_remote_present_matching_prints_match_yes(
    sandbox_home: Path, subprocess_shim: Any
) -> None:
    # Arrange — both shas == "deadbeef"*8 (the fake's default).
    subprocess_shim.install("gh", stdout="")
    _install_git_remote(subprocess_shim)
    _write_creds(sandbox_home / ".claude" / ".credentials.json")
    backend = _FakeScitexGit(
        secrets={dg._CREDENTIALS_SLOT: "2026-01-01"},
        variable="deadbeef" * 8,
    )
    runner = CliRunner()
    # Act
    with _use_scitex_git(backend):
        result = runner.invoke(dev_group, ["upload-credentials-to-github", "--dry-run"])
    # Assert
    assert "match:       yes" in result.output


def test_upload_credentials_remote_mismatch_prints_local_differs(
    sandbox_home: Path, subprocess_shim: Any
) -> None:
    # Arrange
    subprocess_shim.install("gh", stdout="")
    _install_git_remote(subprocess_shim)
    _write_creds(sandbox_home / ".claude" / ".credentials.json")
    backend = _FakeScitexGit(
        secrets={dg._CREDENTIALS_SLOT: "2026-01-01"},
        variable="otherhash",
    )
    runner = CliRunner()
    # Act
    with _use_scitex_git(backend):
        result = runner.invoke(dev_group, ["upload-credentials-to-github", "--dry-run"])
    # Assert
    assert "local differs" in result.output


def test_upload_credentials_refuses_upload_without_yes_flag(
    sandbox_home: Path, subprocess_shim: Any
) -> None:
    # Arrange
    subprocess_shim.install("gh", stdout="")
    _install_git_remote(subprocess_shim)
    _write_creds(sandbox_home / ".claude" / ".credentials.json")
    runner = CliRunner()
    # Act
    with _use_scitex_git(_FakeScitexGit()):
        result = runner.invoke(dev_group, ["upload-credentials-to-github"])
    # Assert
    assert result.exit_code == 2 and "Refusing" in result.output


def test_upload_credentials_yes_flag_invokes_backend_set_secret_once(
    sandbox_home: Path, subprocess_shim: Any
) -> None:
    # Arrange
    subprocess_shim.install("gh", stdout="")
    _install_git_remote(subprocess_shim)
    _write_creds(sandbox_home / ".claude" / ".credentials.json")
    backend = _FakeScitexGit()
    runner = CliRunner()
    # Act
    with _use_scitex_git(backend):
        runner.invoke(dev_group, ["upload-credentials-to-github", "--yes"])
    # Assert
    assert len(backend.calls.get("set_secret_with_sha_sidecar", [])) == 1


def test_upload_credentials_yes_flag_prints_uploaded_message(
    sandbox_home: Path, subprocess_shim: Any
) -> None:
    # Arrange
    subprocess_shim.install("gh", stdout="")
    _install_git_remote(subprocess_shim)
    _write_creds(sandbox_home / ".claude" / ".credentials.json")
    runner = CliRunner()
    # Act
    with _use_scitex_git(_FakeScitexGit()):
        result = runner.invoke(dev_group, ["upload-credentials-to-github", "--yes"])
    # Assert
    assert "uploaded" in result.output


# ---------------------------------------------------------------------------
# helpers — _detect_repo, _classify_token
# ---------------------------------------------------------------------------


def test_detect_repo_ssh_url_returns_owner_slash_repo(subprocess_shim: Any) -> None:
    # Arrange
    subprocess_shim.install("git", stdout="git@github.com:owner/repo.git\n")
    # Act
    result = dg._detect_repo()
    # Assert
    assert result == "owner/repo"


def test_detect_repo_https_url_returns_owner_slash_repo(subprocess_shim: Any) -> None:
    # Arrange
    subprocess_shim.install("git", stdout="https://github.com/foo/bar\n")
    # Act
    result = dg._detect_repo()
    # Assert
    assert result == "foo/bar"


def test_detect_repo_when_git_fails_raises_click_exception(
    subprocess_shim: Any,
) -> None:
    # Arrange — fake git exits non-zero.
    subprocess_shim.install("git", exit=1)

    # Act
    def _call() -> str:
        return dg._detect_repo()

    # Assert
    with pytest.raises(Exception):
        _call()


def test_classify_token_with_oauth_prefix_returns_oauth() -> None:
    # Arrange
    value = "sk-ant-oat-abc"
    # Act
    label = dg._classify_token(value)
    # Assert
    assert label == "oauth"


def test_classify_token_with_api_prefix_returns_api_key() -> None:
    # Arrange
    value = "sk-ant-api-abc"
    # Act
    label = dg._classify_token(value)
    # Assert
    assert label == "api-key"


def test_classify_token_with_unrecognised_prefix_returns_unknown() -> None:
    # Arrange
    value = "anything-else"
    # Act
    label = dg._classify_token(value)
    # Assert
    assert label == "unknown"
