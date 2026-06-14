"""Multiplexer abstraction — dispatches to screen or tmux."""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from ..config import AgentConfig


class MultiplexerProtocol(Protocol):
    """Common interface for screen/tmux managers."""

    @staticmethod
    def exists(session_name: str) -> bool: ...

    @staticmethod
    def start(
        session_name: str,
        command: str,
        workdir: str,
        env_exports: str = "",
        venv: str = "",
    ) -> bool: ...

    @staticmethod
    def stop(session_name: str) -> bool: ...

    @staticmethod
    def session_activity(session_name: str) -> int | None: ...

    @staticmethod
    def capture_content(session_name: str) -> str: ...

    @staticmethod
    def capture_logs(session_name: str, lines: int = 50) -> str: ...

    @staticmethod
    def send_keys(session_name: str, *keys: str) -> None: ...

    @staticmethod
    def send_text_and_submit(session_name: str, text: str) -> None: ...

    @staticmethod
    def send_text_and_submit_verified(
        session_name: str,
        text: str,
        **kwargs: object,
    ) -> int:
        """Send text + Enter with echo-verify retry. See
        :meth:`TmuxManager.send_text_and_submit_verified`. Returns the
        1-indexed attempt number that succeeded.

        Added 2026-06-14 (TUI Ink-drop fix, lead a2a
        ``910ff436642948eb85f8b3100204ed9b``) — every multiplexer the
        TUI runtime drives must expose this. ScreenManager (not in
        tree but referenced by get_multiplexer) is unused by the
        active fleet today; once it lands, it inherits the same
        contract.
        """
        ...

    @staticmethod
    def attach(session_name: str) -> None: ...


def get_multiplexer(config: AgentConfig) -> type:
    """Return the appropriate multiplexer class based on config."""
    if config.multiplexer == "tmux":
        from .tmux import TmuxManager

        return TmuxManager
    from .screen import ScreenManager

    return ScreenManager
