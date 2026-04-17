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
    def capture_content(session_name: str) -> str: ...

    @staticmethod
    def capture_logs(session_name: str, lines: int = 50) -> str: ...

    @staticmethod
    def send_keys(session_name: str, *keys: str) -> None: ...

    @staticmethod
    def send_text_and_submit(session_name: str, text: str) -> None: ...

    @staticmethod
    def attach(session_name: str) -> None: ...


def get_multiplexer(config: AgentConfig) -> type:
    """Return the appropriate multiplexer class based on config."""
    if config.multiplexer == "tmux":
        from .tmux import TmuxManager

        return TmuxManager
    from .screen import ScreenManager

    return ScreenManager
