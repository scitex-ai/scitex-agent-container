"""Tests for ``--skip-active`` excluding pinned-and-running accounts.

Regression coverage for the 2026-06-04 neurovista 401 storm: the
host-side ``sac accounts refresh --all --skip-active`` cron (every 2h
via the federated systemd-user timer) was rotating refresh_tokens for
accounts CURRENTLY pinned by running agents (``spec.claude.account:
<name>``). Both refreshers (host cron + in-container CLI) used the
same refresh_token, OAuth refresh-tokens invalidate on use → race →
401 ~hourly. The fix extends ``--skip-active``'s skip-set with the
account names enumerated from the local file-based agent registry
(the same JSONs ``sac status`` reads, written by
``_lifecycle/_start`` at spawn time).

No-mocks (PA-306): real on-disk registry JSON + real on-disk
``spec.yaml`` files; ``load_config`` is the production parser. HTTP
is injected at ``urllib.request.urlopen`` (the production seam)
via the same ``opener_swap`` shape the sibling refresh tests use.

AAA marker comments; one assertion per test; ≥3-word names.
"""

from __future__ import annotations

from tests.scitex_agent_container._helpers.explicit_spec import explicitize_yaml

import json
from pathlib import Path
from typing import Any, Iterator

import pytest
from click.testing import CliRunner

from scitex_agent_container._state.account_store import save_account
from scitex_agent_container.cli_pkg._account_refresh_skip import (
    _collect_pinned_running_accounts,
    _resolve_registry_dir,
)
from scitex_agent_container.cli_pkg.account_group import account

# ---------------------------------------------------------------------------
# Sandbox + helpers
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def sandbox_home(tmp_path, env_save_restore):
    """Redirect ``$HOME`` so ``Path.home()`` lands inside ``tmp_path``."""
    home = tmp_path / "home"
    home.mkdir()
    env_save_restore.set("HOME", str(home))
    # Some tests check the env override path explicitly; default the
    # explicit override OFF so HOME-derived resolution is exercised.
    env_save_restore.delete("SCITEX_AGENT_CONTAINER_REGISTRY_DIR")
    return home


def _seed_account(home: Path, name: str, *, refresh: str = "the-refresh") -> Path:
    save_account(name, {"email_address": f"{name}@x"}, home=home)
    creds = (
        home / ".scitex" / "agent-container" / "accounts" / name / ".credentials.json"
    )
    creds.write_text(
        json.dumps(
            {
                "claudeAiOauth": {
                    "accessToken": "OLD-ACCESS",
                    "refreshToken": refresh,
                    "clientId": "cid",
                }
            }
        )
    )
    return creds


def _write_pinned_spec(parent: Path, *, name: str, account: str) -> Path:
    """Materialise ``<parent>/<name>/spec.yaml`` pinned to ``account``."""
    spec_dir = parent / name
    spec_dir.mkdir(parents=True, exist_ok=True)
    spec = spec_dir / "spec.yaml"
    spec.write_text(
        explicitize_yaml("apiVersion: scitex-agent-container/v3\n"
        "kind: Agent\n"
        "spec:\n"
        "  runtime: apptainer\n"
        "  host: ${HOSTNAME}\n"
        "  workdir: /home/agent/work\n"
        "  apptainer:\n    image: /x.sif\n    binds: []\n"
        "  health:\n    enabled: true\n    interval: 60\n"
        "  restart:\n    policy: on-failure\n    max_retries: 3\n"
        f"  claude:\n"
        f"    account: {account}\n"
        "    model: sonnet\n")
    )
    return spec


def _write_unpinned_spec(parent: Path, *, name: str) -> Path:
    spec_dir = parent / name
    spec_dir.mkdir(parents=True, exist_ok=True)
    spec = spec_dir / "spec.yaml"
    spec.write_text(
        explicitize_yaml("apiVersion: scitex-agent-container/v3\n"
        "kind: Agent\n"
        "spec:\n"
        "  runtime: apptainer\n"
        "  host: ${HOSTNAME}\n"
        "  workdir: /home/agent/work\n"
        "  apptainer:\n    image: /x.sif\n    binds: []\n"
        "  claude:\n    model: sonnet\n"
        "  health:\n    enabled: true\n    interval: 60\n"
        "  restart:\n    policy: on-failure\n    max_retries: 3\n")
    )
    return spec


