"""Tests for the runner's inbound-turn channel (PR1: queue + executor wiring).

PA-306: no ``unittest.mock``. The SDK symbols are swapped on
``claude_agent_sdk`` and runner module directly via a ``_swap_sdk``
context manager that save/restores each attribute. ``state_root``
fixture uses explicit env / module-attribute save/restore.
"""

from __future__ import annotations

import asyncio
from contextlib import ExitStack, contextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Iterator

import pytest

from scitex_agent_container._runners import claude_session as runner
from scitex_agent_container._runners._session_inbox import (
    ShutdownEnvelope,
    TurnEnvelope,
    WakeableInbox,
    make_inbox,
)


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


class _FakeSDKClient:
    """Stand-in for ``ClaudeSDKClient`` — records query()/interrupt() and
    yields a pre-canned message stream from receive_response()."""

    def __init__(self, options=None) -> None:
        self.queries: list[str] = []
        self.interrupted = 0
        self.scripts: list[list] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def query(self, text: str) -> None:
        self.queries.append(text)

    async def interrupt(self) -> None:
        self.interrupted += 1

    async def receive_response(self):
        if self.scripts:
            msgs = self.scripts.pop(0)
        else:
            msgs = [
                _FakeAssistantMsg("ok"),
                _FakeResultMsg("sess-test", {"input_tokens": 1, "output_tokens": 1}),
            ]
        for m in msgs:
            yield m


@pytest.fixture
def state_root(tmp_path: Path) -> Iterator[Path]:
    """Real save/restore of module attrs that previously used monkeypatch."""
    from scitex_agent_container._runners import _session_state
    from scitex_agent_container.runtimes import _sdk_common

    saved_runner_root = runner.DEFAULT_STATE_ROOT
    saved_sess_root = _session_state.DEFAULT_STATE_ROOT
    saved_build = _sdk_common.build_sdk_options
    runner.DEFAULT_STATE_ROOT = tmp_path
    _session_state.DEFAULT_STATE_ROOT = tmp_path
    _sdk_common.build_sdk_options = lambda name, **kw: SimpleNamespace(name=name, **kw)
    try:
        yield tmp_path
    finally:
        runner.DEFAULT_STATE_ROOT = saved_runner_root
        _session_state.DEFAULT_STATE_ROOT = saved_sess_root
        _sdk_common.build_sdk_options = saved_build


@contextmanager
def _swap_sdk(fake_client: _FakeSDKClient) -> Iterator[None]:
    """Swap the SDK surface used by ``_run_conversation`` for fakes."""
    import claude_agent_sdk

    keys = (
        "ClaudeSDKClient",
        "AssistantMessage",
        "TextBlock",
        "ResultMessage",
        "UserMessage",
    )
    saved = {k: getattr(claude_agent_sdk, k, None) for k in keys}
    claude_agent_sdk.ClaudeSDKClient = lambda options=None: fake_client  # type: ignore[assignment]
    claude_agent_sdk.AssistantMessage = _FakeAssistantMsg  # type: ignore[assignment]
    claude_agent_sdk.TextBlock = _FakeTextBlock  # type: ignore[assignment]
    claude_agent_sdk.ResultMessage = _FakeResultMsg  # type: ignore[assignment]
    claude_agent_sdk.UserMessage = type("U", (), {})  # type: ignore[assignment]
    try:
        yield
    finally:
        for k, v in saved.items():
            if v is None:
                if hasattr(claude_agent_sdk, k):
                    delattr(claude_agent_sdk, k)
            else:
                setattr(claude_agent_sdk, k, v)


# ---------------------------------------------------------------------------
# Scenario fixtures — each fixture runs one full async scenario once,
# returning a dict the per-behaviour tests assert against.
# ---------------------------------------------------------------------------


@pytest.fixture
def single_turn_scenario(state_root: Path) -> dict:
    client = _FakeSDKClient()
    client.scripts = [
        [
            _FakeAssistantMsg("Hello ", "world"),
            _FakeResultMsg("sess-1", {"input_tokens": 5, "output_tokens": 7}),
        ]
    ]

    async def _scenario() -> str:
        inbox = make_inbox()
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        env = TurnEnvelope(text="hi", response=loop.create_future())
        await inbox.put(env)
        await inbox.put(ShutdownEnvelope())
        with _swap_sdk(client):
            await runner._run_conversation(
                "alpha",
                state_root / "alpha",
                pid=1_234,
                inbox=inbox,
                resume_session_id=None,
                stop=stop,
            )
        return await env.response

    reply = asyncio.run(_scenario())
    return {"reply": reply, "queries": client.queries}


