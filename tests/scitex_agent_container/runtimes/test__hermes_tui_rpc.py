"""Native Hermes JSON-RPC delivery tests using an in-memory transport fake."""

from __future__ import annotations

import json
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace

import pytest

from scitex_agent_container.runtimes import _hermes_tui_rpc as rpc_module
from scitex_agent_container.runtimes._hermes_context_rpc import (
    complete_pending_transition,
    reconcile_pending_transition,
    replace_session_from_handoff,
    stored_session_for_title,
)
from scitex_agent_container.runtimes._hermes_tui_owner import GATEWAY_FILE
from scitex_agent_container.runtimes._hermes_tui_rpc import (
    HermesTuiRpcError,
    _select_session,
    active_sessions,
    branch_visible_history,
    clear_heartbeat,
    clear_heartbeat_for_session,
    compress_session,
    execute_slash_command,
    gateway_detailed_health,
    import_fork_seed,
    observe_turn_activity,
    observe_turn_outcome,
    observe_turn_progress,
    submit_turn,
    submit_visible_turn,
)


def test_detailed_health_is_authenticated_and_returns_readiness_json(tmp_path):
    # Arrange
    (tmp_path / GATEWAY_FILE).write_text(json.dumps({"port": 43123}), encoding="utf-8")
    (tmp_path / "hermes-api.key").write_text("secret-token-1234", encoding="utf-8")
    seen = []

    def open_(request, timeout):
        seen.append((request, timeout))
        return nullcontext(
            SimpleNamespace(status=200, read=lambda: b'{"sessions":[],"total":0}')
        )

    # Act
    payload = gateway_detailed_health(tmp_path, urlopen_fn=open_)

    # Assert
    assert (
        payload["status"],
        seen[0][0].full_url,
        seen[0][0].get_header("Authorization"),
    ) == (
        "ok",
        "http://127.0.0.1:43123/api/sessions?limit=1",
        "Bearer secret-token-1234",
    )


def test_detailed_health_rejects_http_200_without_session_store_shape(tmp_path):
    # Arrange
    (tmp_path / GATEWAY_FILE).write_text(json.dumps({"port": 43123}), encoding="utf-8")
    (tmp_path / "hermes-api.key").write_text("secret-token-1234", encoding="utf-8")

    def open_(_request, timeout):
        del timeout
        return nullcontext(
            SimpleNamespace(
                status=200,
                read=lambda: b'{"detail":"storage unavailable"}',
            )
        )

    # Act
    try:
        gateway_detailed_health(tmp_path, urlopen_fn=open_)
    except (
        HermesTuiRpcError
    ) as exc:  # stx-allow: test-capture (reason: STX-TQ002 splits Act from Assert.)
        observed = str(exc)
    else:
        observed = ""

    # Assert
    assert "malformed response" in observed


def test_stored_session_lookup_joins_exact_title_to_management_record(tmp_path):
    # Arrange
    _gateway_files(tmp_path)

    class ListSocket:
        def __init__(self):
            self.sent = []

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def send(self, raw):
            self.sent.append(json.loads(raw))

        def recv(self):
            request = self.sent[-1]
            if request["method"] != "session.list":
                raise RuntimeError(request["method"])
            result = {
                "sessions": [
                    {
                        "id": "root-1",
                        "resolved_id": "tip-2",
                        "title": "sac:hub:qwen",
                        "started_at": 100.0,
                    }
                ]
            }
            return json.dumps({"jsonrpc": "2.0", "id": request["id"], "result": result})

    socket = ListSocket()
    seen = []

    def open_(request, timeout):
        seen.append((request.full_url, timeout))
        return nullcontext(
            SimpleNamespace(
                status=200,
                read=lambda *_args: json.dumps(
                    {
                        "id": "tip-2",
                        "started_at": 100.0,
                        "compression_failure_error": "summary timed out",
                    }
                ).encode(),
            )
        )

    # Act
    record = stored_session_for_title(
        tmp_path,
        "sac:hub:qwen",
        connect_fn=lambda *args, **kwargs: socket,
        urlopen_fn=open_,
    )
    # Assert
    assert (
        record,
        seen[0][0].endswith("/api/sessions/tip-2"),
        [request["method"] for request in socket.sent],
    ) == (
        {
            "id": "tip-2",
            "started_at": 100.0,
            "compression_failure_error": "summary timed out",
        },
        True,
        ["session.list"],
    )


def test_handoff_rotation_proves_nonce_before_closing_old_session(tmp_path):
    # Arrange
    _gateway_files(tmp_path)
    handoff = tmp_path / "handoff.json"
    handoff.write_text('{"task_id":"card"}', encoding="utf-8")

    class RotationSocket:
        def __init__(self):
            self.sent = []
            self.replays = 0

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def send(self, raw):
            self.sent.append(json.loads(raw))

        def recv(self):
            request = self.sent[-1]
            method = request["method"]
            if method == "session.create":
                result = {
                    "session_id": "fresh-live",
                    "stored_session_id": "fresh-stored",
                }
            elif method == "session.events.since":
                self.replays += 1
                result = {
                    "events": (
                        []
                        if self.replays == 1
                        else [
                            {
                                "type": "message.complete",
                                "payload": {"status": "complete"},
                            }
                        ]
                    ),
                    "latest_seq": 5 + self.replays,
                    "epoch": "epoch-1",
                }
            elif method == "prompt.submit":
                result = {"status": "streaming"}
            elif method == "session.history":
                result = {
                    "messages": [
                        {"role": "assistant", "content": "HANDOFF_READY:nonce-123"}
                    ]
                }
            elif method == "session.close":
                result = {"closed": True}
            else:  # pragma: no cover - any extra mutation is the failure
                raise AssertionError(method)
            return json.dumps({"jsonrpc": "2.0", "id": request["id"], "result": result})

    socket = RotationSocket()
    # Act
    session_id = replace_session_from_handoff(
        tmp_path,
        agent_name="hub",
        workdir=Path("/repo"),
        handoff_path=handoff,
        nonce="nonce-123",
        old_session_id="old-live",
        model="qwen3-coder",
        provider="custom:sac-vllm",
        connect_fn=lambda *args, **kwargs: socket,
        sleep_fn=lambda _seconds: None,
    )
    # Assert
    create_params = socket.sent[0]["params"]
    assert (
        session_id,
        [request["method"] for request in socket.sent],
        (create_params["model"], create_params["provider"]),
        socket.sent[-1]["params"],
        (tmp_path / "hermes-context-transition.json").exists(),
    ) == (
        "fresh-stored",
        [
            "session.create",
            "session.events.since",
            "prompt.submit",
            "session.events.since",
            "session.history",
            "session.close",
        ],
        ("qwen3-coder", "custom:sac-vllm"),
        {"session_id": "old-live"},
        True,
    )


