"""Semi-automated ``claude /login`` re-auth driver.

The ONE step an agent cannot do is the browser authorization that trips
bot-detection. This module automates everything around it: it drives an
interactive ``claude`` session inside a tmux pane, extracts the OAuth
*authorize* URL, delivers it to the operator (so all he does is tap the
link on his phone), then waits for the login to complete — either the
browser-only flow (the CLI finishes on its own) or the code-paste flow
(the operator sends back a code that gets typed into the pane).

Design rules (mirror the fleet's fail-loud + no-secret-leak doctrine):

* **Never** print / log / notify a token or credential value. Only the
  auth URL (a public authorization URL) and the account name are
  emitted. Any pane text shown on failure is run through
  :func:`redact_pane` first (the shared secret-shaped matcher).
* Every wait is time-bounded and fails loud with a redacted pane tail —
  never an unbounded interactive hang (the "silent stall" class).
* The tmux automation is REUSED, not re-invented:
  :class:`~scitex_agent_container._runners._tmux.tmux.TmuxManager` owns
  session lifecycle + keystrokes; this module only adds a ``-J`` (joined)
  capture so a long, wrapped OAuth URL reconstructs exactly.

The credential *save* (snapshot the fresh ``~/.claude/.credentials.json``
into the account store) is intentionally NOT done here — the CLI wrapper
runs the existing ``sac accounts save`` logic after this returns, so this
flow stays driveable end-to-end against a fake ``claude`` in tests.
"""

from __future__ import annotations

import os
import re
import subprocess
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .._runners._tmux.tmux import TmuxManager, TuiInputNotReadyError

# ---------------------------------------------------------------------------
# Detection markers + the strict OAuth-URL regex
# ---------------------------------------------------------------------------

# Known OAuth hosts for Claude Code. ``platform.claude.com`` is the live
# endpoint (``console.anthropic.com`` was retired — it 404s, per the
# 2026-07-10 account-pool incident); ``claude.ai`` serves the
# subscription authorize page. Anchored on ``https://`` + a known host +
# a ``/oauth`` or ``/authorize`` path so a stray string can never be
# mistaken for the auth URL — we fail loud rather than deliver a wrong one.
_OAUTH_URL_RE = re.compile(
    r"https://(?:claude\.ai|platform\.claude\.com|console\.anthropic\.com)"
    r"/(?:oauth|authorize)[^\s'\"<>\\]*"
)

_INPUT_READY_MARKER = "? for shortcuts"

# The login-method picker shown by ``/login`` (or auto-shown at boot when
# credentials are expired). The subscription (Pro/Max) flow is option 1.
_LOGIN_METHOD_MARKER = "select login method"

# The pane is asking the operator to paste the authorization code back.
# Matched case-insensitively; deliberately distinct from the URL's own
# ``code=true`` query param so the URL never trips the code-paste branch.
_CODE_PROMPT_MARKERS = (
    "paste code",
    "paste the code",
    "paste your code",
    "enter the code",
    "authorization code",
    "paste it below",
)

# Login completed successfully. Broad on purpose (the exact wording has
# varied across Claude Code builds); overridable by a caller that has
# verified a newer marker against a live TUI.
_SUCCESS_MARKERS = (
    "login successful",
    "logged in as",
    "successfully logged in",
    "you are now logged in",
    "already logged in",
    "authentication successful",
)


class LoginError(RuntimeError):
    """A ``claude /login`` drive failed loud (start / channel / usage)."""


class LoginTimeoutError(LoginError):
    """A bounded wait elapsed — carries a redacted pane tail in its text."""


@dataclass
class LoginResult:
    """Outcome of a completed interactive login (no secret material)."""

    url: str
    status: str
    code_used: bool


# ---------------------------------------------------------------------------
# Pure detection / redaction helpers (unit-testable without tmux)
# ---------------------------------------------------------------------------


def extract_oauth_url(text: str) -> str | None:
    """Return the first OAuth authorize URL in ``text``, or ``None``.

    Strict: only a ``https://<known-host>/oauth|authorize...`` token
    matches. Everything else — including an unrelated ``https://`` URL —
    yields ``None`` so the caller fails loud instead of delivering a
    wrong string.
    """
    if not text:
        return None
    match = _OAUTH_URL_RE.search(text)
    return match.group(0) if match else None


def is_login_method_picker(text: str) -> bool:
    """True iff the ``Select login method:`` picker is on screen."""
    return _LOGIN_METHOD_MARKER in (text or "").lower()


