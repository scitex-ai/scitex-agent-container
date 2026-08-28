"""Tests for ``_state.lead_inbox`` — agent→lead push helper (ADR-0013 Phase 1).

Three layers, all without mocks (PA-306):

* :func:`build_lead_envelope` shape (method, kind allow-list,
  required ``from_agent``, optional ``detail`` / ``conversation_id``).
* :func:`resolve_lead` reads the real ``lead:`` block via the SciTeX
  local-state config cascade; missing block surfaces as a
  ``LeadInboxError`` with an actionable message.
* :func:`push_to_lead` against a REAL ``sac listen`` Starlette app
  bound to a real loopback port. The helper's urllib POST traverses
  the kernel TCP stack, hits the real ``node_message_send`` handler,
  and the inbox event is read back from the real ``channel_events``
  durable table.

One assertion per test (PA-307).
"""

from __future__ import annotations

import contextlib
import os
import socket
import threading
from pathlib import Path

import pytest
import uvicorn

from scitex_agent_container._listen.peer_tokens import write_peer_token
from scitex_agent_container._listen.server import create_app
from scitex_agent_container._runners import _session_state as _ss
from scitex_agent_container._state import registry as _reg
from scitex_agent_container._state import state_db
from scitex_agent_container._state.host_config import LeadConfig
from scitex_agent_container._state.lead_inbox import (
    LEAD_EVENT_KINDS,
    LeadInboxError,
    build_lead_envelope,
    push_to_lead,
    resolve_lead,
)
from scitex_agent_container._state.state_db_channel import list_undelivered
from scitex_agent_container._state.state_db_nodes import record_lineage

TOKEN = "test-lead-inbox-token"


# ---------------------------------------------------------------------------
# build_lead_envelope — pure-function shape pins
# ---------------------------------------------------------------------------


def test_envelope_method_is_message_send() -> None:
    # Arrange
    env = build_lead_envelope(kind="done", summary="ok", from_agent="alice")
    # Act
    method = env["method"]
    # Assert
    assert method == "message/send"


def test_envelope_carries_kind_under_metadata() -> None:
    # Arrange
    env = build_lead_envelope(kind="blocker", summary="x", from_agent="alice")
    # Act
    kind = env["params"]["metadata"]["kind"]
    # Assert
    assert kind == "blocker"


def test_envelope_carries_from_agent_under_metadata() -> None:
    # Arrange
    env = build_lead_envelope(kind="status", summary="x", from_agent="bob")
    # Act
    sender = env["params"]["metadata"]["from_agent"]
    # Assert
    assert sender == "bob"


def test_envelope_summary_lands_in_message_parts_text() -> None:
    # Arrange
    env = build_lead_envelope(
        kind="status", summary="phase 2 of 4", from_agent="alice"
    )
    # Act
    text = env["params"]["message"]["parts"][0]["text"]
    # Assert
    assert text == "phase 2 of 4"


def test_envelope_omits_detail_when_unset() -> None:
    # Arrange
    env = build_lead_envelope(kind="done", summary="ok", from_agent="alice")
    # Act
    meta = env["params"]["metadata"]
    # Assert
    assert "detail" not in meta


def test_envelope_includes_detail_when_set() -> None:
    # Arrange
    env = build_lead_envelope(
        kind="blocker",
        summary="creds expired",
        from_agent="alice",
        detail="OAuth token expired 12s ago; need rotation",
    )
    # Act
    detail = env["params"]["metadata"]["detail"]
    # Assert
    assert detail == "OAuth token expired 12s ago; need rotation"


def test_envelope_includes_conversation_id_when_set() -> None:
    # Arrange
    env = build_lead_envelope(
        kind="done",
        summary="ok",
        from_agent="alice",
        conversation_id="thread-42",
    )
    # Act
    cid = env["params"]["metadata"]["conversation_id"]
    # Assert
    assert cid == "thread-42"


def test_envelope_rejects_unknown_kind() -> None:
    # Arrange
    kwargs = dict(kind="frobnicate", summary="x", from_agent="alice")
    # Act
    # Assert — pytest.raises is the assertion (TQ007: one per test).
    with pytest.raises(LeadInboxError, match="event kind"):
        build_lead_envelope(**kwargs)