def test_lost_close_reply_reconciles_old_absent_and_keeps_fresh(tmp_path):
    # Arrange
    _gateway_files(tmp_path)
    handoff = tmp_path / "handoff.json"
    handoff.write_text("{}", encoding="utf-8")

    class Socket:
        def __init__(self, reconcile=False):
            self.sent = []
            self.replays = 0
            self.reconcile = reconcile

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def send(self, raw):
            self.sent.append(json.loads(raw))

        def recv(self):
            request = self.sent[-1]
            method = request["method"]
            if self.reconcile:
                if method != "session.active_list":
                    raise AssertionError(method)
                result = {"sessions": [{"id": "fresh-live", "status": "idle"}]}
            elif method == "session.create":
                result = {"session_id": "fresh-live", "stored_session_id": "fresh-stored"}
            elif method == "session.events.since":
                self.replays += 1
                result = {
                    "events": (
                        []
                        if self.replays == 1
                        else [{"type": "message.complete", "payload": {"status": "complete"}}]
                    ),
                    "latest_seq": self.replays,
                    "epoch": "epoch",
                }
            elif method == "prompt.submit":
                result = {"status": "streaming"}
            elif method == "session.history":
                result = {
                    "messages": [
                        {"role": "assistant", "content": "HANDOFF_READY:nonce-123"}
                    ]
                }
            elif method == "session.close":
                raise ConnectionError("reply lost after server-side close")
            else:
                raise AssertionError(method)
            return json.dumps({"jsonrpc": "2.0", "id": request["id"], "result": result})

    sockets = iter((Socket(), Socket(reconcile=True)))
    # Act
    replacement = replace_session_from_handoff(
        tmp_path,
        agent_name="hub",
        workdir=Path("/repo"),
        handoff_path=handoff,
        nonce="nonce-123",
        old_session_id="old-live",
        model="qwen3-coder",
        provider="custom:sac-vllm",
        connect_fn=lambda *_args, **_kwargs: next(sockets),
        sleep_fn=lambda _seconds: None,
    )
    # Assert
    assert replacement == "fresh-stored"


def test_startup_reconciliation_rolls_back_fresh_when_old_is_live(tmp_path):
    # Arrange
    _gateway_files(tmp_path)
    journal = tmp_path / "hermes-context-transition.json"
    journal.write_text(
        json.dumps(
            {
                "fresh_title": "sac:hub:handoff:nonce123",
                "old_session_id": "old-live",
                "phase": "proven",
            }
        ),
        encoding="utf-8",
    )

    class Socket:
        def __init__(self, listing):
            self.sent = []
            self.listing = listing

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def send(self, raw):
            self.sent.append(json.loads(raw))

        def recv(self):
            request = self.sent[-1]
            result = self.listing if request["method"] == "session.active_list" else {"closed": True}
            return json.dumps({"jsonrpc": "2.0", "id": request["id"], "result": result})

    listing_socket = Socket(
        {
            "sessions": [
                {"id": "old-live", "title": "sac:hub"},
                {"id": "fresh-live", "title": "sac:hub:handoff:nonce123"},
            ]
        }
    )
    close_socket = Socket({})
    sockets = iter((listing_socket, close_socket))
    # Act
    replacement = reconcile_pending_transition(
        tmp_path, connect_fn=lambda *_args, **_kwargs: next(sockets)
    )
    # Assert
    assert (
        journal.exists(),
        close_socket.sent[-1]["params"],
        replacement,
    ) == (False, {"session_id": "fresh-live"}, "")


def test_startup_reconciliation_returns_committed_fresh_stored_id(tmp_path):
    # Arrange
    _gateway_files(tmp_path)
    journal = tmp_path / "hermes-context-transition.json"
    journal.write_text(
        json.dumps(
            {
                "fresh_stored_id": "fresh-stored",
                "fresh_title": "sac:hub:handoff:nonce123",
                "old_session_id": "old-live",
                "phase": "proven",
            }
        ),
        encoding="utf-8",
    )

    class Socket:
        def __init__(self):
            self.sent = []

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def send(self, raw):
            self.sent.append(json.loads(raw))

        def recv(self):
            request = self.sent[-1]
            result = {
                "sessions": [
                    {
                        "id": "fresh-live",
                        "session_key": "fresh-stored",
                        "title": "sac:hub:handoff:nonce123",
                    }
                ]
            }
            return json.dumps({"jsonrpc": "2.0", "id": request["id"], "result": result})

    # Act
    replacement = reconcile_pending_transition(
        tmp_path, connect_fn=lambda *_args, **_kwargs: Socket()
    )
    remained_until_attach = journal.exists()
    complete_pending_transition(
        tmp_path, {"id": "fresh-live", "session_key": "fresh-stored"}
    )
    # Assert
    assert (replacement, remained_until_attach, journal.exists()) == (
        "fresh-stored",
        True,
        False,
    )


