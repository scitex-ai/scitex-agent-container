"""End-to-end tests for the Stop-hook requester-completion push.

NO MOCKS. The completion push is exercised against a REAL local HTTP
receiver shaped like ``sac listen``'s ``/agents/<name>/message:send``
route: a Starlette app on a real socket that records the bodies it
receives and answers with a real ``delivered_subscriber_count``. The
Stop hook closure built by ``build_event_log_hooks`` is the real one and
is fired directly; the conversation→push path runs the real
``run_conversation`` against a hand-rolled fake SDK module passed via the
injection seam (``sdk_module=``) — a fake collaborator, not a patched
module attribute.

TQ: every test carries ``# Arrange`` / ``# Act`` / ``# Assert`` markers,
a ≥3-word descriptive name, and exactly one assertion.
"""

from __future__ import annotations

import asyncio
import json
import os
import socket
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Iterator

import pytest

from scitex_agent_container._runners._session_completion import (
    STATUS_FAILURE,
    STATUS_SUCCESS,
    STATUS_UNKNOWN,
    CompletionPushError,
    build_completion_report,
    push_completion,
)
from scitex_agent_container._runners._session_hooks import (
    TurnContext,
    build_event_log_hooks,
    emit_completion_push,
)

# ---------------------------------------------------------------------------
# Real collaborators — a message:send-shaped receiver on a real socket
# ---------------------------------------------------------------------------


def _free_port() -> int:
    """Ask the kernel for an unused TCP port."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


async def _wait_bound(port: int) -> None:
    """Poll until the TCP port accepts connections."""
    for _ in range(50):
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                return
        except OSError:
            await asyncio.sleep(0.05)
    pytest.fail(f"receiver never bound on port {port}")


class _Receiver:
    """Records the message:send bodies it is POSTed; configurable delivered count."""

    def __init__(self, *, delivered: int = 1) -> None:
        self.delivered = delivered
        self.received: list[dict[str, Any]] = []
        self.paths: list[str] = []


async def _run_receiver(
    *,
    port: int,
    receiver: _Receiver,
    client_coro,
) -> Any:
    """Spin up the real Starlette receiver, run ``client_coro(port)``, tear down."""
    import uvicorn
    from starlette.applications import Starlette
    from starlette.requests import Request
    from starlette.responses import JSONResponse
    from starlette.routing import Route

    async def message_send(request: Request) -> JSONResponse:
        body = await request.json()
        receiver.received.append(body)
        receiver.paths.append(request.url.path)
        return JSONResponse(
            {
                "msg_id": "m-1",
                "to_agent": request.path_params.get("name", ""),
                "delivered_subscriber_count": receiver.delivered,
            }
        )

    app = Starlette(
        routes=[Route("/agents/{name}/message:send", message_send, methods=["POST"])]
    )
    config = uvicorn.Config(
        app, host="127.0.0.1", port=port, log_level="warning", ws="none", lifespan="off"
    )
    server = uvicorn.Server(config)
    stop = asyncio.Event()

    async def _serve() -> None:
        serve_task = asyncio.create_task(server.serve())
        await stop.wait()
        server.should_exit = True
        await asyncio.wait_for(serve_task, timeout=5.0)

    server_task = asyncio.create_task(_serve())
    try:
        await _wait_bound(port)
        return await client_coro(port)
    finally:
        stop.set()
        await asyncio.wait_for(server_task, timeout=5.0)


@pytest.fixture
def listen_env() -> Iterator[None]:
    """Real save/restore of the listen env the runner's push_fn reads."""
    saved_base = os.environ.get("SAC_LISTEN_BASE_URL")
    saved_bearer = os.environ.get("SAC_LISTEN_BEARER")
    try:
        yield
    finally:
        for key, val in (
            ("SAC_LISTEN_BASE_URL", saved_base),
            ("SAC_LISTEN_BEARER", saved_bearer),
        ):
            if val is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = val


