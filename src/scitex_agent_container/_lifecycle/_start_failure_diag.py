"""Diagnose + persist a failed ``runtime.start()`` — the fail-loud boot report.

Extracted from :mod:`._start` (512-line per-file cap; ``_start`` re-exports
:func:`_format_boot_stderr_section` so existing importers are unchanged).
One cohesive responsibility: when ``runtime.start()`` returns a bare ``False``,
turn that into a LOUD, CAUSE-CARRYING error instead of a cause-less
"Failed to start".
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

__all__ = [
    "_format_boot_stderr_section",
    "capture_pane_diag",
    "fresh_retry_hint",
    "raise_start_failure",
]


def _indent_block(text: str, prefix: str = "      ") -> str:
    """Indent every line of a multi-line body so it reads as ONE sub-section.

    The start-failure report is a headline + labelled sections; each section's
    BODY (an inner-stderr tail, a pane capture) is indented under its label so
    the operator can see at a glance where one section ends and the next begins
    instead of reading a run-on wall (operator 2026-07-19). Empty-safe.
    """
    lines = text.splitlines() or [""]
    return "\n".join(f"{prefix}{line}" for line in lines)


def _format_boot_stderr_section(log: Path) -> str:
    """Formatted 'inner stderr' diagnostic section for a failed TUI start.

    B->A feedback / no silent fallback: ``TuiSessionRuntime`` redirects the
    inner ``apptainer exec … claude`` STDERR — where apptainer's FATAL mount
    errors and an immediate claude exit land — to ``<state>/boot.stderr.log``
    (``log``), which SURVIVES the tmux pane's death. Return its tail so a boot
    failure is the LOUD cause in the raised error, never a cause-less
    ``<empty>`` pane fallback. Empty/absent-log safe; never raises.

    Rendered as a labelled section (the ``inner stderr`` label at 2 spaces, the
    captured body indented under it at 6) so the report reads as distinct,
    separated sections rather than a flush-left blob.
    """
    tail = ""
    if log.is_file():
        tail = log.read_text(errors="replace")[-4_000:].rstrip()
    body = tail or "<no stderr captured — runtime never launched the process>"
    return f"\n  inner stderr ({log}):\n{_indent_block(body)}"


_NO_PANE = "\n  (no pane diagnostics available)"


def fresh_retry_hint(config: Any) -> str:
    """The one-shot ``--fresh`` escape hatch, as a message section.

    Operator 2026-08-18: 「フレッシュは基本的に使いません、最初の起動に必要な
    時だけで、スタートした時に失敗したら --fresh を今回だけはつけろ的なヒント
    をだしてください、スペックは全てレジュームで」 — every spec resumes; a
    start failure is the one moment where a single fresh retry is the right
    move, so the failure report is where that hint belongs.

    WHY IT IS CONDITIONAL. A start that was ALREADY fresh cannot be wedged on a
    resumed session, so naming ``--fresh`` there would be an error asserting a
    cause it did not observe — the same disease :func:`raise_start_failure`
    exists to kill. Emitted only when the resolved ``claude.session`` is
    something other than ``fresh``.

    WHY IT SAYS "ONCE". ``--fresh`` is a per-invocation override, not a spec
    edit, and the difference matters: the spec must stay on resume, because a
    fresh start DISCARDS that agent's working memory. Naming the boundary in
    the hint is the whole point — an unqualified "try --fresh" is how a
    one-shot recovery becomes a habit that quietly erases the fleet's memory.

    Never raises: ``config`` is whatever the caller had when the start blew up,
    and a diagnostic must not fail on a stub. An unreadable mode is treated as
    "not fresh" — the hint is advice, and printing it needlessly costs a line
    while withholding it costs the operator the recovery.
    """
    try:
        mode = str(getattr(getattr(config, "claude", None), "session", "") or "")
    except Exception:  # stx-allow: fallback (reason: a diagnostic must never raise over the failure it is describing)
        mode = ""
    if mode.strip().lower() == "fresh":
        return ""
    name = getattr(config, "name", "<name>")
    return (
        "\n  if this start was RESUMING a prior session: a session wedged on a "
        f"queued\n  boot prompt comes back on every plain restart. Retry ONCE "
        f"with\n    sac agents start {name} --fresh\n  ONCE — as a per-start "
        "override, not a spec edit. The spec stays on resume;\n  a fresh start "
        "discards this agent's working memory."
    )


def raise_start_failure(
    config: Any,
    *,
    capture_fn: Callable[[Any], str] | None = None,
) -> None:
    """Always raises. Capture WHY the start failed, persist it, then fail loud.

    A bare ``False`` from ``runtime.start()`` must not become a cause-less
    "Failed to start". Capture the agent's tmux pane (the inner
    apptainer/claude output) + whether a session exists, so the real boot
    failure — boot-drain timeout, auth, a broken in-container login shell, an
    immediate claude exit — is visible rather than swallowed.

    The capture is also PERSISTED to ``<state>/start_failure_diag.log``: a
    false-negative start leaves no registry row (we raise before
    ``registry.add()``), so killing the session by hand is often the only way
    to stop the agent — which destroys the live pane capture forever unless it
    is written to disk now. ``boot.stderr.log`` already survives pane death;
    the pane tail did not until this. See
    ``sac-agent-start-false-negative-tui-registry-row``.

    ``capture_fn`` is the injection seam for the BEST-EFFORT half (a real
    callable taking ``config`` and returning the message suffix; production uses
    :func:`capture_pane_diag`). It exists so the suite can drive a genuinely
    exploding tmux layer and still assert the record survives.
    """
    # STEP 1 — BEST-EFFORT: the pane capture. It is allowed to fail (no tmux, a
    # wedged server, an apptainer agent that has no pane at all). A failure here
    # must degrade the MESSAGE, never the PERSISTED RECORD.
    try:
        diag = (capture_fn or capture_pane_diag)(config)
    except Exception:  # stx-allow: fallback (reason: diagnostics must never mask the real start failure — degrade to no pane)
        diag = _NO_PANE

    # STEP 2 — MANDATORY: persist the record. This is the whole point of the
    # function, and it MUST NOT ride on step 1 succeeding.
    #
    # It used to. The write was the LAST statement inside step 1's try, under a
    # bare ``except Exception``, so ANY earlier hiccup threw, got swallowed, and
    # the diagnostic was silently discarded. The commonest hiccup is the
    # plainest: THE STATE DIR DOES NOT EXIST, because a start that failed early
    # never created it — so precisely when this record matters most, it was
    # thrown away. The caller was then handed "(no pane diagnostics available)":
    # a message blaming the PANE CAPTURE for a failure it never observed (it was
    # a missing directory). That is the same disease the liveness verdict next
    # door exists to kill — an error asserting a cause it did not see.
    # The recovery hint rides on the SAME suffix as the diagnostics so the
    # persisted record and the raised error carry it identically — a remedy
    # only the live terminal saw is lost to whoever reads the log tomorrow.
    suffix = f"{diag}{fresh_retry_hint(config)}"
    _persist_diag(config, suffix)

    raise RuntimeError(
        f"Failed to start agent '{config.name}': runtime.start() returned False.{suffix}"
    )


def _state_dir(config: Any) -> Path:
    """The agent's state dir. Resolved in one place so both steps agree."""
    from ..runtimes.tui_session import state_dir_for_config

    return state_dir_for_config(config)