def is_code_prompt(text: str) -> bool:
    """True iff the pane is prompting for the pasted authorization code."""
    low = (text or "").lower()
    return any(marker in low for marker in _CODE_PROMPT_MARKERS)


def is_login_success(text: str) -> bool:
    """True iff the pane shows a completed-login marker."""
    low = (text or "").lower()
    return any(marker in low for marker in _SUCCESS_MARKERS)


def redact_pane(text: str, *extra_secrets: str) -> str:
    """Redact secret-shaped substrings from ``text`` before it is shown.

    Reuses the shared multi-line secret matcher
    (``_state._meta.secrets._redact_secrets`` — the same one that
    sanitises captured pane text elsewhere) and additionally masks any
    ``extra_secrets`` the caller knows are sensitive (e.g. a just-typed
    auth code, which is not token-shaped and would otherwise survive).
    """
    from .._state._meta.secrets import _redact_secrets

    out = _redact_secrets(text or "")
    for secret in extra_secrets:
        if secret and secret.strip():
            out = out.replace(secret, "***REDACTED***")
    return out


def _pane_tail(text: str, lines: int = 25) -> str:
    """Last ``lines`` non-empty rows of ``text`` (the actionable tail)."""
    rows = [row for row in (text or "").splitlines() if row.strip()]
    return "\n".join(rows[-lines:])


# ---------------------------------------------------------------------------
# tmux capture / sizing (the only additions on top of TmuxManager)
# ---------------------------------------------------------------------------


def _capture_joined(session: str) -> str:
    """``capture-pane -p -J`` — join soft-wrapped rows into logical lines.

    A real OAuth URL is ~300 chars and soft-wraps across several pane
    rows; ``-J`` reconstructs it as one line so :func:`extract_oauth_url`
    sees the whole URL, not a truncated first fragment.
    """
    result = subprocess.run(
        ["tmux", "capture-pane", "-t", session, "-p", "-J"],
        capture_output=True,
        text=True,
        check=False,
    )
    return result.stdout if result.returncode == 0 else ""


def _resize_wide(session: str, width: int = 400, height: int = 50) -> None:
    """Best-effort: widen the detached pane so the URL avoids a hard wrap.

    Belt-and-suspenders with the ``-J`` capture — a no-op / error on a
    session whose ``window-size`` forbids manual sizing is fine.
    """
    # stx-allow: fallback (reason: cosmetic pane widening; the -J capture
    # is the real de-wrap safety net, so a resize failure is irrelevant.)
    try:
        subprocess.run(
            ["tmux", "resize-window", "-t", session, "-x", str(width), "-y", str(height)],
            capture_output=True,
            check=False,
        )
    except Exception:  # stx-allow: fallback (reason: see inline comment)
        pass


# ---------------------------------------------------------------------------
# Delivery — URL to the operator without importing claude-code-telegrammer
# ---------------------------------------------------------------------------


def _default_notifier(url: str, name: str) -> None:
    """Push the auth URL to the operator via the existing lead-inbox rail.

    Uses :func:`scitex_agent_container._state.lead_inbox.push_to_lead` —
    the same operator-visible A2A push ``sac fleet notify`` uses. The
    lead session surfaces it on the operator's Telegram bridge; NOTHING
    imports claude-code-telegrammer. Raises ``LeadInboxError`` on
    failure, which the caller downgrades to a stdout-only warning.
    """
    from .._state.lead_inbox import push_to_lead

    sender = os.environ.get("SAC_NAME", "").strip() or "sac-accounts-login"
    push_to_lead(
        kind="status",
        summary=f"claude /login: authorize account '{name}' (tap to open in browser)",
        from_agent=sender,
        detail=url,
    )


def deliver_url(
    url: str,
    name: str,
    *,
    notify: bool = True,
    notifier: Callable[[str, str], None] | None = None,
    echo: Callable[[str], None],
) -> None:
    """Emit the auth URL: ALWAYS to stdout, and (best-effort) the rail.

    stdout is the guaranteed channel (a terminal operator sees it); the
    notify rail is a pluggable enhancement. A notify failure is logged
    and swallowed — it must never abort a login whose URL already
    reached stdout.
    """
    echo("")
    echo(f"[sac accounts login] Authorize account '{name}' — open this URL in a browser:")
    echo(url)
    echo("")
    if not notify:
        return
    fn = notifier or _default_notifier
    # stx-allow: fallback (reason: the notify rail is a best-effort
    # enhancement; stdout already delivered the URL, so a rail outage must
    # be a warning, never a failed login.)
    try:
        fn(url, name)
        echo("[sac accounts login] URL also pushed to the operator via the notify rail.")
    except Exception as exc:  # stx-allow: fallback (reason: see inline comment)
        echo(
            "[sac accounts login] warning: notify rail unavailable "
            f"({type(exc).__name__}: {exc}); the URL above on stdout is authoritative."
        )