def test_missing_nonce_proof_never_closes_old_session(tmp_path):
    # Arrange
    _gateway_files(tmp_path)
    handoff = tmp_path / "handoff.json"
    handoff.write_text("{}", encoding="utf-8")

    class MissingProofSocket:
        def __init__(self):
            self.sent = []
            self.replays = 0

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def send(self, raw):
            self.sent.append(json.loads(raw))

        def recv(self):
            request = self.sent[-1]
            method = request["method"]
            if method == "session.create":
                result = {"session_id": "fresh", "stored_session_id": "stored"}
            elif method == "session.events.since":
                self.replays += 1
                result = {
                    "events": (
                        []
                        if self.replays == 1
                        else [
                            {
                                "type": "message.complete",
                                "payload": {"status": "complete"},
                            }
                        ]
                    ),
                    "latest_seq": self.replays,
                    "epoch": "epoch",
                }
            elif method == "prompt.submit":
                result = {"status": "streaming"}
            elif method == "session.history":
                result = {
                    "messages": [
                        {
                            "role": "assistant",
                            "content": "prefix HANDOFF_READY:nonce-123 suffix",
                        }
                    ]
                }
            elif method == "session.close":
                result = {"closed": True}
            else:
                raise AssertionError(method)
            return json.dumps({"jsonrpc": "2.0", "id": request["id"], "result": result})

    socket = MissingProofSocket()

    def action():
        return replace_session_from_handoff(
            tmp_path,
            agent_name="hub",
            workdir=Path("/repo"),
            handoff_path=handoff,
            nonce="nonce-123",
            old_session_id="old-live",
            model="qwen3-coder",
            provider="custom:sac-vllm",
            connect_fn=lambda *args, **kwargs: socket,
            sleep_fn=lambda _seconds: None,
        )

    # Act
    try:
        action()
    except HermesTuiRpcError as exc:
        error = str(exc)
    else:
        error = ""
    closed = [
        request["params"]["session_id"]
        for request in socket.sent
        if request["method"] == "session.close"
    ]
    # Assert
    assert ("without the required nonce" in error, closed) == (True, ["fresh"])


class _Socket:
    def __init__(self, status: str | None = None):
        self.sent = []
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def send(self, raw):
        self.sent.append(json.loads(raw))

    def recv(self):
        request = self.sent[-1]
        method = request["method"]
        result = {
            "session.active_list": {
                "sessions": [
                    {
                        "id": "live-1",
                        "title": "sac:hub",
                        "message_count": 12,
                        "last_active": 44.0,
                        **({"status": self.status} if self.status else {}),
                    }
                ]
            },
            "session.activate": {"id": "live-1"},
            "session.events.since": {
                "events": [],
                "latest_seq": 17,
                "truncated": False,
                "epoch": "epoch-1",
                "open_requests": [],
            },
            "prompt.submit": {"status": "steered"},
            "session.steer": {"status": "queued", "text": "act now"},
        }[method]
        return json.dumps({"jsonrpc": "2.0", "id": request["id"], "result": result})


class _VisibleSocket:
    def __init__(
        self,
        *,
        submit_status: str,
        projections: list[dict],
        session_status: str = "idle",
    ):
        self.sent = []
        self.submit_status = submit_status
        self.projections = iter(projections)
        self.session_status = session_status

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def send(self, raw):
        self.sent.append(json.loads(raw))

    def recv(self):
        request = self.sent[-1]
        method = request["method"]
        if method == "session.active_list":
            result = {
                "sessions": [
                    {
                        "id": "live-1",
                        "title": "sac:hub",
                        "status": self.session_status,
                    }
                ]
            }
        elif method == "session.activate":
            result = next(self.projections)
            result.setdefault("session_key", "stored-1")
        elif method == "prompt.submit":
            result = {"status": self.submit_status}
        elif method == "session.steer":
            result = {"status": "queued", "text": request["params"]["text"]}
        else:  # pragma: no cover - a new RPC is itself a test failure
            raise AssertionError(method)
        return json.dumps({"jsonrpc": "2.0", "id": request["id"], "result": result})


class _ClarifySocket:
    def __init__(self, *, pending=True, malformed=False):
        self.sent = []
        self.pending = pending
        self.malformed = malformed
        self.answers = {}

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def send(self, raw):
        self.sent.append(json.loads(raw))

    def recv(self):
        request = self.sent[-1]
        method = request["method"]
        if method == "session.active_list":
            result = {
                "sessions": [
                    {
                        "id": "live-clarify",
                        "title": "sac:hub",
                        "status": "waiting" if self.pending else "idle",
                    }
                ]
            }
        elif method == "session.activate":
            result = {"session_id": "live-clarify", "status": "waiting"}
            if self.pending:
                choices = "not-a-list" if self.malformed else ["Yes", "No"]
                result["pending_clarify"] = {
                    "request_id": "req-1",
                    "questions": [
                        {
                            "qid": "q0",
                            "question": "Fix the footer?",
                            "choices": choices,
                            "multi_select": False,
                        },
                        {
                            "qid": "q1",
                            "question": "How should auth be audited?",
                            "choices": ["vault", "public", "route"],
                            "multi_select": False,
                        },
                    ],
                }
        elif method == "clarify.respond":
            params = request["params"]
            self.answers[params["question_id"]] = params["answer"]
            remaining = [qid for qid in ("q0", "q1") if qid not in self.answers]
            if not remaining:
                self.pending = False
            result = {"status": "ok", "remaining": remaining}
        else:  # pragma: no cover - a new RPC is itself a test failure
            raise AssertionError(method)
        return json.dumps({"jsonrpc": "2.0", "id": request["id"], "result": result})


def test_observe_pending_clarification_returns_typed_questions(tmp_path):
    # Arrange
    _gateway_files(tmp_path)
    socket = _ClarifySocket()

    # Act
    pending = rpc_module.observe_pending_clarification(
        tmp_path, "hub", connect_fn=lambda *args, **kwargs: socket
    )

    # Assert
    assert (
        pending.request_id,
        pending.session_id,
        [(q.qid, q.question, q.choices, q.multi_select) for q in pending.questions],
    ) == (
        "req-1",
        "live-clarify",
        [
            ("q0", "Fix the footer?", ("Yes", "No"), False),
            ("q1", "How should auth be audited?", ("vault", "public", "route"), False),
        ],
    )