def _register_running(home: Path, *, name: str, config_path: str | None) -> Path:
    """Write a registry JSON the way ``_lifecycle/_start`` writes it."""
    reg_dir = home / ".scitex" / "agent-container" / "runtime" / "registry"
    reg_dir.mkdir(parents=True, exist_ok=True)
    entry = reg_dir / f"{name}.json"
    payload: dict[str, Any] = {
        "name": name,
        "pid": 1,
        "started_at": "2026-06-04T17:35:00Z",
        "screen": name,
    }
    if config_path is not None:
        payload["config"] = config_path
    entry.write_text(json.dumps(payload))
    return entry


class _FakeResp:
    def __init__(self, body: bytes):
        self._body = body

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def read(self):
        return self._body


@pytest.fixture
def opener_swap() -> Iterator[dict]:
    """Swap ``urllib.request.urlopen`` at the production HTTP boundary."""
    import urllib.request

    state: dict[str, Any] = {"response": {"access_token": "NEW", "expires_in": 3600}}
    saved = urllib.request.urlopen

    def fake_urlopen(req, timeout=None):
        resp = state["response"]
        if isinstance(resp, Exception):
            raise resp
        return _FakeResp(json.dumps(resp).encode())

    urllib.request.urlopen = fake_urlopen  # type: ignore[assignment]
    try:
        yield state
    finally:
        urllib.request.urlopen = saved  # type: ignore[assignment]


# ---------------------------------------------------------------------------
# _resolve_registry_dir: env override + HOME-derived fallback
# ---------------------------------------------------------------------------


def test_resolve_registry_dir_uses_home_subdirectory_by_default(
    sandbox_home,
) -> None:
    # Arrange — no env override (autouse fixture deletes it).
    # Act
    resolved = _resolve_registry_dir(sandbox_home)
    # Assert
    assert (
        resolved
        == sandbox_home / ".scitex" / "agent-container" / "runtime" / "registry"
    )


def test_resolve_registry_dir_honors_env_override_when_set(
    sandbox_home, env_save_restore, tmp_path
) -> None:
    # Arrange
    override = tmp_path / "elsewhere"
    env_save_restore.set("SCITEX_AGENT_CONTAINER_REGISTRY_DIR", str(override))
    # Act
    resolved = _resolve_registry_dir(sandbox_home)
    # Assert — env override wins over HOME-derived default.
    assert resolved == override


# ---------------------------------------------------------------------------
# _collect_pinned_running_accounts: the new helper
# ---------------------------------------------------------------------------


def test_collect_pinned_returns_empty_when_no_registry_dir_exists(
    sandbox_home,
) -> None:
    # Arrange — sandbox HOME has NO registry subdirectory at all.
    # Act
    pinned = _collect_pinned_running_accounts(sandbox_home)
    # Assert
    assert pinned == set()


def test_collect_pinned_returns_empty_when_no_running_agents(
    sandbox_home,
) -> None:
    # Arrange — empty registry directory.
    (sandbox_home / ".scitex" / "agent-container" / "runtime" / "registry").mkdir(
        parents=True
    )
    # Act
    pinned = _collect_pinned_running_accounts(sandbox_home)
    # Assert
    assert pinned == set()


def test_collect_pinned_returns_account_for_one_pinned_agent(
    sandbox_home, tmp_path
) -> None:
    # Arrange — a single running agent pinned to ``wyusuuke``.
    spec = _write_pinned_spec(tmp_path / "specs", name="alpha", account="wyusuuke")
    _register_running(sandbox_home, name="alpha", config_path=str(spec))
    # Act
    pinned = _collect_pinned_running_accounts(sandbox_home)
    # Assert
    assert pinned == {"wyusuuke"}


