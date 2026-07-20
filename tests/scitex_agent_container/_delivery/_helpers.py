"""Hand-rolled seams for the verified-delivery suites. NO mocks, no monkeypatch.

:class:`ComposerPane` is not a stand-in for the thing under test — it is a small
REAL implementation of the one behaviour the production code reads: an Ink-style
compose box that accumulates pasted text and empties it when an ``Enter`` is
accepted. It renders an actual pane STRING, so every production detector in the
path (``_compose_pending_live``, ``pane_is_busy``, ``prompts``, the auth-banner
matcher, ``verify_submit_by_advancement``) runs for real against it. The tests
therefore exercise the shipping algorithms rather than a rehearsal of them.

It can also reproduce the two behaviours that caused the incident, because a test
that cannot reproduce the bug cannot prove the fix:

* ``drops_enter`` — the Ink TUI silently EATS an ``Enter`` (this is what happens
  when one is fired into its busy window), so the retry path can be driven and
  the "text sits unsubmitted" mode can be produced on demand;
* ``wrap_at`` — the renderer soft-wraps and draws a border through the middle of
  the pasted text, which is what made a prose grep return 0 for a message that
  had in fact arrived.
"""

from __future__ import annotations

from typing import Optional

__all__ = ["ComposerPane", "KeyRecorder", "PasteRecorder", "TickClock"]

_BUSY_MARKER = "esc to interrupt"
_BANNER = "● Login expired · Please run /login"


class ComposerPane:
    """A real compose box that renders a pane and answers ``Enter``.

    Parameters mirror the failure modes under test rather than the TUI's full
    behaviour — this is the smallest thing that makes the production detectors
    give true answers.
    """

    def __init__(
        self,
        *,
        readable: bool = True,
        busy_captures: int = 0,
        drops_enter: int = 0,
        banner: bool = False,
        wrap_at: int = 0,
    ) -> None:
        self.buffer = ""
        self.submitted: list[str] = []
        self.enters = 0
        self.captures = 0
        #: Enters that arrived while this pane was rendering a busy marker. The
        #: Ink TUI eats those, so the production code must never fire one — this
        #: counter is how a test proves the idle gate held rather than assuming it.
        self.enters_while_busy = 0
        self._readable = readable
        self._busy_captures = busy_captures
        self._drops_enter = drops_enter
        self._banner = banner
        self._wrap_at = wrap_at

    # --- the production signatures -----------------------------------------

    def capture(self, target: str) -> Optional[str]:
        """``capture_fn(session) -> str | None`` — ``None`` means UNREADABLE.

        Tri-state on purpose: the whole package turns on "uncapturable" never
        being spelled the same as "captured and clean".
        """
        self.captures += 1
        if not self._readable:
            return None
        busy = self.captures <= self._busy_captures
        return self._render(busy=busy)

    def paste(self, target: str, text: str) -> None:
        """``paste_fn(session, text)`` — a literal paste appends, never submits."""
        self.buffer += text

    def send_key(self, target: str, key: str) -> None:
        """``send_keys_fn(session, key)`` — only ``Enter`` submits, and it can be eaten."""
        if key != "Enter":
            return
        self.enters += 1
        if self.captures <= self._busy_captures:
            self.enters_while_busy += 1
        if self._drops_enter > 0:
            self._drops_enter -= 1
            return
        self.submitted.append(self.buffer)
        self.buffer = ""

    # --- rendering ----------------------------------------------------------

    def _compose_text(self) -> str:
        """The live compose row, optionally wrapped through a drawn border.

        ``wrap_at`` splits the buffer across rows and puts a ``│`` at each seam,
        which is exactly the artefact that defeats a naive substring search.
        """
        if not self.buffer:
            return "❯"
        if self._wrap_at <= 0:
            return f"❯ {self.buffer}"
        chunks = [
            self.buffer[i : i + self._wrap_at]
            for i in range(0, len(self.buffer), self._wrap_at)
        ]
        head, *rest = chunks
        rows = [f"❯ {head} │"] + [f"│ {chunk} │" for chunk in rest]
        return "\n".join(rows)

    def _render(self, *, busy: bool) -> str:
        lines: list[str] = []
        for turn in self.submitted:
            lines.append(f"  {turn}")
        if self._banner:
            lines.append(_BANNER)
        lines.append("─" * 20)
        lines.append(self._compose_text())
        lines.append("─" * 20)
        lines.append(f"  {_BUSY_MARKER}" if busy else "  ctx:1%")
        return "\n".join(lines) + "\n"


class PasteRecorder:
    """A real ``paste_fn(session, text)`` that records instead of pasting."""

    def __init__(self) -> None:
        self.pastes: list[tuple[str, str]] = []

    def __call__(self, session: str, text: str) -> None:
        self.pastes.append((session, text))


class KeyRecorder:
    """A real ``send_keys_fn(session, key)`` that records instead of sending."""

    def __init__(self) -> None:
        self.keys: list[tuple[str, str]] = []

    def __call__(self, session: str, key: str) -> None:
        self.keys.append((session, key))


class TickClock:
    """An injected clock whose ONLY advance is an explicit sleep.

    Reading the time never moves it, so a loop that forgets to sleep hangs in the
    test instead of passing on wall-clock luck — the bound is proven, not hoped
    for. Every wait budget in this package is therefore verified in milliseconds.
    """

    def __init__(self, start: float = 0.0) -> None:
        self.now_value = start
        self.slept: list[float] = []

    def now(self) -> float:
        return self.now_value

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now_value += seconds