def test_observe_pending_clarification_returns_none_when_session_is_not_waiting(
    tmp_path,
):
    # Arrange
    _gateway_files(tmp_path)
    socket = _ClarifySocket(pending=False)

    # Act
    pending = rpc_module.observe_pending_clarification(
        tmp_path, "hub", connect_fn=lambda *args, **kwargs: socket
    )

    # Assert
    assert pending is None


def test_observe_pending_clarification_rejects_malformed_question(tmp_path):
    # Arrange
    _gateway_files(tmp_path)
    socket = _ClarifySocket(malformed=True)

    # Act
    caught = pytest.raises(HermesTuiRpcError, match="malformed")

    # Assert
    with caught:
        rpc_module.observe_pending_clarification(
            tmp_path, "hub", connect_fn=lambda *args, **kwargs: socket
        )


def test_respond_to_pending_clarification_uses_native_rpc_and_proves_closure(
    tmp_path,
):
    # Arrange
    _gateway_files(tmp_path)
    socket = _ClarifySocket()

    # Act
    receipt = rpc_module.respond_to_pending_clarification(
        tmp_path,
        "hub",
        request_id="req-1",
        answers={"q0": "Yes", "q1": "route"},
        connect_fn=lambda *args, **kwargs: socket,
    )

    # Assert
    assert (
        receipt.request_id,
        receipt.session_id,
        receipt.answered_qids,
        [r["method"] for r in socket.sent],
    ) == (
        "req-1",
        "live-clarify",
        ("q0", "q1"),
        [
            "session.active_list",
            "session.activate",
            "clarify.respond",
            "clarify.respond",
            "session.activate",
        ],
    )


def test_respond_to_pending_clarification_refuses_stale_request_id(tmp_path):
    # Arrange
    _gateway_files(tmp_path)
    socket = _ClarifySocket()

    # Act
    caught = pytest.raises(HermesTuiRpcError, match="request changed")

    # Assert
    with caught:
        rpc_module.respond_to_pending_clarification(
            tmp_path,
            "hub",
            request_id="stale-request",
            answers={"q0": "Yes", "q1": "route"},
            connect_fn=lambda *args, **kwargs: socket,
        )


def test_stale_clarification_request_does_not_submit_any_answer(tmp_path):
    # Arrange
    _gateway_files(tmp_path)
    socket = _ClarifySocket()

    # Act
    try:
        rpc_module.respond_to_pending_clarification(
            tmp_path,
            "hub",
            request_id="stale-request",
            answers={"q0": "Yes", "q1": "route"},
            connect_fn=lambda *args, **kwargs: socket,
        )
    except HermesTuiRpcError:
        pass

    # Assert
    assert [r["method"] for r in socket.sent] == [
        "session.active_list",
        "session.activate",
    ]


class _SearchResponse:
    def __init__(self, payload: dict):
        self.encoded = json.dumps(payload).encode()
        self.read_sizes = []
        self.requests = []

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def read(self, size):
        self.read_sizes.append(size)
        return self.encoded


def _search(payload=None):
    response = _SearchResponse(payload or {"results": []})

    def open_search(request, **kwargs):
        response.requests.append((request, kwargs))
        return response

    return response, open_search


class _ControlSocket(_Socket):
    def __init__(self, heartbeat):
        super().__init__()
        self.heartbeat = heartbeat

    def recv(self):
        request = self.sent[-1]
        if request["method"] == "session.active_list":
            result = {"sessions": [{"id": "live-1", "title": "sac:hub"}]}
        elif request["method"] == "session.control":
            result = {"control": {"heartbeat": self.heartbeat}}
        else:  # pragma: no cover - a new RPC is itself a test failure
            raise AssertionError(request["method"])
        return json.dumps({"jsonrpc": "2.0", "id": request["id"], "result": result})


class _CompressionSocket(_Socket):
    def __init__(self, *, status="idle", result=None):
        super().__init__(status=status)
        self.result = result or {
            "status": "compressed",
            "before_tokens": 663_402,
            "after_tokens": 91_000,
            "before_messages": 1324,
            "after_messages": 87,
            "info": {
                "usage": {
                    "context_used": 91_000,
                    "context_max": 1_000_000,
                    "context_source": "provider_usage",
                    "compressions": 1,
                }
            },
        }

    def recv(self):
        request = self.sent[-1]
        if request["method"] == "session.compress":
            result = self.result
            return json.dumps({"jsonrpc": "2.0", "id": request["id"], "result": result})
        return super().recv()


def test_compress_session_uses_native_rpc_and_returns_telemetry(tmp_path):
    # Arrange
    _gateway_files(tmp_path)
    socket = _CompressionSocket()
    # Act
    receipt = compress_session(
        tmp_path, "hub", connect_fn=lambda *args, **kwargs: socket
    )
    # Assert
    assert (
        [request["method"] for request in socket.sent],
        socket.sent[-1]["params"],
        receipt,
    ) == (
        ["session.active_list", "session.compress"],
        {"session_id": "live-1"},
        type(receipt)(
            session_id="live-1",
            before_tokens=663_402,
            after_tokens=91_000,
            before_messages=1324,
            after_messages=87,
            context_used=91_000,
            context_max=1_000_000,
            context_source="provider_usage",
            compressions=1,
        ),
    )


def test_compress_session_refuses_busy_session_before_mutation(tmp_path):
    # Arrange
    _gateway_files(tmp_path)
    socket = _CompressionSocket(status="working")
    error = None
    # Act
    try:
        compress_session(tmp_path, "hub", connect_fn=lambda *args, **kwargs: socket)
    except HermesTuiRpcError as exc:  # stx-allow: test-capture (reason: assert error and absence of mutating RPC together.)
        error = exc

    # Assert
    assert (
        "busy" in str(error),
        [request["method"] for request in socket.sent],
    ) == (True, ["session.active_list"])