@pytest.fixture
def multi_turn_scenario(state_root: Path) -> dict:
    client = _FakeSDKClient()
    client.scripts = [
        [_FakeAssistantMsg("first"), _FakeResultMsg("sess-2", {})],
        [_FakeAssistantMsg("second"), _FakeResultMsg("sess-2", {})],
    ]

    async def _scenario():
        inbox = make_inbox()
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        e1 = TurnEnvelope(text="q1", response=loop.create_future())
        e2 = TurnEnvelope(text="q2", response=loop.create_future())
        for env in (e1, e2):
            await inbox.put(env)
        await inbox.put(ShutdownEnvelope())
        with _swap_sdk(client):
            await runner._run_conversation(
                "beta",
                state_root / "beta",
                pid=2,
                inbox=inbox,
                resume_session_id=None,
                stop=stop,
            )
        return await e1.response, await e2.response

    r1, r2 = asyncio.run(_scenario())
    return {"r1": r1, "r2": r2, "queries": client.queries}


@pytest.fixture
def exit_after_scenario(state_root: Path) -> dict:
    client = _FakeSDKClient()

    async def _scenario():
        inbox = make_inbox()
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        env = TurnEnvelope(text="bye", response=loop.create_future(), exit_after=True)
        await inbox.put(env)
        with _swap_sdk(client):
            await runner._run_conversation(
                "gamma",
                state_root / "gamma",
                pid=3,
                inbox=inbox,
                resume_session_id=None,
                stop=stop,
            )
        return stop.is_set()

    return {"stop_set": asyncio.run(_scenario())}


class TestInboxDrain:
    def test_turn_envelope_resolves_with_concatenated_assistant_text(
        self, single_turn_scenario: dict
    ) -> None:
        # Arrange
        result = single_turn_scenario
        # Act
        reply = result["reply"]
        # Assert
        assert reply == "Hello world"

    def test_turn_envelope_forwards_text_into_sdk_query(
        self, single_turn_scenario: dict
    ) -> None:
        # Arrange
        result = single_turn_scenario
        # Act
        queries = result["queries"]
        # Assert
        assert queries == ["hi"]

    def test_multiple_turns_first_response_matches_first_script(
        self, multi_turn_scenario: dict
    ) -> None:
        # Arrange
        result = multi_turn_scenario
        # Act
        r1 = result["r1"]
        # Assert
        assert r1 == "first"

    def test_multiple_turns_second_response_matches_second_script(
        self, multi_turn_scenario: dict
    ) -> None:
        # Arrange
        result = multi_turn_scenario
        # Act
        r2 = result["r2"]
        # Assert
        assert r2 == "second"

    def test_multiple_turns_queries_recorded_in_submission_order(
        self, multi_turn_scenario: dict
    ) -> None:
        # Arrange
        result = multi_turn_scenario
        # Act
        queries = result["queries"]
        # Assert
        assert queries == ["q1", "q2"]

    def test_exit_after_envelope_sets_stop_event(
        self, exit_after_scenario: dict
    ) -> None:
        # Arrange
        result = exit_after_scenario
        # Act
        stop_set = result["stop_set"]
        # Assert
        assert stop_set is True


class TestDrainFailedInbox:
    def test_pending_turn_futures_receive_init_failure_exception(self) -> None:
        """If SDK init fails, queued turn futures are resolved with
        the failure so producers don't hang."""

        # Arrange
        async def _scenario():
            inbox = make_inbox()
            loop = asyncio.get_running_loop()
            env = TurnEnvelope(text="x", response=loop.create_future())
            await inbox.put(env)
            await inbox.put(ShutdownEnvelope())
            runner._drain_failed_inbox(inbox, RuntimeError("boom"))
            try:
                await env.response
                return "no-exc"
            except RuntimeError as exc:
                return str(exc)

        # Act
        observed = asyncio.run(_scenario())
        # Assert
        assert observed == "boom"