def test_envelope_rejects_empty_from_agent() -> None:
    # Arrange
    kwargs = dict(kind="done", summary="x", from_agent="")
    # Act
    # Assert — pytest.raises is the assertion (TQ007: one per test).
    with pytest.raises(LeadInboxError, match="from_agent"):
        build_lead_envelope(**kwargs)


def test_envelope_rejects_non_string_summary() -> None:
    # Arrange
    kwargs = dict(kind="done", summary=42, from_agent="alice")
    # Act
    # Assert — pytest.raises is the assertion (TQ007: one per test).
    with pytest.raises(LeadInboxError, match="summary"):
        build_lead_envelope(**kwargs)  # type: ignore[arg-type]


def test_lead_event_kinds_exposes_three_kinds() -> None:
    # Arrange — the public allow-list is the source of truth the CLI
    # ``click.Choice`` pulls from; pin it so a future widening / rename
    # is a deliberate API change reviewable in diff.
    expected = ("done", "blocker", "status")
    # Act
    out = LEAD_EVENT_KINDS
    # Assert
    assert out == expected


# ---------------------------------------------------------------------------
# resolve_lead — config-driven address resolution
# ---------------------------------------------------------------------------


def test_resolve_lead_returns_configured_address(
    tmp_path: Path, env_save_restore
) -> None:
    # Arrange
    cfg = tmp_path / "config.yaml"
    cfg.write_text("lead:\n  name: lead\n  host: mba\n  a2a_port: 8642\n")
    env_save_restore.set("SCITEX_AGENT_CONTAINER_CONFIG", str(cfg))
    # Act
    out = resolve_lead()
    # Assert
    assert out == LeadConfig(name="lead", host="mba", a2a_port=8642)


def test_resolve_lead_raises_when_block_missing(
    tmp_path: Path, env_save_restore
) -> None:
    # Arrange
    cfg = tmp_path / "config.yaml"
    cfg.write_text("peers: {}\n")
    env_save_restore.set("SCITEX_AGENT_CONTAINER_CONFIG", str(cfg))
    # Act
    # Assert — pytest.raises is the assertion (TQ007: one per test).
    with pytest.raises(LeadInboxError, match="no lead inbox configured"):
        resolve_lead()


# ---------------------------------------------------------------------------
# push_to_lead — REAL HTTP roundtrip against a real sac listen
# ---------------------------------------------------------------------------


def _free_port() -> int:
    """Bind ephemeral, return the assigned port."""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


@contextlib.contextmanager
def _run_listen(app, port: int):
    """Run uvicorn on a thread; ready when the bound port accepts a connect."""
    config = uvicorn.Config(
        app,
        host="127.0.0.1",
        port=port,
        log_level="warning",
        lifespan="on",
    )
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    try:
        # uvicorn's ``server.started`` flips after .startup() finishes;
        # a real TCP connect is the portable signal.
        for _ in range(50):
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                    break
            except OSError:
                threading.Event().wait(0.1)
        yield
    finally:
        server.should_exit = True
        thread.join(timeout=5.0)