@pytest.mark.parametrize(
    "result",
    [
        {"status": "aborted"},
        {
            "status": "compressed",
            "before_tokens": 100,
            "after_tokens": 100,
            "before_messages": 10,
            "after_messages": 5,
        },
    ],
)
def test_compress_session_fails_closed_without_proven_reduction(tmp_path, result):
    # Arrange
    _gateway_files(tmp_path)
    socket = _CompressionSocket(result=result)

    # Act
    def action():
        compress_session(tmp_path, "hub", connect_fn=lambda *args, **kwargs: socket)

    # Assert
    with pytest.raises(HermesTuiRpcError, match="did not commit|no strict"):
        action()


@pytest.mark.parametrize(
    ("usage_update", "message"),
    [
        ({"context_used": 1_000_001}, "incomplete context telemetry"),
        ({"context_max": 0}, "incomplete context telemetry"),
        ({"compressions": 0}, "incomplete context telemetry"),
        ({"context_source": ""}, "incomplete context telemetry"),
        ({"context_used": None}, "invalid context_used"),
        ({"compressions": None}, "invalid compressions"),
    ],
)
def test_compress_session_fails_closed_on_invalid_usage_telemetry(
    tmp_path, usage_update, message
):
    # Arrange
    _gateway_files(tmp_path)
    socket = _CompressionSocket()
    socket.result["info"]["usage"].update(usage_update)

    # Act
    def action():
        compress_session(tmp_path, "hub", connect_fn=lambda *args, **kwargs: socket)

    # Assert
    with pytest.raises(HermesTuiRpcError, match=message):
        action()


def test_compress_session_fails_closed_on_missing_usage_telemetry(tmp_path):
    # Arrange
    _gateway_files(tmp_path)
    result = {
        "status": "compressed",
        "before_tokens": 100,
        "after_tokens": 50,
        "before_messages": 10,
        "after_messages": 5,
    }
    socket = _CompressionSocket(result=result)

    # Act
    def action():
        compress_session(tmp_path, "hub", connect_fn=lambda *args, **kwargs: socket)

    # Assert
    with pytest.raises(HermesTuiRpcError, match="no post-compression usage"):
        action()


def test_clear_heartbeat_uses_control_plane_without_model_turn(tmp_path):
    # Arrange
    _gateway_files(tmp_path)
    socket = _ControlSocket(None)

    # Act
    status = clear_heartbeat_for_session(
        tmp_path, "live-1", connect_fn=lambda *args, **kwargs: socket
    )

    # Assert
    assert (
        status,
        [request["method"] for request in socket.sent],
        socket.sent[-1]["params"],
    ) == (
        "absent",
        ["session.control"],
        {"session_id": "live-1", "action": "heartbeat.clear"},
    )


def test_clear_heartbeat_refuses_persisted_state(tmp_path):
    # Arrange
    _gateway_files(tmp_path)
    socket = _ControlSocket({"status": "active"})

    # Act
    def action():
        clear_heartbeat_for_session(
            tmp_path, "live-1", connect_fn=lambda *a, **k: socket
        )

    # Assert
    with pytest.raises(HermesTuiRpcError, match="did not clear"):
        action()


def test_clear_heartbeat_resolves_named_session_without_model_turn(tmp_path):
    # Arrange
    _gateway_files(tmp_path)
    socket = _ControlSocket(None)

    # Act
    status = clear_heartbeat(tmp_path, "hub", connect_fn=lambda *args, **kwargs: socket)

    # Assert
    assert (
        status,
        [request["method"] for request in socket.sent],
        socket.sent[-1]["params"],
    ) == (
        "absent",
        ["session.active_list", "session.control"],
        {"session_id": "live-1", "action": "heartbeat.clear"},
    )


def test_submit_turn_targets_same_live_session_and_accepts_steer(tmp_path):
    # Arrange
    (tmp_path / GATEWAY_FILE).write_text('{"port":19000}', encoding="utf-8")
    (tmp_path / "hermes-api.key").write_text("a-secure-test-token\n", encoding="utf-8")
    socket = _Socket(status="working")

    # Act
    receipt = submit_turn(tmp_path, "hub", "act now", connect_fn=lambda *a, **k: socket)

    # Assert
    assert (
        receipt.status,
        receipt.delivery_mode,
        [row["method"] for row in socket.sent],
        socket.sent[-1]["params"],
    ) == (
        "steered",
        "steer",
        ["session.active_list", "session.steer"],
        {
            "render_user_message": True,
            "session_id": "live-1",
            "text": "act now",
        },
    )


def test_execute_slash_command_uses_command_plane_not_prompt_submit(tmp_path):
    # Arrange
    _gateway_files(tmp_path)

    class SlashSocket(_Socket):
        def recv(self):
            request = self.sent[-1]
            if request["method"] == "slash.exec":
                return json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "id": request["id"],
                        "result": {"output": "Switched model."},
                    }
                )
            return super().recv()

    socket = SlashSocket(status="idle")

    # Act
    receipt = execute_slash_command(
        tmp_path,
        "hub",
        "/model qwen38-27b --provider custom:sac-qwen38-27b --session",
        connect_fn=lambda *args, **kwargs: socket,
    )

    # Assert
    assert (
        receipt.output,
        receipt.progress.message_count,
        [request["method"] for request in socket.sent],
        socket.sent[-3]["params"],
    ) == (
        "Switched model.",
        12,
        [
            "session.active_list",
            "slash.exec",
            "session.active_list",
            "session.events.since",
        ],
        {
            "session_id": "live-1",
            "command": ("/model qwen38-27b --provider custom:sac-qwen38-27b --session"),
        },
    )


def test_observe_turn_progress_uses_non_activating_live_registry(tmp_path):
    # Arrange
    _gateway_files(tmp_path)
    socket = _Socket(status="idle")
    # Act
    progress = observe_turn_progress(
        tmp_path,
        "hub",
        connect_fn=lambda *args, **kwargs: socket,
    )
    # Assert
    assert (
        progress.message_count,
        progress.last_active,
        progress.status,
        [request["method"] for request in socket.sent],
    ) == (
        12,
        44.0,
        "idle",
        ["session.active_list"],
    )