# Keep ``ExitStack`` referenced even though our new pattern uses
# ``with _swap_sdk(...)`` directly — kept for forward-compat if a
# multi-swap helper grows back.
_ = ExitStack


# ---------------------------------------------------------------------------
# WakeableInbox — #41 wake-on-inbound (lead a2a f39bdcc5)
# ---------------------------------------------------------------------------


def _make_env(text: str = "hi") -> TurnEnvelope:
    """Build a TurnEnvelope with an attached Future. Must run inside an
    event loop because the Future binds to the running loop."""
    loop = asyncio.get_event_loop()
    return TurnEnvelope(text=text, response=loop.create_future())


def test_make_inbox_returns_wakeable_inbox():
    # Arrange / Act
    async def _go():
        inbox = make_inbox()
        return isinstance(inbox, WakeableInbox)

    # Assert
    assert asyncio.run(_go()) is True


def test_wakeable_inbox_put_then_get_returns_same_envelope():
    # Arrange
    async def _scenario():
        inbox = make_inbox()
        env = _make_env("hello")
        await inbox.put(env)
        return await inbox.get()

    # Act
    got = asyncio.run(_scenario())
    # Assert
    assert got.text == "hello"


def test_wakeable_inbox_wait_for_item_returns_when_envelope_arrives():
    # Arrange — wait_for_item is blocked until a producer puts.
    async def _scenario():
        inbox = make_inbox()
        observed = []

        async def _waiter():
            await inbox.wait_for_item()
            observed.append("woke")

        waiter = asyncio.create_task(_waiter())
        # Give the waiter time to actually start waiting.
        await asyncio.sleep(0.01)
        assert observed == []  # Pin: still waiting before put.
        await inbox.put(_make_env())
        await asyncio.wait_for(waiter, timeout=1.0)
        return observed

    # Act
    observed = asyncio.run(_scenario())
    # Assert
    assert observed == ["woke"]


def test_wakeable_inbox_wait_for_item_returns_immediately_when_nonempty():
    # Arrange
    async def _scenario():
        inbox = make_inbox()
        await inbox.put(_make_env())
        # Should return without yielding (queue already non-empty).
        await asyncio.wait_for(inbox.wait_for_item(), timeout=0.5)
        return inbox.qsize()

    # Act
    qsize = asyncio.run(_scenario())
    # Assert — wait_for_item did NOT consume the envelope.
    assert qsize == 1


def test_wakeable_inbox_wait_for_item_does_not_consume():
    # Arrange — the wake-on-inbound contract relies on this: wait_for_item
    # tells us an envelope IS THERE so the conversation loop's subsequent
    # inbox.get() will dequeue it. If wait_for_item consumed, the
    # envelope would be lost.
    async def _scenario():
        inbox = make_inbox()
        env_in = _make_env("preserved")
        await inbox.put(env_in)
        await inbox.wait_for_item()
        # The envelope is still queued.
        env_out = await inbox.get()
        return env_out.text

    # Act
    got = asyncio.run(_scenario())
    # Assert
    assert got == "preserved"


def test_wakeable_inbox_event_clears_after_full_drain():
    # Arrange — once the queue empties, wait_for_item must BLOCK again
    # (a subsequent producer's put must be required to wake the next
    # waiter). Otherwise the wake task would re-fire spuriously.
    async def _scenario():
        inbox = make_inbox()
        await inbox.put(_make_env())
        await inbox.get()  # full drain
        # wait_for_item must block; assert by timing out.
        try:
            await asyncio.wait_for(inbox.wait_for_item(), timeout=0.1)
        except asyncio.TimeoutError:
            return "blocked"
        return "did-not-block"

    # Act
    outcome = asyncio.run(_scenario())
    # Assert
    assert outcome == "blocked"


def test_wakeable_inbox_event_stays_set_with_multiple_pending():
    # Arrange — two envelopes queued, only one drained: wait_for_item
    # must STILL return immediately (because the queue is still non-
    # empty, the wake signal is honest).
    async def _scenario():
        inbox = make_inbox()
        await inbox.put(_make_env("a"))
        await inbox.put(_make_env("b"))
        await inbox.get()  # one drained, one remains
        await asyncio.wait_for(inbox.wait_for_item(), timeout=0.5)
        return inbox.qsize()

    # Act
    remaining = asyncio.run(_scenario())
    # Assert
    assert remaining == 1