# ---------------------------------------------------------------------------
# build_completion_report — honest status, bounded summary
# ---------------------------------------------------------------------------


class TestBuildCompletionReport:
    def test_report_carries_dispatch_id_for_task_correlation(self) -> None:
        # Arrange
        did = "dispatch-abc"
        # Act
        report = build_completion_report(
            agent="a", dispatch_id=did, status=STATUS_SUCCESS, summary_text="ok"
        )
        # Assert
        assert report["dispatch_id"] == did

    def test_report_preserves_honest_success_status(self) -> None:
        # Arrange
        # Act
        report = build_completion_report(
            agent="a", dispatch_id=None, status=STATUS_SUCCESS, summary_text="ok"
        )
        # Assert
        assert report["status"] == STATUS_SUCCESS

    def test_report_preserves_honest_failure_status(self) -> None:
        # Arrange
        # Act
        report = build_completion_report(
            agent="a", dispatch_id=None, status=STATUS_FAILURE, summary_text="boom"
        )
        # Assert
        assert report["status"] == STATUS_FAILURE

    def test_report_coerces_unknown_status_never_fabricates_success(self) -> None:
        # Arrange
        bogus = "totally-made-up"
        # Act
        report = build_completion_report(
            agent="a", dispatch_id=None, status=bogus, summary_text=""
        )
        # Assert
        assert report["status"] == STATUS_UNKNOWN

    def test_report_bounds_runaway_summary_length(self) -> None:
        # Arrange
        huge = "x" * 9_000
        # Act
        report = build_completion_report(
            agent="a", dispatch_id=None, status=STATUS_SUCCESS, summary_text=huge
        )
        # Assert
        assert len(report["summary"]) < len(huge)

    def test_report_names_reporting_agent(self) -> None:
        # Arrange
        # Act
        report = build_completion_report(
            agent="worker-7", dispatch_id=None, status=STATUS_SUCCESS, summary_text=""
        )
        # Assert
        assert report["agent"] == "worker-7"


# ---------------------------------------------------------------------------
# push_completion — real receiver, loud on no-subscriber / transport
# ---------------------------------------------------------------------------


