"""tmux-pane capture, subagent counting, and pane-state classification.

Extracted from ``agent_meta.py`` to keep that module under the 512-line
hook ceiling. ``agent_meta`` re-exports every helper here so existing
test code (``agent_meta._capture_pane`` etc.) keeps working.
"""

from __future__ import annotations

import re
import subprocess

_SUBAGENT_MARKER_RE = re.compile(
    r"(\d+)\s+local\s+agents?(?:\s+still)?\s+running",
    re.IGNORECASE,
)

# A Claude OAuth authorize URL on the post-``/login`` screen. Mirrors the
# extraction regex in ``_notify.login_relay`` (kept local so this low-level
# classifier carries no _notify import). tmux ``-J`` capture joins wrapped
# lines, so the long URL appears on one logical line.
_OAUTH_URL_RE = re.compile(r"https?://[^\s'\"<>`]*oauth[^\s'\"<>`]*", re.IGNORECASE)


def parse_subagent_count_from_pane_text(pane: str) -> int:
    """Return the subagent count advertised by Claude Code's status marker.

    Claude Code emits a line of the form ``N local agent(s) running`` (or
    ``... still running``) in the tmux pane while subagent ``Agent``
    calls are in flight. Match that marker (anchored on the literal
    ``running`` trailer so chat text that merely mentions "local agent"
    can't false-positive us). Anything else (no marker, empty pane) is
    reported as ``0``.
    """
    if not pane:
        return 0
    m = _SUBAGENT_MARKER_RE.search(pane)
    return int(m.group(1)) if m else 0


def _subagent_count_from_pane(session: str, multiplexer: str) -> int:
    if multiplexer != "tmux":
        return 0
    # stx-allow: fallback (reason: session may not exist yet; 0 is the
    # correct "unknown" sentinel — never block a heartbeat on tmux error)
    try:
        pane = subprocess.run(
            ["tmux", "capture-pane", "-t", session, "-p"],
            capture_output=True,
            text=True,
        ).stdout
    except Exception:  # stx-allow: fallback (reason: catch-all safety net — see inline comment for context)
        return 0
    return parse_subagent_count_from_pane_text(pane)


def _capture_pane(session: str, multiplexer: str, max_chars: int = 10_000) -> str:
    """Return the current tmux pane contents, truncated. Empty on error."""
    if multiplexer != "tmux":
        return ""
    # stx-allow: fallback (reason: session may have exited between the
    # has-session check and capture-pane — empty string is safe for callers)
    try:
        out = (
            subprocess.run(
                ["tmux", "capture-pane", "-t", session, "-p", "-J"],
                capture_output=True,
                text=True,
            ).stdout
            or ""
        )
    except Exception:  # stx-allow: fallback (reason: catch-all safety net — see inline comment for context)
        return ""
    if len(out) > max_chars:
        out = out[-max_chars:]
    return out


def _classify_pane_state(pane_text: str) -> tuple[str, str]:
    """Heuristic pane-state classifier. Returns (state, stuck_prompt_text).

    States:
      - "running": agent is actively working (prompt >_ present, no stuck marker)
      - "idle_prompt": prompt visible, no recent activity
      - "y_n_prompt": y/n prompt blocking
      - "auth_error": credential error shown
      - "login_url": OAuth authorize screen shown after /login (URL in snippet)
      - "compose_pending_unsent": user text typed but not yet submitted
      - "limit_reached": Anthropic rate limit warning visible
      - "unknown": nothing matched
    """
    if not pane_text:
        return "unknown", ""
    tail = pane_text[-2_000:]
    lower = tail.lower()
    # login_url: the OAuth authorize screen Claude shows after `/login`.
    # Checked before auth_error because that screen can also carry auth
    # wording. Require a paste/authorize/login cue alongside the URL so a
    # working agent that merely prints an oauth link is not misread as
    # needing login.
    _oauth = _OAUTH_URL_RE.search(tail)
    if _oauth and (
        "paste" in lower
        or "authoriz" in lower
        or "sign in" in lower
        or "log in" in lower
        or "/login" in lower
    ):
        return "login_url", _oauth.group(0)[:300]
    # auth_error patterns — Claude Code surfaces a few wordings depending on
    # which auth path failed. Keep the list literal-string so adding new
    # variants is obvious; lower-case match because the source casing varies.
    _auth_markers = (
        "invalid api key",
        "invalid authentication credentials",  # Anthropic API 401 body
        "please re-run /login",
        "please run /login",  # Claude Code 2.1.x wording
        "authentication_error",  # raw API error type
    )
    if any(m in lower for m in _auth_markers):
        # Return the line that actually contains the auth marker so the
        # snippet is operationally useful (a human reading the dashboard
        # sees "/login" / "401" rather than the trailing bare prompt).
        snippet = ""
        for ln in reversed(tail.strip().splitlines()):
            if any(m in ln.lower() for m in _auth_markers):
                snippet = ln.strip()[:200]
                break
        if not snippet:
            snippet = tail.strip().splitlines()[-1][:200]
        return "auth_error", snippet
    if "limit reached" in lower or "resets in" in lower:
        return "limit_reached", ""
    if re.search(r"\(y/n\)|\[y/n\]|\(yes/no\)|\[yes/no\]", lower):
        return "y_n_prompt", tail.strip().splitlines()[-1][:200]
    # compose_pending: ❯ followed by non-whitespace on the SAME line.
    # Use `[^\s\n]` (non-newline, non-whitespace) to avoid matching the
    # decorative dashed separator that lives a line below the empty
    # prompt — earlier `❯\s+\S` greedily crossed the newline and lit
    # compose_pending for every freshly-booted agent.
    # The gap class includes U+00A0 NBSP: Claude's Ink TUI renders the prompt
    # as ``❯\xa0[Pasted text …]`` (NBSP, not ASCII space), so a bare ``[ \t]``
    # missed every pasted-but-unsent buffer (proj-scitex-dev 2026-06-23).
    if re.search(r"❯[ \t\xa0]+\S", tail):
        return "compose_pending_unsent", ""
    if "❯" in tail or ">" in tail:
        return "running", ""
    return "unknown", ""

    # Note: "waiting" (freshly booted, never received work) is intentionally
    # NOT detected here. The earlier draft relied on the claude-hud
    # statusline `Context ░░░░░░░░░░ 0%` marker, but claude-hud is an
    # external tool not present in every install. The dashboard derives
    # "waiting" instead from the hub-side `last_tool_at` field — an agent
    # that is connected but has never recorded a tool call is waiting,
    # regardless of what its pane statusline looks like.