def test_wakeable_inbox_put_nowait_signals_event():
    # Arrange — synchronous put_nowait is on the API surface (mirrors
    # asyncio.Queue), used by tests; it must signal the wake event too.
    async def _scenario():
        inbox = make_inbox()
        inbox.put_nowait(_make_env())
        # wait_for_item should not block.
        await asyncio.wait_for(inbox.wait_for_item(), timeout=0.5)
        return inbox.qsize()

    # Act
    sz = asyncio.run(_scenario())
    # Assert
    assert sz == 1


def test_wakeable_inbox_get_waits_when_empty_and_returns_on_put():
    # Arrange — sanity that asyncio.Queue semantics are preserved (get
    # blocks until a put arrives).
    async def _scenario():
        inbox = make_inbox()
        observed: list[str] = []

        async def _consumer():
            env = await inbox.get()
            observed.append(env.text)

        consumer = asyncio.create_task(_consumer())
        await asyncio.sleep(0.01)
        assert observed == []
        await inbox.put(_make_env("late"))
        await asyncio.wait_for(consumer, timeout=1.0)
        return observed

    # Act
    seen = asyncio.run(_scenario())
    # Assert
    assert seen == ["late"]


def test_wakeable_inbox_two_concurrent_waiters_both_wake():
    # Arrange — wait_for_item must broadcast to all concurrent awaiters
    # (asyncio.Event semantics). The conversation loop's wake task and a
    # future test/diagnostic listener might both be waiting; both should
    # wake on a single put.
    async def _scenario():
        inbox = make_inbox()
        woke: list[str] = []

        async def _a():
            await inbox.wait_for_item()
            woke.append("a")

        async def _b():
            await inbox.wait_for_item()
            woke.append("b")

        ta = asyncio.create_task(_a())
        tb = asyncio.create_task(_b())
        await asyncio.sleep(0.01)
        await inbox.put(_make_env())
        await asyncio.wait_for(asyncio.gather(ta, tb), timeout=1.0)
        return sorted(woke)

    # Act
    woke = asyncio.run(_scenario())
    # Assert
    assert woke == ["a", "b"]


def test_wakeable_inbox_get_nowait_returns_item_synchronously():
    # Arrange — _drain_failed_inbox in _session_conversation calls
    # inbox.get_nowait() (synchronous, no await) to resolve futures
    # with a startup error. WakeableInbox MUST keep the asyncio.Queue
    # surface for that path.
    async def _scenario():
        inbox = make_inbox()
        await inbox.put(_make_env("now"))
        env = inbox.get_nowait()
        return env.text, inbox.empty()

    # Act
    text, empty_after = asyncio.run(_scenario())
    # Assert
    assert (text, empty_after) == ("now", True)


def test_wakeable_inbox_get_nowait_raises_queue_empty_on_empty():
    # Arrange / Act / Assert — must surface asyncio.QueueEmpty so
    # callers like _drain_failed_inbox can break their drain loop on
    # the same exception type they always have.
    async def _scenario():
        inbox = make_inbox()
        try:
            inbox.get_nowait()
        except asyncio.QueueEmpty:
            return "queue-empty"
        return "no-raise"

    assert asyncio.run(_scenario()) == "queue-empty"


def test_wakeable_inbox_get_nowait_clears_event_when_drains_last():
    # Arrange — full drain via get_nowait must clear the wake event
    # (same invariant as the async get).
    async def _scenario():
        inbox = make_inbox()
        await inbox.put(_make_env())
        inbox.get_nowait()
        # wait_for_item must block now.
        try:
            await asyncio.wait_for(inbox.wait_for_item(), timeout=0.1)
        except asyncio.TimeoutError:
            return "blocked"
        return "did-not-block"

    assert asyncio.run(_scenario()) == "blocked"


def test_wakeable_inbox_qsize_and_empty_match_asyncio_queue():
    # Arrange / Act
    async def _scenario():
        inbox = make_inbox()
        results: list[tuple[bool, int]] = [(inbox.empty(), inbox.qsize())]
        await inbox.put(_make_env())
        results.append((inbox.empty(), inbox.qsize()))
        await inbox.get()
        results.append((inbox.empty(), inbox.qsize()))
        return results

    # Assert
    results = asyncio.run(_scenario())
    assert results == [(True, 0), (False, 1), (True, 0)]