def test_observe_turn_outcome_reads_terminal_event_without_activation(tmp_path):
    # Arrange
    _gateway_files(tmp_path)

    class OutcomeSocket(_Socket):
        def recv(self):
            request = self.sent[-1]
            if request["method"] == "session.events.since":
                return json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "id": request["id"],
                        "result": {
                            "events": [
                                {
                                    "type": "message.complete",
                                    "seq": 18,
                                    "payload": {"status": "error"},
                                }
                            ],
                            "latest_seq": 18,
                            "truncated": False,
                            "epoch": "epoch-1",
                            "open_requests": [],
                        },
                    }
                )
            return super().recv()

    socket = OutcomeSocket(status="idle")
    # Act
    outcome = observe_turn_outcome(
        tmp_path,
        "hub",
        after_seq=17,
        expected_epoch="epoch-1",
        connect_fn=lambda *args, **kwargs: socket,
    )
    # Assert
    assert (
        outcome.terminal_status,
        [request["method"] for request in socket.sent],
        socket.sent[-1]["params"]["last_seen"],
    ) == ("error", ["session.active_list", "session.events.since"], 17)


def test_explicit_queue_uses_hermes_next_turn_queue_not_active_steer(tmp_path):
    # Arrange
    _gateway_files(tmp_path)

    class QueueSocket(_Socket):
        def recv(self):
            request = self.sent[-1]
            if request["method"] == "prompt.submit":
                return json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "id": request["id"],
                        "result": {"status": "queued"},
                    }
                )
            return super().recv()

    socket = QueueSocket(status="working")

    # Act
    receipt = submit_turn(
        tmp_path,
        "hub",
        "run this afterward",
        delivery_mode="queue",
        connect_fn=lambda *a, **k: socket,
    )

    # Assert
    assert (
        receipt.delivery_mode,
        [request["method"] for request in socket.sent],
        socket.sent[-1]["params"],
    ) == (
        "queue",
        ["session.active_list", "prompt.submit"],
        {
            "render_user_message": True,
            "session_id": "live-1",
            "text": "run this afterward",
            "queued": True,
        },
    )


def test_active_default_never_accepts_hermes_next_turn_queue_as_steer(tmp_path):
    # Arrange
    _gateway_files(tmp_path)

    class RejectingSteerSocket(_Socket):
        def recv(self):
            request = self.sent[-1]
            if request["method"] == "session.steer":
                result = {"status": "rejected", "text": request["params"]["text"]}
                return json.dumps(
                    {"jsonrpc": "2.0", "id": request["id"], "result": result}
                )
            return super().recv()

    socket = RejectingSteerSocket(status="working")

    error = None
    # Act
    try:
        submit_turn(
            tmp_path, "hub", "urgent correction", connect_fn=lambda *a, **k: socket
        )
    except HermesTuiRpcError as exc:  # stx-allow: test-capture (reason: STX-TQ002 requires Act and Assert to remain separate.)
        error = exc
    # Assert
    assert (
        "did not accept the active-turn steer" in str(error),
        [request["method"] for request in socket.sent],
    ) == (
        True,
        ["session.active_list", "session.steer"],
    )


def test_session_selection_refuses_ambiguous_gateway():
    # Arrange
    rows = [{"id": "one", "title": "other"}, {"id": "two", "title": "another"}]

    # Act
    def action() -> None:
        _select_session(rows, "sac:hub")

    # Assert
    with pytest.raises(HermesTuiRpcError, match="cannot identify one"):
        action()


def test_active_sessions_is_observation_only(tmp_path):
    # Arrange
    (tmp_path / GATEWAY_FILE).write_text('{"port":19000}', encoding="utf-8")
    (tmp_path / "hermes-api.key").write_text("a-secure-test-token\n", encoding="utf-8")
    socket = _Socket()

    # Act
    rows = active_sessions(tmp_path, connect_fn=lambda *a, **k: socket)

    # Assert
    assert (rows, [row["method"] for row in socket.sent]) == (
        [
            {
                "id": "live-1",
                "title": "sac:hub",
                "message_count": 12,
                "last_active": 44.0,
            }
        ],
        ["session.active_list"],
    )


@pytest.mark.parametrize(
    ("native_status", "expected_state"),
    [
        ("idle", "idle"),
        ("working", "active"),
        ("waiting", "active"),
        ("starting", "active"),
    ],
)
def test_turn_activity_uses_native_live_session_status(
    tmp_path, native_status, expected_state
):
    # Arrange
    (tmp_path / GATEWAY_FILE).write_text('{"port":19000}', encoding="utf-8")
    (tmp_path / "hermes-api.key").write_text("a-secure-test-token\n", encoding="utf-8")
    socket = _Socket(status=native_status)

    # Act
    observed = observe_turn_activity(tmp_path, "hub", connect_fn=lambda *a, **k: socket)

    # Assert
    assert (
        observed.state,
        observed.session_status,
        observed.session_id,
        [row["method"] for row in socket.sent],
    ) == (expected_state, native_status, "live-1", ["session.active_list"])


def test_turn_activity_refuses_unknown_native_status(tmp_path):
    # Arrange
    (tmp_path / GATEWAY_FILE).write_text('{"port":19000}', encoding="utf-8")
    (tmp_path / "hermes-api.key").write_text("a-secure-test-token\n", encoding="utf-8")

    def action():
        observe_turn_activity(
            tmp_path, "hub", connect_fn=lambda *a, **k: _Socket(status="mystery")
        )

    # Act
    run = action
    # Assert
    with pytest.raises(HermesTuiRpcError, match="unknown activity status"):
        run()


def _gateway_files(tmp_path):
    (tmp_path / GATEWAY_FILE).write_text('{"port":19000}', encoding="utf-8")
    (tmp_path / "hermes-api.key").write_text("a-secure-test-token\n", encoding="utf-8")