def test_collect_pinned_returns_empty_for_running_unpinned_agent(
    sandbox_home, tmp_path
) -> None:
    # Arrange — running agent with NO ``spec.claude.account``.
    spec = _write_unpinned_spec(tmp_path / "specs", name="alpha")
    _register_running(sandbox_home, name="alpha", config_path=str(spec))
    # Act
    pinned = _collect_pinned_running_accounts(sandbox_home)
    # Assert
    assert pinned == set()


def test_collect_pinned_unions_multiple_pinned_agents(sandbox_home, tmp_path) -> None:
    # Arrange — two running agents pinned to two distinct accounts.
    a = _write_pinned_spec(tmp_path / "specs", name="alpha", account="acct-a")
    b = _write_pinned_spec(tmp_path / "specs", name="beta", account="acct-b")
    _register_running(sandbox_home, name="alpha", config_path=str(a))
    _register_running(sandbox_home, name="beta", config_path=str(b))
    # Act
    pinned = _collect_pinned_running_accounts(sandbox_home)
    # Assert — both account names surface; order doesn't matter (set).
    assert pinned == {"acct-a", "acct-b"}


def test_collect_pinned_dedupes_two_agents_on_same_account(
    sandbox_home, tmp_path
) -> None:
    # Arrange — two running agents BOTH pinned to ``shared``.
    a = _write_pinned_spec(tmp_path / "specs", name="alpha", account="shared")
    b = _write_pinned_spec(tmp_path / "specs", name="beta", account="shared")
    _register_running(sandbox_home, name="alpha", config_path=str(a))
    _register_running(sandbox_home, name="beta", config_path=str(b))
    # Act
    pinned = _collect_pinned_running_accounts(sandbox_home)
    # Assert — set semantics dedupe the duplicate ``shared`` entry.
    assert pinned == {"shared"}


def test_collect_pinned_tolerates_registry_entry_without_config_field(
    sandbox_home,
) -> None:
    # Arrange — registry entry missing the ``config`` key entirely.
    _register_running(sandbox_home, name="alpha", config_path=None)
    # Act
    pinned = _collect_pinned_running_accounts(sandbox_home)
    # Assert — tolerantly skipped, returns empty rather than crashing.
    assert pinned == set()


def test_collect_pinned_tolerates_unreadable_spec_path(
    sandbox_home,
) -> None:
    # Arrange — registry entry points at a spec that does not exist.
    _register_running(sandbox_home, name="alpha", config_path="/nonexistent/spec.yaml")
    # Act
    pinned = _collect_pinned_running_accounts(sandbox_home)
    # Assert
    assert pinned == set()


def test_collect_pinned_tolerates_malformed_registry_json(
    sandbox_home,
) -> None:
    # Arrange — corrupted registry JSON.
    reg_dir = sandbox_home / ".scitex" / "agent-container" / "runtime" / "registry"
    reg_dir.mkdir(parents=True)
    (reg_dir / "alpha.json").write_text("{not valid json")
    # Act
    pinned = _collect_pinned_running_accounts(sandbox_home)
    # Assert — one bad row must not crash the whole skip-set build.
    assert pinned == set()


def test_collect_pinned_recovers_after_one_bad_row(sandbox_home, tmp_path) -> None:
    # Arrange — one corrupt entry plus one healthy entry pinned to ``good``.
    reg_dir = sandbox_home / ".scitex" / "agent-container" / "runtime" / "registry"
    reg_dir.mkdir(parents=True)
    (reg_dir / "bad.json").write_text("{corrupt")
    spec = _write_pinned_spec(tmp_path / "specs", name="alpha", account="good")
    _register_running(sandbox_home, name="alpha", config_path=str(spec))
    # Act
    pinned = _collect_pinned_running_accounts(sandbox_home)
    # Assert — the healthy entry survives the corrupt sibling.
    assert pinned == {"good"}


# ---------------------------------------------------------------------------
# CLI integration: --all --skip-active also excludes pinned-running
# ---------------------------------------------------------------------------