@pytest.fixture
def lead_env(tmp_path: Path, env_save_restore):
    """Isolated state.db + registry dirs + peer-tokens + config.yaml.

    Mirrors ``test_server.py``'s ``isolated_env`` shape (HOME under
    tmp_path so peer-tokens land beneath it, plus state.db pinned to
    tmp). Yields a dict the per-test setup uses to wire the lead
    address into ``config.yaml``.
    """
    saved_home = os.environ.get("HOME")
    saved_db_env = os.environ.get("SCITEX_AGENT_CONTAINER_STATE_DB")
    saved_reg_env = os.environ.get("SCITEX_AGENT_CONTAINER_REGISTRY_DIR")
    saved_run_env = os.environ.get("SCITEX_AGENT_CONTAINER_RUNTIME_DIR")
    saved_reg_const = _reg.REGISTRY_DIR
    saved_state_const = _ss.DEFAULT_STATE_ROOT
    saved_db_const = state_db.DEFAULT_DB_PATH

    db = tmp_path / "state.db"
    os.environ["HOME"] = str(tmp_path)
    os.environ["SCITEX_AGENT_CONTAINER_STATE_DB"] = str(db)
    os.environ["SCITEX_AGENT_CONTAINER_REGISTRY_DIR"] = str(tmp_path / "registry")
    os.environ["SCITEX_AGENT_CONTAINER_RUNTIME_DIR"] = str(tmp_path / "runtime")
    state_db.DEFAULT_DB_PATH = db
    _reg.REGISTRY_DIR = tmp_path / "registry"
    _ss.DEFAULT_STATE_ROOT = tmp_path / "runtime"
    state_db.init_schema(db)

    # Lead config — production reads this via the env-routed config.yaml.
    # Each test that actually pushes rewrites ``a2a_port`` with the
    # bound uvicorn port (a placeholder lives here so ``resolve_lead``
    # succeeds even in tests that don't start uvicorn).
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        "lead:\n"
        "  name: lead\n"
        "  host: 127.0.0.1\n"
        "  a2a_port: 1\n"
    )
    env_save_restore.set("SCITEX_AGENT_CONTAINER_CONFIG", str(cfg))

    try:
        yield {"db": db, "cfg": cfg, "tmp_path": tmp_path}
    finally:
        state_db.DEFAULT_DB_PATH = saved_db_const
        _reg.REGISTRY_DIR = saved_reg_const
        _ss.DEFAULT_STATE_ROOT = saved_state_const
        for k, v in (
            ("HOME", saved_home),
            ("SCITEX_AGENT_CONTAINER_STATE_DB", saved_db_env),
            ("SCITEX_AGENT_CONTAINER_REGISTRY_DIR", saved_reg_env),
            ("SCITEX_AGENT_CONTAINER_RUNTIME_DIR", saved_run_env),
        ):
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def _setup_lead_at_port(lead_env, port: int) -> None:
    """Rewrite config.yaml so ``lead.a2a_port`` matches the bound port.

    Also registers the lead's per-host bearer in the peer-tokens
    registry — same mechanism the cross-host forwarder uses (the
    push helper authenticates with the LEAD host's bearer, not its
    own).
    """
    cfg = lead_env["cfg"]
    cfg.write_text(
        "lead:\n  name: lead\n  host: 127.0.0.1\n"
        f"  a2a_port: {port}\n"
    )
    write_peer_token(peer_host="127.0.0.1", token=TOKEN)


def _push_kind(lead_env, *, kind: str, summary: str, from_agent: str) -> dict:
    """Stand up sac listen on a free port, push one event, return server reply."""
    port = _free_port()
    _setup_lead_at_port(lead_env, port)
    # Lead must be reachable for ACL — same-group send.
    record_lineage(child=from_agent, parent="root")
    record_lineage(child="lead", parent="root")

    app = create_app(token=TOKEN, local_host="127.0.0.1")
    with _run_listen(app, port):
        return push_to_lead(
            kind=kind,
            summary=summary,
            from_agent=from_agent,
            timeout_s=5.0,
        )


def test_push_to_lead_returns_server_msg_id(lead_env, pg_schema: str) -> None:
    # Arrange
    kind = "done"
    # Act
    out = _push_kind(
        lead_env, kind=kind, summary="PR merged", from_agent="alice"
    )
    # Assert
    assert isinstance(out.get("msg_id"), str) and out["msg_id"]


def test_push_to_lead_persists_event_with_kind(lead_env, pg_schema: str) -> None:
    # Arrange — push then read back from the durable table.
    kind = "blocker"
    # Act
    _push_kind(
        lead_env, kind=kind, summary="creds expired", from_agent="alice"
    )
    rows = list_undelivered(target="lead")
    # Assert — the persisted event carries the typed kind so a fresh
    # subscriber (or Phase 2's registry consumer) sees it.
    assert rows and rows[0]["event"].get("kind") == "blocker"


