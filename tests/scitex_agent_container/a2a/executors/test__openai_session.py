"""A2A executor test suite for ``_openai_session.py`` (openai-compat-2).

Mirrors the coverage the ``_claude_session`` executor has across
``test__base.py`` (module/mirror smoke) and ``test__handlers.py``
(registry membership + missing-SDK HandlerError path), applied to the
``openai_session`` sibling. The missing-SDK path uses the same
import-blocker technique as the ``stub_claude_sdk_without_symbols``
fixture — a real interpreter behavior (``sys.modules[name] = None`` ⇒
``ModuleNotFoundError``), no ``unittest.mock``.

STX-TQ002 AAA + STX-TQ007 one-assert-per-test.
"""

from __future__ import annotations

import sys

import pytest

from scitex_agent_container.a2a import _handlers as h
from scitex_agent_container.a2a.executors import EXECUTORS
from scitex_agent_container.a2a.executors._base import BaseSyncExecutor
from scitex_agent_container.a2a.executors._openai_session import (
    OpenAISessionExecutor,
)


@pytest.fixture
def block_agents_import():
    """Force ``import agents`` to raise ModuleNotFoundError; restore after."""
    real = sys.modules.get("agents")
    sys.modules["agents"] = None  # type: ignore[assignment]
    try:
        yield
    finally:
        if real is None:
            sys.modules.pop("agents", None)
        else:
            sys.modules["agents"] = real


# ---------------------------------------------------------------------------
# Structural — same assertions the claude_session sibling gets
# ---------------------------------------------------------------------------


def test_openai_session_executor_class_loads() -> None:
    # Arrange
    target = OpenAISessionExecutor
    # Act
    is_class = isinstance(target, type)
    # Assert
    assert is_class is True


def test_openai_session_executor_subclasses_base_sync_executor() -> None:
    # Arrange
    target = OpenAISessionExecutor
    # Act
    is_subclass = issubclass(target, BaseSyncExecutor)
    # Assert
    assert is_subclass is True


def test_openai_session_executor_handler_key() -> None:
    # Arrange
    executor = OpenAISessionExecutor("alpha")
    # Act
    key = executor.handler_key
    # Assert
    assert key == "openai_session"


def test_executors_registry_maps_openai_session_key() -> None:
    # Arrange
    registry = EXECUTORS
    # Act
    mapped = registry["openai_session"]
    # Assert
    assert mapped is OpenAISessionExecutor


def test_handlers_registry_maps_openai_session_key() -> None:
    # Arrange
    registry = h.HANDLERS
    # Act
    mapped = registry["openai_session"]
    # Assert
    assert mapped is h.handle_openai_session


# ---------------------------------------------------------------------------
# Dispatch — missing SDK surfaces as HandlerError (same bar as the
# claude_session missing-SDK test)
# ---------------------------------------------------------------------------


def test_run_sync_without_openai_agents_raises_handler_error(
    block_agents_import,
) -> None:
    # Arrange
    executor = OpenAISessionExecutor("alpha")
    raised: Exception | None = None
    # Act
    try:
        executor._run_sync("alpha", "hi")
    except h.HandlerError as exc:
        raised = exc
    # Assert
    assert raised is not None


def test_run_sync_missing_sdk_error_names_the_extra(block_agents_import) -> None:
    # Arrange
    executor = OpenAISessionExecutor("alpha")
    message = ""
    # Act
    try:
        executor._run_sync("alpha", "hi")
    except h.HandlerError as exc:
        message = str(exc)
    # Assert
    assert "openai-agents" in message