class TestPushCompletion:
    def test_push_delivers_report_to_requester_inbox(self) -> None:
        # Arrange
        port = _free_port()
        receiver = _Receiver(delivered=1)
        report = build_completion_report(
            agent="worker", dispatch_id="d1", status=STATUS_SUCCESS, summary_text="done"
        )

        async def _client(p: int):
            await push_completion(
                agent="worker",
                requester="lead",
                report=report,
                listen_url=f"http://127.0.0.1:{p}",
                bearer=None,
                dispatch_id="d1",
            )
            return receiver.received

        # Act
        received = asyncio.run(
            _run_receiver(port=port, receiver=receiver, client_coro=_client)
        )
        # Assert
        assert len(received) == 1

    def test_push_addresses_the_requester_path(self) -> None:
        # Arrange
        port = _free_port()
        receiver = _Receiver(delivered=1)
        report = build_completion_report(
            agent="worker", dispatch_id=None, status=STATUS_SUCCESS, summary_text=""
        )

        async def _client(p: int):
            await push_completion(
                agent="worker",
                requester="lead",
                report=report,
                listen_url=f"http://127.0.0.1:{p}",
                bearer=None,
            )
            return receiver.paths

        # Act
        paths = asyncio.run(
            _run_receiver(port=port, receiver=receiver, client_coro=_client)
        )
        # Assert
        assert paths == ["/agents/lead/message:send"]

    def test_push_body_carries_reporting_agent_as_from_agent(self) -> None:
        # Arrange
        port = _free_port()
        receiver = _Receiver(delivered=1)
        report = build_completion_report(
            agent="worker", dispatch_id=None, status=STATUS_SUCCESS, summary_text=""
        )

        async def _client(p: int):
            await push_completion(
                agent="worker",
                requester="lead",
                report=report,
                listen_url=f"http://127.0.0.1:{p}",
                bearer=None,
            )
            return receiver.received[0]

        # Act
        body = asyncio.run(
            _run_receiver(port=port, receiver=receiver, client_coro=_client)
        )
        # Assert
        assert body["params"]["metadata"]["from_agent"] == "worker"

    def test_push_body_carries_dispatch_id_in_metadata(self) -> None:
        # Arrange
        port = _free_port()
        receiver = _Receiver(delivered=1)
        report = build_completion_report(
            agent="worker", dispatch_id="d-77", status=STATUS_SUCCESS, summary_text=""
        )

        async def _client(p: int):
            await push_completion(
                agent="worker",
                requester="lead",
                report=report,
                listen_url=f"http://127.0.0.1:{p}",
                bearer=None,
                dispatch_id="d-77",
            )
            return receiver.received[0]

        # Act
        body = asyncio.run(
            _run_receiver(port=port, receiver=receiver, client_coro=_client)
        )
        # Assert
        assert body["params"]["metadata"]["dispatch_id"] == "d-77"

    def test_push_message_text_is_the_completion_report_json(self) -> None:
        # Arrange
        port = _free_port()
        receiver = _Receiver(delivered=1)
        report = build_completion_report(
            agent="worker", dispatch_id="d-9", status=STATUS_SUCCESS, summary_text="hi"
        )

        async def _client(p: int):
            await push_completion(
                agent="worker",
                requester="lead",
                report=report,
                listen_url=f"http://127.0.0.1:{p}",
                bearer=None,
                dispatch_id="d-9",
            )
            text = receiver.received[0]["params"]["message"]["parts"][0]["text"]
            return json.loads(text)

        # Act
        decoded = asyncio.run(
            _run_receiver(port=port, receiver=receiver, client_coro=_client)
        )
        # Assert
        assert decoded["status"] == STATUS_SUCCESS

    def test_push_raises_loud_when_no_live_subscriber(self) -> None:
        # Arrange
        port = _free_port()
        receiver = _Receiver(delivered=0)  # no subscriber → woke nobody
        report = build_completion_report(
            agent="worker", dispatch_id=None, status=STATUS_SUCCESS, summary_text=""
        )

        async def _client(p: int):
            await push_completion(
                agent="worker",
                requester="lead",
                report=report,
                listen_url=f"http://127.0.0.1:{p}",
                bearer=None,
            )

        async def _scenario():
            await _run_receiver(port=port, receiver=receiver, client_coro=_client)

        # Act
        ctx = pytest.raises(CompletionPushError)
        # Assert
        with ctx:
            asyncio.run(_scenario())

    def test_push_raises_loud_when_requester_unreachable(self) -> None:
        # Arrange
        dead_port = _free_port()  # nothing listening here
        report = build_completion_report(
            agent="worker", dispatch_id=None, status=STATUS_FAILURE, summary_text="x"
        )

        async def _scenario():
            await push_completion(
                agent="worker",
                requester="lead",
                report=report,
                listen_url=f"http://127.0.0.1:{dead_port}",
                bearer=None,
            )

        # Act
        ctx = pytest.raises(CompletionPushError)
        # Assert
        with ctx:
            asyncio.run(_scenario())


# ---------------------------------------------------------------------------
# Stop hook fires → emits the completion push (the real hook closure)
# ---------------------------------------------------------------------------


class _HookMatcher:
    """Real stand-in for the SDK HookMatcher — records the callbacks it wraps."""

    def __init__(self, hooks: list) -> None:
        self.hooks = hooks


def _stop_callback(hooks_dict: dict):
    """Pull the single Stop callback out of the built hooks dict."""
    return hooks_dict["Stop"][0].hooks[0]