def test_push_to_lead_persists_summary_as_content(lead_env, pg_schema: str) -> None:
    # Arrange
    summary = "phase 2/4"
    # Act
    _push_kind(lead_env, kind="status", summary=summary, from_agent="alice")
    rows = list_undelivered(target="lead")
    # Assert
    assert rows and rows[0]["event"].get("content") == "phase 2/4"


def test_push_to_lead_persists_from_agent(lead_env, pg_schema: str) -> None:
    # Arrange
    sender = "bob"
    # Act
    _push_kind(lead_env, kind="done", summary="ok", from_agent=sender)
    rows = list_undelivered(target="lead")
    # Assert
    assert rows and rows[0]["event"].get("from_agent") == "bob"


def test_push_to_lead_loud_on_missing_peer_token(lead_env) -> None:
    # Arrange — config points at a real port but the per-host token
    # is NOT registered. The helper must refuse loudly.
    port = _free_port()
    cfg = lead_env["cfg"]
    cfg.write_text(
        f"lead:\n  name: lead\n  host: 127.0.0.1\n  a2a_port: {port}\n",
    )
    app = create_app(token=TOKEN, local_host="127.0.0.1")
    # Act
    # Assert — pytest.raises is the assertion (TQ007: one per test).
    with _run_listen(app, port), pytest.raises(
        LeadInboxError, match="peer token"
    ):
        push_to_lead(
            kind="done",
            summary="x",
            from_agent="alice",
            timeout_s=2.0,
        )


def test_push_to_lead_loud_on_unreachable_lead(lead_env) -> None:
    # Arrange — register the token so we get past the peer-token gate,
    # then point at a port nothing is bound to.
    port = _free_port()
    _setup_lead_at_port(lead_env, port)
    # Act
    # Assert — no server started; pytest.raises is the assertion (TQ007).
    with pytest.raises(LeadInboxError, match="unreachable"):
        push_to_lead(
            kind="status",
            summary="x",
            from_agent="alice",
            timeout_s=2.0,
        )


# ---------------------------------------------------------------------------
# REGRESSION GUARD (ADR-0013 Phase 1 — operator-mandated)
#
# The exact path "agent POSTs a completion event → it lands in the lead
# inbox over the real ``sac listen`` HTTP surface" silently regressed in
# the past because no single test exercised the full end-to-end shape
# at once. This test pins the entire path: real uvicorn on a loopback
# port, real urllib POST through the kernel TCP stack, real bearer
# auth, real ACL gate, real persist + publish on the lead. Without
# this test the PR cannot be merged.
# ---------------------------------------------------------------------------


def test_regression_agent_completion_event_lands_in_lead_inbox(lead_env, pg_schema: str) -> None:
    # Arrange — real lead listen on a free port; real peer-token; real
    # same-group lineage so the ACL admits the send. The agent identity
    # is ``alice``; the lead identity is ``lead`` (matches lead_env's
    # config.yaml).
    port = _free_port()
    _setup_lead_at_port(lead_env, port)
    record_lineage(child="alice", parent="root")
    record_lineage(child="lead", parent="root")
    app = create_app(token=TOKEN, local_host="127.0.0.1")

    # Act — agent pushes a completion event over the real HTTP stack.
    with _run_listen(app, port):
        reply = push_to_lead(
            kind="done",
            summary="PR #224 merged",
            from_agent="alice",
            detail="all checks green",
            timeout_s=5.0,
        )
    rows = list_undelivered(target="lead")

    # Assert — the completion event must be readable from the lead's
    # durable inbox table with sender, kind, summary, and detail all
    # preserved. ``msg_id`` from the server reply must match the row's
    # msg_id, proving the persisted row is THIS push (not a stale row
    # from a leaked fixture). One compound assert covers the full
    # regression contract (TQ007: one assertion per test). The leading
    # ``rows`` clause short-circuits the rest when the inbox is empty,
    # so the failure message names that case without a second assert.
    event = rows[0]["event"] if rows else {}
    assert (
        rows
        and event.get("kind") == "done"
        and event.get("from_agent") == "alice"
        and event.get("content") == "PR #224 merged"
        and event.get("msg_id") == reply.get("msg_id")
    ), (
        f"completion event did not land in lead inbox as expected: "
        f"rows={rows!r} reply={reply!r}"
    )