# ---------------------------------------------------------------------------
# Code acquisition (stdin prompt + file/env drop point)
# ---------------------------------------------------------------------------


def _read_text(path: Path) -> str:
    """Best-effort read of ``path``; ``""`` when missing/unreadable."""
    # stx-allow: fallback (reason: the code-file is polled while a remote
    # deliverer writes it; a not-yet-present / mid-write file is expected
    # and must read as empty, never raise.)
    try:
        return path.read_text(encoding="utf-8")
    except Exception:  # stx-allow: fallback (reason: see inline comment)
        return ""


def _acquire_code(
    *,
    code_file: str | None,
    env_var: str | None,
    deadline: float,
    poll_s: float,
    echo: Callable[[str], None],
    isatty_fn: Callable[[], bool] = lambda: os.isatty(0),
    prompt_fn: Callable[[str], str] = input,
    read_fn: Callable[[Path], str] = _read_text,
    sleep_fn: Callable[[float], None] = time.sleep,
    time_fn: Callable[[], float] = time.monotonic,
) -> str:
    """Obtain the pasted auth code from an env var, a polled file, or stdin.

    Precedence: ``$env_var`` (immediate) → ``--code-file`` (polled until
    non-empty or ``deadline``) → interactive stdin prompt (only when
    stdin is a tty). Raises loud when none is available so the flow can't
    stall on an unanswerable prompt. The code is never echoed.
    """
    if env_var:
        val = os.environ.get(env_var, "").strip()
        if val:
            return val
    if code_file:
        path = Path(code_file)
        echo(f"[sac accounts login] waiting for the auth code at {path} ...")
        while time_fn() < deadline:
            content = read_fn(path)
            if content and content.strip():
                return content.strip().splitlines()[0].strip()
            sleep_fn(poll_s)
        raise LoginTimeoutError(
            f"no auth code appeared in {path} before the timeout elapsed."
        )
    if isatty_fn():
        return prompt_fn(
            "Paste the auth code from the browser and press Enter: "
        ).strip()
    raise LoginError(
        "claude is asking for an auth code but no delivery channel is available. "
        "Re-run with --code-file PATH (a deliverer writes the code there), set "
        f"${env_var or 'SAC_LOGIN_CODE'}, or run in an interactive terminal."
    )


# ---------------------------------------------------------------------------
# Drive loops
# ---------------------------------------------------------------------------


def _await_oauth_url(
    session: str,
    *,
    url_timeout_s: float,
    poll_s: float,
    echo: Callable[[str], None],
    capture_fn: Callable[[str], str] = _capture_joined,
    send_keys_fn: Callable[..., None] = TmuxManager.send_keys,
    send_text_fn: Callable[[str, str], None] = TmuxManager.send_text_and_submit,
    sleep_fn: Callable[[float], None] = time.sleep,
    time_fn: Callable[[], float] = time.monotonic,
) -> str:
    """Drive the pane to the OAuth URL and return it.

    Handles both entry states: a healthy REPL (send ``/login`` once the
    input marker is up) and an expired-credential boot (claude auto-shows
    the login picker without a REPL). Answers the login-method picker
    with the subscription option (1). Fails loud with a redacted tail if
    no URL appears within ``url_timeout_s``.
    """
    deadline = time_fn() + url_timeout_s
    login_sent = False
    picker_answered = False
    last = ""
    while time_fn() < deadline:
        last = capture_fn(session)
        url = extract_oauth_url(last)
        if url:
            return url
        if is_login_method_picker(last) and not picker_answered:
            send_keys_fn(session, "1", "Enter")  # subscription flow
            picker_answered = True
            sleep_fn(poll_s)
            continue
        if not login_sent and _INPUT_READY_MARKER in last:
            send_text_fn(session, "/login")
            login_sent = True
            sleep_fn(poll_s)
            continue
        sleep_fn(poll_s)
    raise LoginTimeoutError(
        f"the OAuth authorize URL never appeared within {url_timeout_s:.0f}s "
        f"(login_sent={login_sent}, picker_answered={picker_answered}). "
        f"Pane tail:\n{redact_pane(_pane_tail(last))}"
    )