def test_visible_idle_turn_is_proven_in_native_inflight_projection(tmp_path):
    # Arrange
    _gateway_files(tmp_path)
    text = "idle delivery <!-- delivery:n_idle -->"
    socket = _VisibleSocket(
        submit_status="streaming",
        projections=[{"messages": []}, {"inflight": {"user": text}}],
    )
    _response, search = _search()

    # Act
    receipt = submit_visible_turn(
        tmp_path,
        "hub",
        text,
        delivery_id="n_idle",
        connect_fn=lambda *a, **k: socket,
        urlopen_fn=search,
    )

    # Assert
    assert (
        receipt.status,
        receipt.visibility,
        [request["method"] for request in socket.sent],
        socket.sent[2]["params"]["render_user_message"],
    ) == (
        "streaming",
        "session.inflight.user",
        [
            "session.active_list",
            "session.activate",
            "prompt.submit",
            "session.activate",
        ],
        True,
    )


def test_visible_busy_turn_is_proven_as_native_steer(tmp_path):
    # Arrange
    _gateway_files(tmp_path)
    text = "busy delivery <!-- delivery:n_busy -->"
    socket = _VisibleSocket(
        submit_status="steered",
        session_status="working",
        projections=[
            {"inflight": {"user": "original", "corrections": []}},
            {"inflight": {"user": "original", "corrections": [text]}},
        ],
    )
    _response, search = _search()

    # Act
    receipt = submit_visible_turn(
        tmp_path,
        "hub",
        text,
        delivery_id="n_busy",
        connect_fn=lambda *a, **k: socket,
        urlopen_fn=search,
    )

    # Assert
    assert (
        receipt.status,
        receipt.visibility,
        socket.sent[2]["params"]["render_user_message"],
    ) == (
        "steered",
        "session.inflight.corrections",
        True,
    )


def test_visible_busy_fallback_is_proven_in_native_queue(tmp_path):
    # Arrange
    _gateway_files(tmp_path)
    text = "queued delivery <!-- delivery:n_queued -->"
    socket = _VisibleSocket(
        submit_status="queued",
        session_status="working",
        projections=[{}, {"queued": {"user": text}}],
    )
    _response, search = _search()

    # Act
    receipt = submit_visible_turn(
        tmp_path,
        "hub",
        text,
        delivery_id="n_queued",
        delivery_mode="queue",
        connect_fn=lambda *a, **k: socket,
        urlopen_fn=search,
    )

    # Assert
    assert (receipt.status, receipt.visibility) == (
        "queued",
        "session.queued.user",
    )


def test_visible_retry_reuses_transcript_proof_without_duplicate_submit(tmp_path):
    # Arrange: native acceptance succeeded long ago, but the downstream Cards
    # ACK did not. The 7,442-message transcript must never cross the websocket.
    _gateway_files(tmp_path)
    text = "retry delivery <!-- delivery:n_retry -->"
    socket = _VisibleSocket(
        submit_status="streaming",
        projections=[{"message_count": 7_442, "messages": []}],
    )
    response, search = _search(
        {
            "results": [
                {
                    "role": "user",
                    "session_id": "stored-1",
                    "lineage_root": "stored-1",
                    "snippet": "retry delivery <!-- >>>delivery:n_retry<<< -->",
                    "message_count": 7_442,
                }
            ]
        }
    )

    # Act
    receipt = submit_visible_turn(
        tmp_path,
        "hub",
        text,
        delivery_id="n_retry",
        connect_fn=lambda *a, **k: socket,
        urlopen_fn=search,
    )

    # Assert
    request, request_kwargs = response.requests[0]
    assert (
        receipt.status,
        receipt.visibility,
        [request["method"] for request in socket.sent],
        socket.sent[-1]["params"]["omit_messages"],
        response.read_sizes,
        request.full_url.endswith(
            "/api/sessions/search?q=%22delivery%20n_retry%22&limit=20"
        ),
        request.get_header("X-hermes-session-token"),
        request_kwargs,
    ) == (
        "already_visible",
        "session.search",
        ["session.active_list", "session.activate"],
        True,
        [256 * 1024 + 1],
        True,
        "a-secure-test-token",
        {"timeout": 10.0},
    )


def test_accepted_submit_without_native_visibility_fails_closed(tmp_path):
    # Arrange
    _gateway_files(tmp_path)
    text = "unproven delivery <!-- delivery:n_unproven -->"
    socket = _VisibleSocket(
        submit_status="streaming",
        projections=[{"messages": []}, {"messages": []}, {"messages": []}],
    )
    _response, search = _search()

    # Act
    def action():
        submit_visible_turn(
            tmp_path,
            "hub",
            text,
            delivery_id="n_unproven",
            max_observations=2,
            poll_s=0,
            connect_fn=lambda *a, **k: socket,
            urlopen_fn=search,
        )

    # Assert
    with pytest.raises(HermesTuiRpcError, match="accepted.*not visible"):
        action()


def test_accepted_submit_closes_with_post_submit_persisted_identity(tmp_path):
    # Arrange — both lightweight projections miss a just-accepted input, but
    # Hermes' final indexed view has committed its durable delivery marker.
    # Returning failure here would make CCT activate its native fallback rail.
    _gateway_files(tmp_path)
    text = "late delivery <!-- delivery:n_late -->"
    socket = _VisibleSocket(
        submit_status="steered",
        projections=[{"messages": []}, {"messages": []}, {"messages": []}],
    )
    searches = iter(
        [
            {"results": []},
            {
                "results": [
                    {
                        "role": "user",
                        "session_id": "stored-1",
                        "lineage_root": "stored-1",
                        "snippet": "late delivery <!-- >>>delivery:n_late<<< -->",
                    }
                ]
            },
        ]
    )

    def search(_request, **_kwargs):
        return _SearchResponse(next(searches))

    # Act
    receipt = submit_visible_turn(
        tmp_path,
        "hub",
        text,
        delivery_id="n_late",
        max_observations=2,
        poll_s=0,
        connect_fn=lambda *a, **k: socket,
        urlopen_fn=search,
    )

    # Assert
    assert (
        receipt.status,
        receipt.visibility,
        [request["method"] for request in socket.sent].count("prompt.submit"),
    ) == ("steered", "session.search", 1)


