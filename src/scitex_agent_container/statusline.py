"""sac-statusline: Claude Code statusline command that persists the JSON payload.

Claude Code calls this command on each status-line update, piping a JSON payload
via stdin that contains context usage, rate-limits, model info, and session
details. We tee the payload to a per-agent JSON file so ``sac agent status`` can
report authoritative context data instead of the 1M-token JSONL approximation.

sac then renders the status line itself — one line naming the agent, host,
workdir, model, context, 5h quota and active account. There is no delegation
to any external renderer: what the pane shows is sac's decision on every host.

Usage (written by setup_settings_json into .claude/settings.local.json):
    { "statusLine": { "type": "command", "command": "sac-statusline" } }
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from ._env import getenv as _sac_env


def _state_dir() -> Path:
    """Resolve the statusline persist dir at call time.

    Honours ``$SAC_STATUSLINE_STATE_DIR`` as override (tests + ops);
    otherwise ``~/.scitex/agent-container/statusline``.
    """
    override = os.environ.get("SAC_STATUSLINE_STATE_DIR")
    if override:
        return Path(override)
    return Path.home() / ".scitex" / "agent-container" / "statusline"


def _agent_name() -> str:
    return _sac_env("AGENT") or os.environ.get("CLAUDE_AGENT_ID") or "unknown"


def _persist(raw: bytes, agent: str) -> None:
    state_dir = _state_dir()
    state_dir.mkdir(parents=True, exist_ok=True)
    tmp = state_dir / f"{agent}.json.tmp"
    out = state_dir / f"{agent}.json"
    # stx-allow: fallback (reason: disk-full or permission error must not crash
    # the Claude Code statusLine handler — missing persist is non-fatal)
    try:
        tmp.write_bytes(raw)
        tmp.rename(out)
    except OSError:
        pass


def _hostname() -> str:
    """Which machine this agent is really on.

    The fleet runs one agent name on several hosts over its life, and a
    duplicate on two hosts is a real incident (measured 2026-08-17: two
    `scitex-hub` sessions on compute-03 and compute-04 shared one bot token
    and split the operator's messages between them). The pane is where a
    human notices that first, so the host belongs in it.
    """
    # stx-allow: fallback (reason: a statusline must never raise; an
    # unresolvable hostname degrades to empty, not to a crash)
    try:
        import socket

        return socket.gethostname()
    except Exception:
        return ""


def _workdir(data: dict) -> str:
    """The directory this session is working in, from the payload or cwd."""
    # stx-allow: fallback (reason: display-only; any failure degrades to empty)
    try:
        ws = data.get("workspace") or {}
        cwd = ws.get("current_dir") or ws.get("cwd") or data.get("cwd")
        if not cwd:
            cwd = os.getcwd()
        return Path(str(cwd)).name or str(cwd)
    except Exception:
        return ""


def _active_account() -> str:
    """Which stored account the LIVE credential is, by content match.

    Deliberately local and O(number of accounts): the live
    ``~/.claude/.credentials.json`` is compared byte-for-byte against each
    stored snapshot. No network call — a statusline renders on every turn
    and must not depend on an API being reachable.

    NOTE this reports the account whose SNAPSHOT matches, i.e. it inherits
    the store's directory naming. The authoritative answer to "whose token
    is this" is ``/api/oauth/profile`` (see ``_account/account_identity``);
    that costs a round trip and belongs in ``accounts list``, not here.

    IN A CONTAINER ``$HOME`` IS ``/home/agent`` AND THE ACCOUNTS STORE IS NOT
    BOUND THERE — only the operator's home is. Measured 2026-08-17 while adding
    this: the first version looked solely under ``Path.home()`` and rendered
    ``acct:?`` for every agent, i.e. it was blank in precisely the place the
    field exists to serve. So both roots are searched, container home first.
    """
    # stx-allow: fallback (reason: display-only; unreadable store degrades to
    # empty rather than breaking the pane)
    try:
        live = Path.home() / ".claude" / ".credentials.json"
        if not live.is_file():
            return ""
        want = live.read_bytes()
        if not want:
            return ""
        roots = [
            Path.home() / ".scitex" / "agent-container" / "accounts",
            Path("/home/ywatanabe/.scitex/agent-container/accounts"),
        ]
        seen: set = set()
        for root in roots:
            resolved = str(root)
            if resolved in seen:
                continue
            seen.add(resolved)
            for cand in sorted(root.glob("*/.credentials.json")):
                if cand.read_bytes() == want:
                    return cand.parent.name
        return "?"
    except Exception:
        return ""


#: Hard width budget for the rendered line.
#:
#: The payload carries NO terminal width — measured 2026-08-17 against a live
#: capture: it has session, model, workspace, cost, context_window and
#: rate_limits, and nothing describing the pane. So this is a FLOOR we choose,
#: not a width we read: 80 columns is the classic minimum, and the line must
#: still fit after the TUI's own left decoration.
#:
#: Why a budget at all (operator, 2026-08-17): 「途中でトランケーションされて
#: しまってんですよ。なので情報が見えなくなってる…全部見えるように」. The line
#: was 131 chars and his pane cut it at ~86, mid-model-name. Terminal
#: truncation eats from the RIGHT, so it discarded ctx / 5h / account —
#: every NUMBER — while keeping the identity, which is the one part a human
#: already knows. The fix is to fit, not to reorder.
#:
#: 80 and not less: at 78 this agent's own line lost its MODEL, because the
#: full line measures exactly 80. Trimming the budget below the standard
#: terminal width buys nothing (nothing is 79 columns wide) and costs a field
#: on every turn, so the floor IS the budget.
STATUSLINE_MAX_WIDTH = 80

#: Marker for a clamp WE performed. A deliberate, visible ellipsis beats the
#: terminal's invisible one: the reader can tell the difference between "this
#: is the whole value" and "sac shortened this".
_CLAMP = "…"


def _short_host(host: str) -> str:
    """Drop the fleet-wide ``scitex-`` prefix every host name carries.

    ``scitex-compute-04`` -> ``compute-04``. Purely redundant: the prefix is
    on every host, so it distinguishes nothing while costing 7 columns on the
    one line where columns are scarce. What remains is still unique across the
    peer table (compute-01..04, nas-01..03, mba, spartan, ywata-note-win), so
    :func:`_hostname`'s stated purpose — noticing the same agent name alive on
    two machines — survives intact.
    """
    return host[len("scitex-") :] if host.startswith("scitex-") else host


def _short_model(model: str) -> str:
    """``Opus 5 (1M context)`` -> ``Opus 5 1M``; leave un-parenthesised names alone.

    The parenthetical is the payload's prose, not information: the only part
    that varies between models an operator might be surprised by is the size
    token itself. A name with no parenthetical (``claude-opus-4-7``) is passed
    through byte-for-byte.
    """
    head, sep, tail = model.partition("(")
    if not sep:
        return model
    head = head.strip()
    size = tail.split()[0].rstrip(")") if tail.split() else ""
    return f"{head} {size}".strip() if size else head


def _short_account(acct: str) -> str:
    """First dash-segment, which is the fleet's EXISTING key for an account.

    ``wyusuuke-gmail-com`` -> ``wyusuuke``. Not an invention: the quota cache
    already indexes accounts by exactly this segment under the name ``short``,
    and the stored accounts are distinct in it (scitex, ywatanabe, wyusuuke,
    ywata1989). So the pane and the quota cache name accounts the same way.
    """
    return acct.split("-", 1)[0] if "-" in acct else acct


def _render(data: dict) -> str:
    """Build the status line, guaranteed to fit :data:`STATUSLINE_MAX_WIDTH`.

    Field order and the sacrifice order are the whole design:

    * IDENTITY (``agent@host``) — who and where.
    * MODEL — compacted.
    * NUMBERS (``ctx`` / ``5h`` / ``7d``) — grouped into ONE segment separated
      by spaces rather than ``|``, which buys back four columns.
    * ACCOUNT — which credential is live.

    ``7d`` IS NEW AND IS THE POINT. Measured 2026-08-17: scitex-hub ran pinned
    to an account at 7d=100%, capped for days, and answered "You've hit your
    weekly limit" on every turn — while 5h read LOW and every other signal
    (SUCC, live tmux, rendered TUI) said healthy. The pane was displaying the
    reassuring number and hiding the fatal one, and ``seven_day`` was in the
    payload the whole time.

    WORKDIR IS DROPPED WHEN IT REPEATS THE AGENT NAME. sac repos are named
    after their agent, so the old line spent 24 columns printing
    ``scitex-agent-container`` a second time.

    When the line still does not fit, it is shortened in a stated order —
    model first (least volatile, and recoverable from ``sac agents list``),
    then the identity is clamped with a visible marker. The NUMBERS and the
    ACCOUNT are never dropped: they are why the line exists.
    """
    ctx_pct = (data.get("context_window") or {}).get("used_percentage", 0)

    host = _short_host(_hostname())
    agent = _agent_name()
    if agent and agent != "unknown":
        identity = f"{agent}@{host}" if host else agent
    else:
        identity = host

    model = _short_model((data.get("model") or {}).get("display_name", ""))

    nums = [f"ctx:{ctx_pct:.0f}%"]
    rl = data.get("rate_limits") or {}
    fh_pct = (rl.get("five_hour") or {}).get("used_percentage")
    if fh_pct is not None:
        nums.append(f"5h:{fh_pct:.0f}%")
    sd_pct = (rl.get("seven_day") or {}).get("used_percentage")
    if sd_pct is not None:
        nums.append(f"7d:{sd_pct:.0f}%")

    tail: list[str] = [" ".join(nums)]
    acct = _short_account(_active_account())
    if acct:
        tail.append(acct)

    wd = _workdir(data)
    if wd == agent:
        # sac repos are named after their agent, so the old line spent 24
        # columns printing the same string twice.
        wd = ""

    def _head(with_wd: bool, with_model: bool) -> list[str]:
        out = [identity] if identity else []
        if with_wd and wd:
            out.append(wd)
        if with_model and model:
            out.append(model)
        return out

    # Sacrifice order, widest-first and stated rather than emergent:
    #   1. WORKDIR — the weakest field. Normally the repo name, which is
    #      derivable from the agent; it only differs at all inside a worktree.
    #   2. MODEL — recoverable from `sac agents list`, and it changes rarely.
    #   3. IDENTITY — clamped, never dropped, and clamped VISIBLY.
    # The numbers and the account are never sacrificed: they are why the line
    # exists, and they are precisely what the terminal was eating before.
    for with_wd, with_model in ((True, True), (False, True), (False, False)):
        head = _head(with_wd, with_model)
        line = " | ".join(head + tail)
        if len(line) <= STATUSLINE_MAX_WIDTH:
            return line
    if not head:
        return line

    # 2. Clamp the identity, visibly. The numbers keep their columns.
    fixed = len(" | ".join(head[1:] + tail)) + len(" | ")
    room = STATUSLINE_MAX_WIDTH - fixed
    if room < len(_CLAMP) + 1:
        return " | ".join(head[1:] + tail)
    head[0] = head[0][: room - len(_CLAMP)] + _CLAMP
    return " | ".join(head + tail)


def _display(raw: bytes) -> None:
    # stx-allow: fallback (reason: statusLine display must never raise; corrupt
    # or unexpected payload shape silently outputs nothing rather than aborting)
    try:
        print(_render(json.loads(raw)), flush=True)
    except Exception:
        pass


def main(stdin=None) -> None:
    """Entry point for the sac-statusline command.

    ``stdin`` is an injection seam: defaults to ``sys.stdin`` so production
    callers are unchanged; tests pass a real bytes-stream.
    """
    if stdin is None:
        stdin = sys.stdin
    raw = stdin.buffer.read() if hasattr(stdin, "buffer") else stdin.read()
    if isinstance(raw, str):
        raw = raw.encode()
    agent = _agent_name()
    _persist(raw, agent)

    # sac RENDERS ITS OWN STATUS LINE — there is no delegation seam, by
    # operator ruling 2026-08-17: provide the status line from sac, because
    # then it is ours to control, and delete the rest cleanly.
    #
    # This function used to shell out to an external renderer whenever that
    # binary happened to be on PATH, which made the pane's contents depend on
    # what was installed rather than on what sac decided: the same agent showed
    # different information on two hosts, and the fields sac wants to guarantee
    # (host, workdir, account) could not be guaranteed at all. It was installed
    # on NEITHER compute-03 NOR compute-04 when measured, so every agent was
    # already rendering the line below — latent variance, not a live feature.
    _display(raw)


def read_statusline_json(agent_name: str) -> dict | None:
    """Read the last persisted statusline payload for ``agent_name``.

    Returns ``None`` if no file exists or the file is unreadable/stale.
    Callers should treat ``None`` as "no authoritative data, fall back".
    """
    path = _state_dir() / f"{agent_name}.json"
    if not path.exists():
        return None
    # stx-allow: fallback (reason: corrupt or truncated JSON returns None so
    # callers fall back to the JSONL approximation — no data beats wrong data)
    try:
        return json.loads(path.read_text())
    except Exception:
        return None