def _await_completion(
    session: str,
    name: str,
    *,
    code_file: str | None,
    env_var: str | None,
    human_timeout_s: float,
    poll_s: float,
    echo: Callable[[str], None],
    capture_fn: Callable[[str], str] = _capture_joined,
    send_text_fn: Callable[[str, str], None] = TmuxManager.send_text_and_submit,
    sleep_fn: Callable[[float], None] = time.sleep,
    time_fn: Callable[[], float] = time.monotonic,
) -> bool:
    """Wait for login to complete; type the code if a code-prompt appears.

    Returns whether a code was pasted. Bounded by ``human_timeout_s`` (the
    human browser step) and fails loud with a redacted tail — the pasted
    code is scrubbed from that tail too.
    """
    deadline = time_fn() + human_timeout_s
    code_sent = False
    sent_code: str | None = None
    last = ""
    while time_fn() < deadline:
        last = capture_fn(session)
        if is_login_success(last):
            return code_sent
        if not code_sent and is_code_prompt(last):
            sent_code = _acquire_code(
                code_file=code_file,
                env_var=env_var,
                deadline=deadline,
                poll_s=poll_s,
                echo=echo,
            )
            send_text_fn(session, sent_code)
            code_sent = True
            echo("[sac accounts login] auth code delivered to the pane; waiting for completion ...")
            sleep_fn(poll_s)
            continue
        sleep_fn(poll_s)
    raise LoginTimeoutError(
        f"login for account {name!r} did not complete within {human_timeout_s:.0f}s "
        f"(code_pasted={code_sent}). Pane tail:\n"
        f"{redact_pane(_pane_tail(last), sent_code or '')}"
    )


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------


def _session_name(name: str) -> str:
    """A tmux-safe unique session name for this login attempt."""
    slug = re.sub(r"[^A-Za-z0-9_-]", "-", name)[:32]
    return f"sac-login-{slug}-{uuid.uuid4().hex[:8]}"


def run_interactive_login(
    name: str,
    *,
    notify: bool = True,
    notifier: Callable[[str, str], None] | None = None,
    code_file: str | None = None,
    env_var: str | None = "SAC_LOGIN_CODE",
    human_timeout_s: float = 600.0,
    url_timeout_s: float = 120.0,
    poll_s: float = 0.5,
    claude_bin: str = "claude",
    workdir: str | None = None,
    session_name: str | None = None,
    echo: Callable[[str], None] | None = None,
) -> LoginResult:
    """Drive ``claude /login`` end-to-end and return the outcome.

    Spawns ``claude`` in a tmux pane, extracts + delivers the OAuth URL,
    waits for the operator to authorize (browser-only or code-paste),
    and returns a :class:`LoginResult`. Does NOT touch the credential
    store — the caller runs ``sac accounts save`` after this returns.

    Raises :class:`LoginError` / :class:`LoginTimeoutError` (loud, with a
    redacted pane tail) on any failure; the tmux session is always
    cleaned up.
    """
    import click

    say = echo or click.echo
    session = session_name or _session_name(name)
    work = workdir or str(Path.home())

    started = TmuxManager.start(session, command=claude_bin, workdir=work)
    if not started:
        raise LoginError(
            f"failed to start a tmux session for claude (session={session!r}, "
            f"workdir={work!r}). Is tmux installed and is {claude_bin!r} on PATH?"
        )
    try:
        _resize_wide(session)
        try:
            url = _await_oauth_url(
                session,
                url_timeout_s=url_timeout_s,
                poll_s=poll_s,
                echo=say,
            )
        except TuiInputNotReadyError as exc:  # never raised here today, kept for safety
            raise LoginTimeoutError(str(exc)) from exc
        deliver_url(url, name, notify=notify, notifier=notifier, echo=say)
        code_used = _await_completion(
            session,
            name,
            code_file=code_file,
            env_var=env_var,
            human_timeout_s=human_timeout_s,
            poll_s=poll_s,
            echo=say,
        )
        say(f"[sac accounts login] login completed for account '{name}'.")
        return LoginResult(url=url, status="success", code_used=code_used)
    finally:
        TmuxManager.stop(session)