class _ForkSocket(_Socket):
    def recv(self):
        request = self.sent[-1]
        method = request["method"]
        if method == "session.active_list":
            result = {
                "sessions": [
                    {
                        "id": "parent-live",
                        "title": "sac:scitex-hub-gui:engine-a",
                        "session_key": "parent-stored",
                    }
                ]
            }
        elif method == "session.branch":
            result = {
                "session_id": "branch-live",
                "stored_session_id": "branch-stored",
                "title": "sac:disposable-fork:engine-a",
                "parent": "parent-stored",
                "message_count": 2,
                "messages": [
                    {"role": "user", "text": "nonce-parent-context"},
                    {"role": "assistant", "text": "context retained"},
                ],
                "info": {"cwd": "/work/scitex-hub"},
            }
        elif method == "session.close":
            result = {"closed": True}
        elif method == "session.delete":
            result = {"deleted": "branch-stored"}
        else:  # pragma: no cover - a new RPC is itself a test failure
            raise AssertionError(method)
        return json.dumps({"jsonrpc": "2.0", "id": request["id"], "result": result})


def test_native_fork_branches_once_and_closes_only_temporary_branch(tmp_path):
    # Arrange
    _gateway_files(tmp_path)
    socket = _ForkSocket()
    # Act
    seed = branch_visible_history(
        tmp_path,
        parent_session_key="sac:scitex-hub-gui:engine-a",
        child_session_key="sac:disposable-fork:engine-a",
        child_cwd="/work/scitex-hub",
        connect_fn=lambda *args, **kwargs: socket,
    )
    # Assert
    assert (
        [request["method"] for request in socket.sent],
        socket.sent[1]["params"],
        socket.sent[2]["params"],
        socket.sent[3]["params"],
        seed,
    ) == (
        ["session.active_list", "session.branch", "session.close", "session.delete"],
        {
            "session_id": "parent-live",
            "name": "sac:disposable-fork:engine-a",
        },
        {"session_id": "branch-live"},
        {"session_id": "branch-stored"},
        {
            "version": 1,
            "title": "sac:disposable-fork:engine-a",
            "parent_session_id": "parent-stored",
            "cwd": "/work/scitex-hub",
            "messages": [
                {"role": "user", "text": "nonce-parent-context"},
                {"role": "assistant", "text": "context retained"},
            ],
        },
    )


class _ImportForkSocket(_Socket):
    def recv(self):
        request = self.sent[-1]
        method = request["method"]
        if method == "session.list":
            result = {"sessions": []}
        elif method == "session.create":
            result = {
                "session_id": "seed-parent-live",
                "stored_session_id": "seed-parent-stored",
                "message_count": 2,
                "messages": request["params"]["messages"],
                "info": {"cwd": request["params"]["cwd"]},
            }
        elif method == "session.branch":
            result = {
                "session_id": "child-live",
                "stored_session_id": "child-stored",
                "title": "sac:disposable-fork:engine-a",
                "parent": "seed-parent-stored",
                "message_count": 2,
                "messages": [
                    {"role": "user", "text": "nonce-parent-context"},
                    {"role": "assistant", "text": "context retained"},
                ],
                "info": {"cwd": "/work/scitex-hub"},
            }
        elif method == "session.close":
            result = {"closed": True}
        else:  # pragma: no cover - a new RPC is itself a test failure
            raise AssertionError(method)
        return json.dumps({"jsonrpc": "2.0", "id": request["id"], "result": result})


def test_child_import_receives_identical_visible_history_and_native_lineage(tmp_path):
    # Arrange
    _gateway_files(tmp_path)
    socket = _ImportForkSocket()
    seed = {
        "version": 1,
        "title": "sac:disposable-fork:engine-a",
        "parent_session_id": "parent-stored",
        "cwd": "/work/scitex-hub",
        "messages": [
            {"role": "user", "text": "nonce-parent-context"},
            {"role": "assistant", "text": "context retained"},
        ],
    }
    # Act
    stored = import_fork_seed(
        tmp_path, seed, connect_fn=lambda *args, **kwargs: socket
    )
    # Assert
    methods = [request["method"] for request in socket.sent]
    create = next(request for request in socket.sent if request["method"] == "session.create")
    branch = next(request for request in socket.sent if request["method"] == "session.branch")
    assert (stored, methods, create["params"], branch["params"]) == (
        "child-stored",
        [
            "session.list",
            "session.list",
            "session.create",
            "session.branch",
            "session.close",
            "session.close",
        ],
        {
            "messages": seed["messages"],
            "title": "sac:disposable-fork:engine-a [fork seed:parent-stored]",
            "cwd": "/work/scitex-hub",
            "hidden": True,
        },
        {
            "session_id": "seed-parent-live",
            "name": "sac:disposable-fork:engine-a",
        },
    )


class _ExistingForkSocket(_Socket):
    def recv(self):
        request = self.sent[-1]
        method = request["method"]
        if method == "session.list":
            result = {
                "sessions": [
                    {
                        "id": "child-stored",
                        "title": "sac:child:engine-a",
                        "message_count": 1,
                    }
                ]
            }
        elif method == "session.resume":
            result = {
                "session_id": "child-live",
                "session_key": "child-stored",
                "message_count": 1,
                "messages": [{"role": "user", "text": "parent nonce"}],
            }
        elif method == "session.close":
            result = {"closed": True}
        else:  # pragma: no cover
            raise AssertionError(method)
        return json.dumps({"jsonrpc": "2.0", "id": request["id"], "result": result})


def test_retry_verifies_existing_fork_without_creating_or_branching_again(tmp_path):
    # Arrange
    _gateway_files(tmp_path)
    socket = _ExistingForkSocket()
    seed = {
        "version": 1,
        "title": "sac:child:engine-a",
        "parent_session_id": "parent-stored",
        "cwd": "/work/repo",
        "messages": [{"role": "user", "text": "parent nonce"}],
    }
    # Act
    stored = import_fork_seed(
        tmp_path, seed, connect_fn=lambda *args, **kwargs: socket
    )
    # Assert
    assert (
        stored,
        [request["method"] for request in socket.sent],
    ) == ("child-stored", ["session.list", "session.resume", "session.close"])