class TestStopHookEmitsCompletion:
    def test_stop_hook_fire_delivers_completion_to_requester(
        self, tmp_path: Path
    ) -> None:
        # Arrange
        port = _free_port()
        receiver = _Receiver(delivered=1)
        ctx = TurnContext()
        ctx.begin(requester="lead", dispatch_id="d-stop")
        ctx.finish(status=STATUS_SUCCESS, summary="all good")

        async def _client(p: int):
            async def _push_fn(report, requester, dispatch_id):
                await push_completion(
                    agent="worker",
                    requester=requester,
                    report=report,
                    listen_url=f"http://127.0.0.1:{p}",
                    bearer=None,
                    dispatch_id=dispatch_id,
                )

            hooks = build_event_log_hooks(
                "worker",
                _HookMatcher,
                event_log_root=tmp_path,
                turn_context=ctx,
                push_fn=_push_fn,
            )
            on_stop = _stop_callback(hooks)
            await on_stop({"stop_hook_active": False}, None, None)
            return receiver.received

        # Act
        received = asyncio.run(
            _run_receiver(port=port, receiver=receiver, client_coro=_client)
        )
        # Assert
        assert len(received) == 1

    def test_stop_hook_completion_reports_honest_success_status(
        self, tmp_path: Path
    ) -> None:
        # Arrange
        port = _free_port()
        receiver = _Receiver(delivered=1)
        ctx = TurnContext()
        ctx.begin(requester="lead", dispatch_id="d-stop2")
        ctx.finish(status=STATUS_SUCCESS, summary="done")

        async def _client(p: int):
            async def _push_fn(report, requester, dispatch_id):
                await push_completion(
                    agent="worker",
                    requester=requester,
                    report=report,
                    listen_url=f"http://127.0.0.1:{p}",
                    bearer=None,
                    dispatch_id=dispatch_id,
                )

            hooks = build_event_log_hooks(
                "worker",
                _HookMatcher,
                event_log_root=tmp_path,
                turn_context=ctx,
                push_fn=_push_fn,
            )
            await _stop_callback(hooks)({"stop_hook_active": False}, None, None)
            text = receiver.received[0]["params"]["message"]["parts"][0]["text"]
            return json.loads(text)

        # Act
        report = asyncio.run(
            _run_receiver(port=port, receiver=receiver, client_coro=_client)
        )
        # Assert
        assert report["status"] == STATUS_SUCCESS

    def test_stop_hook_without_requester_pushes_nothing(self, tmp_path: Path) -> None:
        # Arrange
        ctx = TurnContext()
        ctx.begin(requester=None, dispatch_id=None)  # mission/boot turn
        ctx.finish(status=STATUS_SUCCESS, summary="x")
        pushed: list = []

        async def _push_fn(report, requester, dispatch_id):
            pushed.append(report)

        async def _scenario():
            hooks = build_event_log_hooks(
                "worker",
                _HookMatcher,
                event_log_root=tmp_path,
                turn_context=ctx,
                push_fn=_push_fn,
            )
            await _stop_callback(hooks)({"stop_hook_active": False}, None, None)
            return pushed

        # Act
        observed = asyncio.run(_scenario())
        # Assert
        assert observed == []

    def test_stop_hook_failed_push_does_not_raise(self, tmp_path: Path) -> None:
        # Arrange
        ctx = TurnContext()
        ctx.begin(requester="lead", dispatch_id="d")
        ctx.finish(status=STATUS_SUCCESS, summary="x")

        async def _push_fn(report, requester, dispatch_id):
            raise CompletionPushError("no subscriber")

        async def _scenario():
            hooks = build_event_log_hooks(
                "worker",
                _HookMatcher,
                event_log_root=tmp_path,
                turn_context=ctx,
                push_fn=_push_fn,
            )
            # A loud-but-contained push failure must NOT propagate out of
            # the Stop hook (the turn already completed).
            await _stop_callback(hooks)({"stop_hook_active": False}, None, None)
            return "no-raise"

        # Act
        observed = asyncio.run(_scenario())
        # Assert
        assert observed == "no-raise"


# ---------------------------------------------------------------------------
# emit_completion_push — the once-per-turn guard
# ---------------------------------------------------------------------------


