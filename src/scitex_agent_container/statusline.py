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


def _display(raw: bytes) -> None:
    # stx-allow: fallback (reason: statusLine display must never raise; corrupt
    # or unexpected payload shape silently outputs nothing rather than aborting)
    try:
        data = json.loads(raw)
        ctx_pct = (data.get("context_window") or {}).get("used_percentage", 0)
        model = (data.get("model") or {}).get("display_name", "")
        parts: list[str] = []

        host = _hostname()
        agent = _agent_name()
        if agent and agent != "unknown":
            parts.append(f"{agent}@{host}" if host else agent)
        elif host:
            parts.append(host)

        wd = _workdir(data)
        if wd:
            parts.append(wd)

        if model:
            parts.append(model)
        parts.append(f"ctx:{ctx_pct:.0f}%")

        rl = data.get("rate_limits") or {}
        fh = rl.get("five_hour") or {}
        fh_pct = fh.get("used_percentage")
        if fh_pct is not None:
            parts.append(f"5h:{fh_pct:.0f}%")

        acct = _active_account()
        if acct:
            parts.append(f"acct:{acct}")

        print(" | ".join(parts), flush=True)
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
