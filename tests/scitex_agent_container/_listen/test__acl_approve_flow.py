"""End-to-end tests for the ACL block/unblock flow (task #27).

Lead's design amendment (2026-06-01) replaced the held-message-replay
flow with a BLOCK / UNBLOCK primitive. This module covers:

* ``node_message_send`` on a cross-group ACL deny records ONE
  pending-prompt + pushes a receiver-facing prompt embedding BOTH
  the ``sac a2a unblock`` and ``sac a2a block`` CLI commands.
* Dedupe: subsequent denied attempts from the same (sender, target)
  pair while pending DO NOT re-emit the prompt (no flood).
* ``sac a2a unblock`` writes the grant + removes any block + clears
  the pending row.
* ``sac a2a block`` writes ``comms_blocks`` + clears the pending row.
* A blocked sender's future attempts are silently dropped — NO
  receiver push, NO approve-prompt re-fire.

No-mocks (PA-306): real on-disk state.db, real Starlette TestClient
+ real CliRunner. AAA markers, one assertion per test.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Iterator

import pytest
from click.testing import CliRunner
from starlette.testclient import TestClient

from scitex_agent_container._listen.server import create_app
from scitex_agent_container._runners import _session_state as _ss
from scitex_agent_container._state import registry as _reg
from scitex_agent_container._state import state_db
from scitex_agent_container._state.state_db_blocks import has_block
from scitex_agent_container._state.state_db_channel import list_undelivered
from scitex_agent_container._state.state_db_nodes import (
    has_grant,
    record_lineage,
)
from scitex_agent_container._state.state_db_pending_approval import (
    has_pending_prompt,
)

_TOKEN = "test-token-approve-flow"


@pytest.fixture
def isolated_state(tmp_path: Path) -> Iterator[Path]:
    db = tmp_path / "state.db"
    saved_env = os.environ.get("SCITEX_AGENT_CONTAINER_STATE_DB")
    saved_default = state_db.DEFAULT_DB_PATH
    saved_home = os.environ.get("HOME")
    saved_reg_const = _reg.REGISTRY_DIR
    saved_state_const = _ss.DEFAULT_STATE_ROOT
    os.environ["SCITEX_AGENT_CONTAINER_STATE_DB"] = str(db)
    state_db.DEFAULT_DB_PATH = db
    state_db.init_schema(db)
    os.environ["HOME"] = str(tmp_path)
    _reg.REGISTRY_DIR = tmp_path / "registry"
    _ss.DEFAULT_STATE_ROOT = tmp_path / "runtime"
    try:
        # Seed worker-a → root lineage. Under messaging DEFAULT-ALLOW
        # (operator 2026-07-03) a cross-group ``worker-a → lead`` send no
        # longer denies, so the receiver approve-prompt (grant-or-block on
        # a cross-group deny) no longer fires from a send. The surviving,
        # still-meaningful behaviour this file covers is the BLOCK /
        # UNBLOCK decision primitives + CLI, plus the pending-prompt clear
        # (pending seeded directly via ``record_pending_prompt``).
        record_lineage(child="worker-a", parent="root", db_path=db)
        yield db
    finally:
        state_db.DEFAULT_DB_PATH = saved_default
        _reg.REGISTRY_DIR = saved_reg_const
        _ss.DEFAULT_STATE_ROOT = saved_state_const
        if saved_env is None:
            os.environ.pop("SCITEX_AGENT_CONTAINER_STATE_DB", None)
        else:
            os.environ["SCITEX_AGENT_CONTAINER_STATE_DB"] = saved_env
        if saved_home is None:
            os.environ.pop("HOME", None)
        else:
            os.environ["HOME"] = saved_home


def _send_payload(sender: str, content: str = "hi") -> dict:
    return {
        "jsonrpc": "2.0",
        "id": "1",
        "method": "SendMessage",
        "params": {
            "message": {
                "message_id": "m1",
                "role": "ROLE_USER",
                "parts": [{"text": content}],
            },
            "metadata": {"from_agent": sender},
        },
    }


# ---------------------------------------------------------------------------
# Approve-prompt helpers (unit). Under messaging DEFAULT-ALLOW (operator
# 2026-07-03) a cross-group send no longer denies, so the receiver
# approve-prompt no longer fires from a send. The dedupe + prompt-content
# primitives remain and are exercised directly here so the block/unblock
# UX stays covered.
# ---------------------------------------------------------------------------


def test_record_pending_prompt_first_call_returns_true(
    isolated_state: Path,
) -> None:
    # Arrange
    from scitex_agent_container._state.state_db_pending_approval import (
        record_pending_prompt,
    )

    # Act
    first = record_pending_prompt(
        sender="worker-a", target="lead", db_path=isolated_state
    )
    # Assert
    assert first is True


def test_record_pending_prompt_dedupes_second_call(
    isolated_state: Path,
) -> None:
    # Arrange — the second record for the same pair must NOT re-prompt.
    from scitex_agent_container._state.state_db_pending_approval import (
        record_pending_prompt,
    )

    record_pending_prompt(sender="worker-a", target="lead", db_path=isolated_state)
    # Act
    second = record_pending_prompt(
        sender="worker-a", target="lead", db_path=isolated_state
    )
    # Assert
    assert second is False


def test_approval_prompt_content_embeds_unblock_command() -> None:
    # Arrange
    from scitex_agent_container._listen._acl_approve_prompt import (
        approval_prompt_content,
    )

    # Act
    content = approval_prompt_content("worker-a", "lead")
    # Assert
    assert "sac a2a unblock worker-a lead" in content


def test_approval_prompt_content_embeds_block_command() -> None:
    # Arrange
    from scitex_agent_container._listen._acl_approve_prompt import (
        approval_prompt_content,
    )

    # Act
    content = approval_prompt_content("worker-a", "lead")
    # Assert
    assert "sac a2a block worker-a lead" in content


def test_approval_prompt_body_does_not_leak_message_content() -> None:
    # Arrange — the prompt decides on identity, never the message body.
    from scitex_agent_container._listen._acl_approve_prompt import (
        _mint_approval_prompt,
    )

    # Act
    prompt = _mint_approval_prompt(target="lead", sender="worker-a")
    # Assert
    assert "SECRET-PAYLOAD" not in (prompt.get("content") or "")


# ---------------------------------------------------------------------------
# UNBLOCK decision — grant + remove block + clear pending
# ---------------------------------------------------------------------------


def test_unblock_writes_comms_grants_row(isolated_state: Path) -> None:
    # Arrange
    from scitex_agent_container._state.grant_flush import (
        unblock_and_clear_pending,
    )

    # Act
    unblock_and_clear_pending(sender="worker-a", target="lead")
    # Assert
    assert has_grant(sender="worker-a", target="lead", db_path=isolated_state)


def test_unblock_clears_the_pending_prompt(isolated_state: Path) -> None:
    # Arrange — seed a pending row directly, then unblock.
    from scitex_agent_container._state.grant_flush import (
        unblock_and_clear_pending,
    )
    from scitex_agent_container._state.state_db_pending_approval import (
        record_pending_prompt,
    )

    record_pending_prompt(sender="worker-a", target="lead", db_path=isolated_state)
    # Act
    unblock_and_clear_pending(sender="worker-a", target="lead")
    # Assert
    assert not has_pending_prompt(
        sender="worker-a", target="lead", db_path=isolated_state
    )


def test_unblock_removes_existing_block_row(isolated_state: Path) -> None:
    # Arrange — block first, then unblock. The unblock MUST remove
    # the block row (otherwise the block-precedence rule in
    # ``check_send_acl`` would still silently drop sends post-
    # "unblock", which is exactly the wrong UX).
    from scitex_agent_container._state.grant_flush import (
        block_and_clear_pending,
        unblock_and_clear_pending,
    )

    block_and_clear_pending(sender="worker-a", target="lead")
    # Act
    unblock_and_clear_pending(sender="worker-a", target="lead")
    # Assert
    assert not has_block(sender="worker-a", target="lead", db_path=isolated_state)


# ---------------------------------------------------------------------------
# BLOCK decision — comms_blocks row + clear pending + silent future denies
# ---------------------------------------------------------------------------


def test_block_writes_comms_blocks_row(isolated_state: Path) -> None:
    # Arrange
    from scitex_agent_container._state.grant_flush import (
        block_and_clear_pending,
    )

    # Act
    block_and_clear_pending(sender="worker-a", target="lead")
    # Assert
    assert has_block(sender="worker-a", target="lead", db_path=isolated_state)


def test_block_clears_the_pending_prompt(isolated_state: Path) -> None:
    # Arrange — seed a pending row directly, then block.
    from scitex_agent_container._state.grant_flush import (
        block_and_clear_pending,
    )
    from scitex_agent_container._state.state_db_pending_approval import (
        record_pending_prompt,
    )

    record_pending_prompt(sender="worker-a", target="lead", db_path=isolated_state)
    # Act
    block_and_clear_pending(sender="worker-a", target="lead")
    # Assert
    assert not has_pending_prompt(
        sender="worker-a", target="lead", db_path=isolated_state
    )


def test_blocked_send_emits_no_receiver_push(isolated_state: Path) -> None:
    # Arrange — block first, then attempt a send. The receiver
    # MUST see NOTHING (no denied_attempt, no approve-prompt, no
    # new prompt re-fire). The sender still gets 403 (next test).
    from scitex_agent_container._state.grant_flush import (
        block_and_clear_pending,
    )

    block_and_clear_pending(sender="worker-a", target="lead")
    app = create_app(token=_TOKEN)
    # Act
    with TestClient(app) as client:
        client.post(
            "/agents/lead/message:send",
            json=_send_payload("worker-a"),
            headers={"authorization": f"Bearer {_TOKEN}"},
        )
    rows = list_undelivered(target="lead", db_path=isolated_state)
    # Assert — no rows landed for the lead from this blocked send.
    assert rows == []


def test_blocked_send_still_returns_403_to_sender(
    isolated_state: Path,
) -> None:
    # Arrange — sender side learns their send did not land. The
    # silence is receiver-only; we are not in the business of
    # gaslighting senders that a delivered message vanished.
    from scitex_agent_container._state.grant_flush import (
        block_and_clear_pending,
    )

    block_and_clear_pending(sender="worker-a", target="lead")
    app = create_app(token=_TOKEN)
    # Act
    with TestClient(app) as client:
        r = client.post(
            "/agents/lead/message:send",
            json=_send_payload("worker-a"),
            headers={"authorization": f"Bearer {_TOKEN}"},
        )
    # Assert
    assert r.status_code == 403


# ---------------------------------------------------------------------------
# CLI verbs — sac a2a unblock + sac a2a block
# ---------------------------------------------------------------------------


def test_cli_unblock_writes_comms_grants(isolated_state: Path) -> None:
    # Arrange
    from scitex_agent_container.cli_pkg.a2a_group import a2a

    # Act
    CliRunner().invoke(a2a, ["unblock", "worker-a", "lead"])
    # Assert
    assert has_grant(sender="worker-a", target="lead", db_path=isolated_state)


def test_cli_block_writes_comms_blocks(isolated_state: Path) -> None:
    # Arrange
    from scitex_agent_container.cli_pkg.a2a_group import a2a

    # Act
    CliRunner().invoke(a2a, ["block", "worker-a", "lead"])
    # Assert
    assert has_block(sender="worker-a", target="lead", db_path=isolated_state)


def test_cli_grant_alias_still_unblocks(isolated_state: Path) -> None:
    # Arrange — legacy `sac a2a grant` MUST behave like unblock so
    # operator muscle memory keeps working.
    from scitex_agent_container.cli_pkg.a2a_group import a2a

    # Act
    CliRunner().invoke(a2a, ["grant", "worker-a", "lead"])
    # Assert
    assert has_grant(sender="worker-a", target="lead", db_path=isolated_state)
