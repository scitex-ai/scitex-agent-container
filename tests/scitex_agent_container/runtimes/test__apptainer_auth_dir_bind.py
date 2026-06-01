"""Pinned-account OAuth bind must be a DIRECTORY bind (task #11 regression).

Why this exists
---------------
PR #262 (operator task #15) made ``resolve_cred_file`` return the
per-account snapshot path directly and the runtime bound that
snapshot file ``:rw`` into ``/tmp/sac-claude/.credentials.json``.
This fixed the boot-copy / stale-token outage but introduced a
namespace-level regression that the lead observed live on 4 of 5
running account-pinned agents: a SINGLE-FILE bind mount is on the
file's dentry/inode. Host-side writers — ``creds_sync._atomic_copy``
(line ~138), ``account_store.switch_account`` (line ~307),
``claude_usage._refresh_access_token_at`` (line ~284) — all replace
the snapshot via ``tmp + os.replace`` (atomic rename). After the
rename the bind mount still points at the ORPHAN inode (visible as
``...credentials.json//deleted`` in ``/proc/<pid>/mountinfo``), so
every already-running pinned agent silently loses the shared
snapshot and the per-copy collision-401 disease returns.

The structural fix: bind the per-account DIRECTORY
(``~/.scitex/agent-container/accounts/<acct>/``) at ``/tmp/sac-claude``
instead of the single file. A directory bind resolves files by name
through the underlying filesystem on every open, so an atomic-replace
inside the dir is reflected immediately in the container without a
restart. ``CLAUDE_CONFIG_DIR=/tmp/sac-claude`` is unchanged — the
in-container Claude CLI still finds ``.credentials.json`` under it.

No mocks (STX-NM / PA-306): real ``$HOME`` redirect, real account-store
on disk, real ``auth_argv`` and ``resolve_cred_file`` invocations.
Each test pins one observable fact (TQ007) with AAA markers (TQ002)
and a descriptive name (TQ003).
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest

from scitex_agent_container.config import AgentConfig
from scitex_agent_container.config._types import ClaudeSpec
from scitex_agent_container.runtimes._apptainer_auth import auth_argv

# ---------------------------------------------------------------------------
# Fixtures + helpers (mirror the other apptainer test modules' shape)
# ---------------------------------------------------------------------------


@pytest.fixture
def home_redirect(tmp_path: Path, env_save_restore) -> Path:
    """Redirect ``$HOME`` so credential resolution stays in the sandbox."""
    home = tmp_path / "home"
    home.mkdir()
    env_save_restore.set("HOME", str(home))
    return home


def _write_snapshot(
    home: Path, name: str, expires_at_seconds: float, *, token: str = "boot-token"
) -> Path:
    """Materialise a real saved-account snapshot at the on-disk layout
    that ``sac accounts save`` writes."""
    acct_dir = home / ".scitex" / "agent-container" / "accounts" / name
    acct_dir.mkdir(parents=True, exist_ok=True)
    snap = acct_dir / ".credentials.json"
    body = {
        "claudeAiOauth": {
            "accessToken": token,
            "expiresAt": int(expires_at_seconds * 1_000),
        }
    }
    snap.write_text(json.dumps(body))
    return snap


def _pinned_config(workdir: Path, account: str) -> AgentConfig:
    return AgentConfig(
        name="pinned-agent",
        runtime="apptainer",
        workdir=str(workdir),
        claude=ClaudeSpec(account=account),
    )


def _extract_bind_source_for_target(argv: list[str], target: str) -> str | None:
    """Return the source path of the first ``--bind src:dest[:opts]`` whose
    dest equals ``target``. Returns ``None`` if no such bind exists."""
    for i, tok in enumerate(argv):
        if tok != "--bind":
            continue
        if i + 1 >= len(argv):
            continue
        spec = argv[i + 1]
        parts = spec.split(":")
        if len(parts) < 2:
            continue
        src, dest = parts[0], parts[1]
        if dest == target:
            return src
    return None


def _extract_bind_spec_for_target_prefix(
    argv: list[str], target_prefix: str
) -> str | None:
    """Return the FULL ``src:dest[:opts]`` spec whose dest starts with
    ``target_prefix``. Used to also catch the legacy single-file bind whose
    dest is ``/tmp/sac-claude/.credentials.json`` (prefix match)."""
    for i, tok in enumerate(argv):
        if tok != "--bind":
            continue
        if i + 1 >= len(argv):
            continue
        spec = argv[i + 1]
        parts = spec.split(":")
        if len(parts) < 2:
            continue
        if parts[1].startswith(target_prefix):
            return spec
    return None


# ---------------------------------------------------------------------------
# Pinned-account dir-bind shape (the structural fix)
# ---------------------------------------------------------------------------


def test_pinned_bind_source_is_the_account_directory(
    tmp_path: Path, home_redirect: Path
) -> None:
    # Arrange — a healthy pinned snapshot. The expected source of the
    # /tmp/sac-claude bind is the account DIR (not the snapshot file).
    now = time.time()
    snap = _write_snapshot(home_redirect, "alpha", now + 3_600)
    cfg = _pinned_config(tmp_path / "wd", account="alpha")
    # Act
    argv = auth_argv(cfg, state_dir=tmp_path / "state")
    src = _extract_bind_source_for_target(argv, "/tmp/sac-claude")
    # Assert — the bind source is the account dir, not the snapshot file.
    # File binds detach under host atomic-replace ("//deleted" in
    # /proc/<pid>/mountinfo); a dir bind survives.
    assert src == str(snap.parent)


def test_pinned_bind_destination_is_directory_not_file_path(
    tmp_path: Path, home_redirect: Path
) -> None:
    # Arrange — the legacy single-file bind targeted
    # /tmp/sac-claude/.credentials.json. The new dir-bind targets the
    # dir itself; CLAUDE_CONFIG_DIR is unchanged.
    now = time.time()
    _write_snapshot(home_redirect, "alpha", now + 3_600)
    cfg = _pinned_config(tmp_path / "wd", account="alpha")
    # Act
    argv = auth_argv(cfg, state_dir=tmp_path / "state")
    spec = _extract_bind_spec_for_target_prefix(argv, "/tmp/sac-claude")
    # Assert — the dest is exactly /tmp/sac-claude (dir), not
    # /tmp/sac-claude/.credentials.json (the file the legacy bind hit).
    assert spec is not None and spec.split(":")[1] == "/tmp/sac-claude"


def test_pinned_bind_is_rw(tmp_path: Path, home_redirect: Path) -> None:
    # Arrange — the bind must stay :rw so refresh writeback by the
    # in-container Claude CLI lands on the shared snapshot.
    now = time.time()
    _write_snapshot(home_redirect, "alpha", now + 3_600)
    cfg = _pinned_config(tmp_path / "wd", account="alpha")
    # Act
    argv = auth_argv(cfg, state_dir=tmp_path / "state")
    spec = _extract_bind_spec_for_target_prefix(argv, "/tmp/sac-claude")
    # Assert
    assert spec is not None and spec.split(":")[-1] == "rw"


def test_pinned_claude_config_dir_env_unchanged(
    tmp_path: Path, home_redirect: Path
) -> None:
    # Arrange — the dir-bind fix MUST keep CLAUDE_CONFIG_DIR pointing at
    # /tmp/sac-claude so the in-container CLI finds .credentials.json
    # inside the bound dir.
    now = time.time()
    _write_snapshot(home_redirect, "alpha", now + 3_600)
    cfg = _pinned_config(tmp_path / "wd", account="alpha")
    # Act
    argv = auth_argv(cfg, state_dir=tmp_path / "state")
    # Assert
    assert "CLAUDE_CONFIG_DIR=/tmp/sac-claude" in argv


# ---------------------------------------------------------------------------
# Host atomic-replace regression: the bind source's view of
# .credentials.json must reflect the NEW inode after a tmp+os.replace
# (this is the failure mode that knocked 4/5 pinned agents offline).
# ---------------------------------------------------------------------------


def test_host_atomic_replace_inside_account_dir_is_visible_via_bind_source(
    tmp_path: Path, home_redirect: Path
) -> None:
    # Arrange — boot a pinned agent's auth flow, then simulate the
    # host-side atomic-replace path that creds_sync._atomic_copy +
    # account_store.switch_account + claude_usage._refresh_access_token_at
    # all share: write .credentials.json.tmp, then os.replace onto the
    # snapshot. With a directory bind, opening
    # ``<bind_source>/.credentials.json`` after the replace MUST yield
    # the NEW token (NEW inode resolved via dir lookup). The legacy
    # single-file bind detached under exactly this sequence.
    now = time.time()
    snap = _write_snapshot(home_redirect, "alpha", now + 3_600, token="boot-token")
    cfg = _pinned_config(tmp_path / "wd", account="alpha")
    argv = auth_argv(cfg, state_dir=tmp_path / "state")
    bind_src = _extract_bind_source_for_target(argv, "/tmp/sac-claude")
    assert bind_src is not None, "auth_argv did not emit /tmp/sac-claude bind"
    bind_src_path = Path(bind_src)
    # Act — host atomic-replace: write tmp sibling, then os.replace.
    new_body = {
        "claudeAiOauth": {
            "accessToken": "refreshed-token-after-host-replace",
            "expiresAt": int((now + 86_400) * 1_000),
        }
    }
    tmp_file = snap.with_suffix(snap.suffix + ".tmp")
    tmp_file.write_text(json.dumps(new_body))
    os.replace(tmp_file, snap)
    # Assert — re-reading through the bind source's dir view picks up
    # the NEW content. Pre-fix file-bind would have read the orphan.
    container_visible = json.loads((bind_src_path / ".credentials.json").read_text())
    assert (
        container_visible["claudeAiOauth"]["accessToken"]
        == "refreshed-token-after-host-replace"
    )
