"""Live credential-BIND resolution for the agent list's Account column.

The column answered "which account is this agent on?" from the agent's
``<runtime>/home/.claude.json`` — Claude Code's own persisted login record.
That record is NOT rewritten when the credential bind changes, and when it
carries no ``oauthAccount`` key the caller falls back to the SPEC label, which
for a pool agent (``credentials_files`` with no singular ``account`` pin)
collapses to the host's shared OAuth identity so every such agent renders
identically.

Measured 2026-08-02, the day it cost an hour: the fleet had ALREADY been moved
onto one account and the column still showed three. The login records behind
those cells were 11 to 37 days old (mtimes 06-26, 06-28, 07-08, 07-09, 07-19,
07-22), and the one row that looked correct matched by coincidence.

This module reads the BIND instead: the kernel's own record, in each live
container's ``mountinfo``, of which host credentials file is mounted at the
container's ``~/.claude/.credentials.json``. For a RUNNING agent that is
ground truth rather than a derived value — it is what the process will
actually authenticate with.

Scope, stated because a silent gap here is how the previous version misled:
this resolves LOCAL containers only. An agent on another host has no
``/proc`` entry here and resolves to ``None``, so the caller falls back to the
older, weaker signals. It never invents an answer.
"""

from __future__ import annotations

import re
from functools import lru_cache
from pathlib import Path

# The bind SOURCE is ``.../accounts/[<provider>/]<account>/.credentials.json``.
# The provider segment (``anthropic``) is optional so an older layout without
# it still resolves.
_BIND_SOURCE_RE = re.compile(
    r"accounts/(?:[A-Za-z0-9_-]+/)?([A-Za-z0-9._-]+)/\.credentials\.json"
)
# The bind TARGET inside the container. Matching on it keeps an unrelated
# accounts-shaped path elsewhere in the mount table from being read as the
# credential bind.
_BIND_TARGET = "/.claude/.credentials.json"

__all__ = [
    "account_from_mountinfo",
    "bound_account_for",
    "bound_accounts_by_agent",
]


def account_from_mountinfo(mountinfo: str) -> str:
    """The account slug bound at the container's credentials path, or ``""``.

    Pure: takes the text of a ``mountinfo`` file and returns what it says.
    Split from the ``/proc`` walk so it can be tested against REAL captured
    mount tables instead of a mocked filesystem — the parsing is where a
    wrong answer would come from, and it is the part worth pinning.

    A line is only read when it binds ONTO the container credentials path, so
    an unrelated accounts-shaped mount elsewhere in the table is not mistaken
    for the credential bind.
    """
    for line in mountinfo.splitlines():
        if _BIND_TARGET not in line:
            continue
        match = _BIND_SOURCE_RE.search(line)
        if match:
            return match.group(1)
    return ""


@lru_cache(maxsize=1)
def bound_accounts_by_agent() -> dict[str, str]:
    """Map ``SAC_NAME`` -> bound account slug for every LIVE local container.

    One ``/proc`` walk, cached, because the list renders many rows and a scan
    per row would be quadratic. The cache is per-process and MUST be cleared
    at the start of each list invocation — a long-lived caller (``--watch``)
    would otherwise report a bind from minutes ago, which is the exact class
    of staleness this module exists to remove.
    """
    found: dict[str, str] = {}
    proc = Path("/proc")
    if not proc.is_dir():  # non-Linux / no procfs — resolve nothing, claim nothing
        return found
    for entry in proc.iterdir():
        if not entry.name.isdigit():
            continue
        name = _sac_name_of(entry)
        if not name or name in found:
            continue
        account = _bound_account_of(entry)
        if account:
            found[name] = account
    return found


def _sac_name_of(proc_entry: Path) -> str:
    """The agent name a process belongs to, from ``SAC_NAME`` in its environ."""
    # stx-allow: fallback (reason: a process may exit mid-walk, or belong to
    # another user; either way it is simply not one of our agents.)
    try:
        raw = (proc_entry / "environ").read_bytes().decode("utf-8", "replace")
    except OSError:
        return ""
    for pair in raw.split("\0"):
        if pair.startswith("SAC_NAME="):
            return pair[len("SAC_NAME=") :]
    return ""


def _bound_account_of(proc_entry: Path) -> str:
    """The account slug bound at the container's credentials path, or ``""``."""
    # stx-allow: fallback (reason: see _sac_name_of — a vanished or foreign
    # process is not an error, it is not our agent.)
    try:
        mounts = (proc_entry / "mountinfo").read_text()
    except OSError:
        return ""
    return account_from_mountinfo(mounts)


def bound_account_for(name: str) -> str | None:
    """The account ``name`` is CURRENTLY bound to, or ``None`` if unresolvable.

    ``None`` means "this host cannot see that agent's mounts" — a remote agent,
    a stopped agent, or no procfs. It never means "no account": the caller
    falls back to the spec/runtime label and the cell degrades to the older
    signal rather than to a wrong one.
    """
    # stx-allow: fallback (reason: the Account column must never crash the
    # list; an unresolvable bind degrades to the caller's weaker signals.)
    try:
        return bound_accounts_by_agent().get(name) or None
    except Exception:  # stx-allow: fallback (reason: see inline comment)
        return None