def capture_pane_diag(config: Any) -> str:
    """BEST-EFFORT: the tmux pane tail + inner stderr, as a message suffix.

    The injectable collaborator of :func:`raise_start_failure`. Separated so the
    MANDATORY persist below cannot be taken down by a tmux hiccup, and so the
    suite can drive a genuinely exploding capture without patching internals.
    """
    from .._runners._tmux.tmux import TmuxManager
    from ..runtimes.tui_session import session_name_for

    sess = session_name_for(config)
    pane = TmuxManager.capture_logs(sess, lines=60).rstrip()
    boot_log = _state_dir(config) / "boot.stderr.log"
    return (
        f"\n  tmux session: exists={TmuxManager.exists(sess)} (session {sess!r})"
        f"{_format_boot_stderr_section(boot_log)}"
        f"\n  pane tail:\n{_indent_block(pane or '<empty>')}"
    )


def _persist_diag(config: Any, diag: str) -> None:
    """Write ``<state>/start_failure_diag.log``, CREATING the state dir if needed.

    ``mkdir(parents=True, exist_ok=True)`` is load-bearing, not defensive
    boilerplate: the agents this runs for are exactly the ones whose start
    FAILED, and a start that failed early enough never created its own state
    dir. ``write_text`` into a directory that does not exist raises
    ``FileNotFoundError`` — which is how the only durable evidence of a boot
    failure was being silently discarded.
    """
    try:
        log = _state_dir(config) / "start_failure_diag.log"
        log.parent.mkdir(parents=True, exist_ok=True)
        log.write_text(
            f"{datetime.now(timezone.utc).isoformat()} start failed for "
            f"{config.name!r}: runtime.start() returned False.{diag}\n"
        )
    except Exception:  # stx-allow: fallback (reason: an unwritable state dir must not mask the real start failure — the RuntimeError still carries the cause)
        pass