class TestEmitOnceGuard:
    def test_second_emit_for_same_turn_is_suppressed(self) -> None:
        # Arrange
        ctx = TurnContext()
        ctx.begin(requester="lead", dispatch_id="d")
        ctx.finish(status=STATUS_SUCCESS, summary="x")
        calls: list = []

        async def _push_fn(report, requester, dispatch_id):
            calls.append(report)

        async def _scenario():
            await emit_completion_push(ctx, _push_fn, agent_name="worker")
            await emit_completion_push(ctx, _push_fn, agent_name="worker")
            return calls

        # Act
        observed = asyncio.run(_scenario())
        # Assert
        assert len(observed) == 1


# ---------------------------------------------------------------------------
# emit_completion_push — sender-side empty-beacon noise suppression
# ---------------------------------------------------------------------------


class TestEmptyBeaconSuppression:
    """Sender-side guard: status==unknown AND empty summary → SKIP dispatch.

    The conversation never populated an outcome (no ResultMessage, no
    exception caught) so the beacon would carry no actionable information
    for the requester. Across the fleet these structurally-identical empty
    beacons were noise; suppressing at the SENDER (loud-skip beats silent
    noise) keeps the a2a channel signal-only.
    """

    def test_unknown_status_with_empty_summary_is_suppressed(self) -> None:
        # Arrange — begin opens turn (status=None, summary=""); never finished.
        ctx = TurnContext()
        ctx.begin(requester="lead", dispatch_id="d-empty")
        calls: list = []

        async def _push_fn(report, requester, dispatch_id):
            calls.append(report)

        async def _scenario():
            await emit_completion_push(ctx, _push_fn, agent_name="worker")
            return calls

        # Act
        observed = asyncio.run(_scenario())
        # Assert
        assert observed == []

    def test_unknown_status_with_whitespace_only_summary_is_suppressed(self) -> None:
        # Arrange — whitespace summary is treated as empty (no real signal).
        ctx = TurnContext()
        ctx.begin(requester="lead", dispatch_id="d-ws")
        ctx.finish(status=STATUS_UNKNOWN, summary="   \n\t  ")
        calls: list = []

        async def _push_fn(report, requester, dispatch_id):
            calls.append(report)

        async def _scenario():
            await emit_completion_push(ctx, _push_fn, agent_name="worker")
            return calls

        # Act
        observed = asyncio.run(_scenario())
        # Assert
        assert observed == []

    def test_unknown_status_with_real_summary_still_dispatches(self) -> None:
        # Arrange — summary carries signal; honest "unknown" must still go.
        ctx = TurnContext()
        ctx.begin(requester="lead", dispatch_id="d-known-unk")
        ctx.finish(status=STATUS_UNKNOWN, summary="partial output then SDK stalled")
        calls: list = []

        async def _push_fn(report, requester, dispatch_id):
            calls.append(report)

        async def _scenario():
            await emit_completion_push(ctx, _push_fn, agent_name="worker")
            return calls

        # Act
        observed = asyncio.run(_scenario())
        # Assert
        assert len(observed) == 1

    def test_success_status_with_empty_summary_still_dispatches(self) -> None:
        # Arrange — a clean turn that happened to produce no text is still
        # a real outcome the requester needs to hear about.
        ctx = TurnContext()
        ctx.begin(requester="lead", dispatch_id="d-empty-success")
        ctx.finish(status=STATUS_SUCCESS, summary="")
        calls: list = []

        async def _push_fn(report, requester, dispatch_id):
            calls.append(report)

        async def _scenario():
            await emit_completion_push(ctx, _push_fn, agent_name="worker")
            return calls

        # Act
        observed = asyncio.run(_scenario())
        # Assert
        assert len(observed) == 1

    def test_failure_status_with_empty_summary_still_dispatches(self) -> None:
        # Arrange — honest failure with no captured detail is still signal.
        ctx = TurnContext()
        ctx.begin(requester="lead", dispatch_id="d-empty-fail")
        ctx.finish(status=STATUS_FAILURE, summary="")
        calls: list = []

        async def _push_fn(report, requester, dispatch_id):
            calls.append(report)

        async def _scenario():
            await emit_completion_push(ctx, _push_fn, agent_name="worker")
            return calls

        # Act
        observed = asyncio.run(_scenario())
        # Assert
        assert len(observed) == 1

    def test_suppressed_empty_beacon_marks_turn_pushed(self) -> None:
        # Arrange — pushed=True after suppression so the failure-path's
        # redundant emit_completion_push() in _drive_turn.finally cannot
        # retry the same empty beacon.
        ctx = TurnContext()
        ctx.begin(requester="lead", dispatch_id="d-once-empty")
        calls: list = []

        async def _push_fn(report, requester, dispatch_id):
            calls.append(report)

        async def _scenario():
            await emit_completion_push(ctx, _push_fn, agent_name="worker")
            return ctx.pushed

        # Act
        pushed = asyncio.run(_scenario())
        # Assert
        assert pushed is True

    def test_suppressed_empty_beacon_logs_loud_warning(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        # Arrange — suppress must be LOUD (WARNING) so a stuck emitter is
        # observable in the agent log; silent drops are the anti-pattern.
        ctx = TurnContext()
        ctx.begin(requester="lead", dispatch_id="d-loud")
        calls: list = []

        async def _push_fn(report, requester, dispatch_id):
            calls.append(report)

        async def _scenario():
            with caplog.at_level(
                "WARNING", logger="scitex_agent_container._runners._session_hooks"
            ):
                await emit_completion_push(ctx, _push_fn, agent_name="worker")
            return caplog.records

        # Act
        records = asyncio.run(_scenario())
        # Assert
        assert any("SUPPRESSED" in r.getMessage() for r in records)


# ---------------------------------------------------------------------------
# Full conversation → requester push (real run_conversation, fake SDK module)
# ---------------------------------------------------------------------------


class _FakeAssistantMsg:
    def __init__(self, *texts: str) -> None:
        self.content = [_FakeTextBlock(t) for t in texts]


class _FakeTextBlock:
    def __init__(self, text: str) -> None:
        self.text = text


class _FakeResultMsg:
    def __init__(self, session_id: str, usage: dict | None = None) -> None:
        self.session_id = session_id
        self.usage = usage


class _FakeUserMsg:
    pass


class _FakeSDKClient:
    """Real fake SDK client: one canned assistant turn + a ResultMessage."""

    def __init__(self, options=None) -> None:
        self.queries: list[str] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def query(self, text: str) -> None:
        self.queries.append(text)

    async def interrupt(self) -> None:
        pass

    async def receive_response(self):
        for m in (
            _FakeAssistantMsg("the ", "answer"),
            _FakeResultMsg("sess-c", {"input_tokens": 1, "output_tokens": 1}),
        ):
            yield m


def _fake_sdk_module(client: _FakeSDKClient) -> SimpleNamespace:
    """Hand-rolled fake of the claude_agent_sdk module — passed via injection."""
    return SimpleNamespace(
        ClaudeSDKClient=lambda options=None: client,
        AssistantMessage=_FakeAssistantMsg,
        TextBlock=_FakeTextBlock,
        ResultMessage=_FakeResultMsg,
        UserMessage=_FakeUserMsg,
        HookMatcher=_HookMatcher,
    )


class TestConversationPushesCompletion:
    def test_completed_turn_pushes_report_to_requester(
        self, tmp_path: Path, listen_env
    ) -> None:
        # Arrange
        from scitex_agent_container._runners import _session_conversation as conv
        from scitex_agent_container._runners._session_inbox import (
            ShutdownEnvelope,
            TurnEnvelope,
            make_inbox,
        )

        port = _free_port()
        receiver = _Receiver(delivered=1)
        os.environ["SAC_LISTEN_BASE_URL"] = f"http://127.0.0.1:{port}"
        os.environ.pop("SAC_LISTEN_BEARER", None)

        async def _client(p: int):
            inbox = make_inbox()
            stop = asyncio.Event()
            loop = asyncio.get_running_loop()
            env = TurnEnvelope(
                text="hi",
                response=loop.create_future(),
                from_agent="lead",
                dispatch_id="d-conv",
            )
            await inbox.put(env)
            await inbox.put(ShutdownEnvelope())
            await conv.run_conversation(
                "worker",
                tmp_path / "worker",
                pid=4_321,
                inbox=inbox,
                resume_session_id=None,
                stop=stop,
                sdk_module=_fake_sdk_module(_FakeSDKClient()),
                build_sdk_options_fn=lambda name, **kw: SimpleNamespace(name=name),
            )
            return receiver.received

        # Act
        received = asyncio.run(
            _run_receiver(port=port, receiver=receiver, client_coro=_client)
        )
        # Assert
        assert len(received) == 1

    def test_completed_turn_completion_carries_dispatch_id(
        self, tmp_path: Path, listen_env
    ) -> None:
        # Arrange
        from scitex_agent_container._runners import _session_conversation as conv
        from scitex_agent_container._runners._session_inbox import (
            ShutdownEnvelope,
            TurnEnvelope,
            make_inbox,
        )

        port = _free_port()
        receiver = _Receiver(delivered=1)
        os.environ["SAC_LISTEN_BASE_URL"] = f"http://127.0.0.1:{port}"
        os.environ.pop("SAC_LISTEN_BEARER", None)

        async def _client(p: int):
            inbox = make_inbox()
            stop = asyncio.Event()
            loop = asyncio.get_running_loop()
            env = TurnEnvelope(
                text="hi",
                response=loop.create_future(),
                from_agent="lead",
                dispatch_id="d-corr",
            )
            await inbox.put(env)
            await inbox.put(ShutdownEnvelope())
            await conv.run_conversation(
                "worker",
                tmp_path / "worker",
                pid=4_322,
                inbox=inbox,
                resume_session_id=None,
                stop=stop,
                sdk_module=_fake_sdk_module(_FakeSDKClient()),
                build_sdk_options_fn=lambda name, **kw: SimpleNamespace(name=name),
            )
            return receiver.received[0]

        # Act
        body = asyncio.run(
            _run_receiver(port=port, receiver=receiver, client_coro=_client)
        )
        # Assert
        assert body["params"]["metadata"]["dispatch_id"] == "d-corr"

    def test_mission_turn_without_requester_pushes_nothing(
        self, tmp_path: Path, listen_env
    ) -> None:
        # Arrange
        from scitex_agent_container._runners import _session_conversation as conv
        from scitex_agent_container._runners._session_inbox import (
            ShutdownEnvelope,
            TurnEnvelope,
            make_inbox,
        )

        port = _free_port()
        receiver = _Receiver(delivered=1)
        os.environ["SAC_LISTEN_BASE_URL"] = f"http://127.0.0.1:{port}"
        os.environ.pop("SAC_LISTEN_BEARER", None)

        async def _client(p: int):
            inbox = make_inbox()
            stop = asyncio.Event()
            loop = asyncio.get_running_loop()
            # No from_agent → a mission/boot turn that answers to no peer.
            env = TurnEnvelope(text="boot", response=loop.create_future())
            await inbox.put(env)
            await inbox.put(ShutdownEnvelope())
            await conv.run_conversation(
                "worker",
                tmp_path / "worker",
                pid=4_323,
                inbox=inbox,
                resume_session_id=None,
                stop=stop,
                sdk_module=_fake_sdk_module(_FakeSDKClient()),
                build_sdk_options_fn=lambda name, **kw: SimpleNamespace(name=name),
            )
            return receiver.received

        # Act
        received = asyncio.run(
            _run_receiver(port=port, receiver=receiver, client_coro=_client)
        )
        # Assert
        assert received == []