def test_skip_active_excludes_pinned_running_account_from_refresh(
    sandbox_home, opener_swap, tmp_path
) -> None:
    # Arrange — two stored accounts; alpha pinned to a running agent, beta unpinned.
    creds_a = _seed_account(sandbox_home, "alpha")
    _seed_account(sandbox_home, "beta")
    spec = _write_pinned_spec(tmp_path / "specs", name="agent-x", account="alpha")
    _register_running(sandbox_home, name="agent-x", config_path=str(spec))
    runner = CliRunner()
    # Act
    runner.invoke(account, ["refresh", "--all", "--skip-active"])
    # Assert — alpha's stored token was NOT rotated (still the seeded value).
    written_alpha = json.loads(creds_a.read_text())["claudeAiOauth"]["accessToken"]
    assert written_alpha == "OLD-ACCESS"


def test_skip_active_still_refreshes_unpinned_account_when_other_pinned(
    sandbox_home, opener_swap, tmp_path
) -> None:
    # Arrange — alpha pinned to running agent; beta unpinned + unrelated.
    _seed_account(sandbox_home, "alpha")
    creds_b = _seed_account(sandbox_home, "beta")
    spec = _write_pinned_spec(tmp_path / "specs", name="agent-x", account="alpha")
    _register_running(sandbox_home, name="agent-x", config_path=str(spec))
    runner = CliRunner()
    # Act
    runner.invoke(account, ["refresh", "--all", "--skip-active"])
    # Assert — beta was refreshed (not pinned-running, not host-active).
    written_beta = json.loads(creds_b.read_text())["claudeAiOauth"]["accessToken"]
    assert written_beta == "NEW"


def test_skip_active_logs_pinned_running_exclusion_to_stderr(
    sandbox_home, opener_swap, tmp_path
) -> None:
    # Arrange
    _seed_account(sandbox_home, "alpha")
    spec = _write_pinned_spec(tmp_path / "specs", name="agent-x", account="alpha")
    _register_running(sandbox_home, name="agent-x", config_path=str(spec))
    runner = CliRunner()
    # Act
    result = runner.invoke(account, ["refresh", "--all", "--skip-active"])
    # Assert — operator gets a diagnostic naming the excluded account + reason.
    # click >=8.2 separates stderr; <8.2 merges it. Tolerate both.
    stderr_text = getattr(result, "stderr", "") or ""
    all_out = (result.output or "") + stderr_text
    assert "pinned-running" in all_out and "alpha" in all_out


def test_skip_active_without_pinned_running_still_refreshes_target(
    sandbox_home, opener_swap
) -> None:
    # Arrange — no running agents at all; beta is the only target.
    creds_b = _seed_account(sandbox_home, "beta")
    runner = CliRunner()
    # Act
    runner.invoke(account, ["refresh", "--all", "--skip-active"])
    # Assert — without any pinned-running set, the normal refresh proceeds.
    written = json.loads(creds_b.read_text())["claudeAiOauth"]["accessToken"]
    assert written == "NEW"


def test_skip_active_excludes_both_pinned_running_and_host_active(
    sandbox_home, opener_swap, tmp_path
) -> None:
    # Arrange — alpha pinned-running; beta is the host's ~/.claude active login.
    creds_a = _seed_account(sandbox_home, "alpha")
    creds_b = _seed_account(sandbox_home, "beta")
    # Pin alpha to a running agent.
    spec = _write_pinned_spec(tmp_path / "specs", name="agent-x", account="alpha")
    _register_running(sandbox_home, name="agent-x", config_path=str(spec))
    # Seed the host's ~/.claude.json so beta resolves as the active account
    # (email field matches what save_account stored above).
    (sandbox_home / ".claude.json").write_text(
        json.dumps({"oauthAccount": {"emailAddress": "beta@x"}})
    )
    runner = CliRunner()
    # Act
    runner.invoke(account, ["refresh", "--all", "--skip-active"])
    # Assert — neither alpha (pinned-running) nor beta (host-active) was touched.
    a = json.loads(creds_a.read_text())["claudeAiOauth"]["accessToken"]
    b = json.loads(creds_b.read_text())["claudeAiOauth"]["accessToken"]
    assert a == "OLD-ACCESS" and b == "OLD-ACCESS"
