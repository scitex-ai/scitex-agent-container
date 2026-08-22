"""OBSERVATION half of the poller-singleton detector: who is polling, on what?

This module answers only "which live processes on this host are Telegram
pollers, and what is the opaque fingerprint of the bot token each one holds?".
The VERDICT — whether that population violates one-poller-per-token — lives in
:mod:`._cct_poller_singleton`, which imports this. Splitting them keeps the
thing that touches ``/proc`` separate from the thing that decides, so the
decision can be tested without a process and the reader without a rule.

Read-only, stdlib-only. Nothing here signals, kills or reaps anything.

TOKEN VALUES NEVER LEAVE THIS MODULE
    A value is read from a process environment, handed straight to
    :func:`.._account._rotation_audit.fingerprint_token`, and dropped. Only the
    opaque ``sha256:<12hex>`` prefix is stored or returned — the same contract
    ``_host_push_config`` states as "Only sha256 digests (12 chars) are ever
    printed", and the reason this reuses that helper rather than growing a
    second one.

    :class:`LivePoller` deliberately carries NO cmdline either. sac never puts
    a token on an argv (see :mod:`._cct_token_pool`), but a poller someone
    started by hand might, and a detector that leaks the secret it is
    protecting is worse than no detector.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Sequence

from .._account._rotation_audit import fingerprint_token
from ._cct_token_pool import _AGENT_ID_VAR, _TOKEN_VAR

#: Substrings identifying the poller SCRIPT in an argv token. The poller is a
#: bun/TypeScript process, not a Python module — matching the script name
#: rather than the interpreter keeps this true if the runner changes. Same
#: string :mod:`..cli_pkg._helpers._agent_list_inbound_rail` matches for its
#: own, different question (is THIS agent's claude the parent of one?).
POLLER_SCRIPT_MARKERS: tuple[str, ...] = (
    "telegram-server.ts",
    "telegram-server.js",
)

#: argv[0] basenames that MENTION the poller script without BEING one.
#:
#: THE NAMED TRAP: a plain ``pgrep -f telegram-server`` matches the searching
#: shell itself, and a detector that counts its own search as a poller invents
#: the very duplicate it exists to find. Excluding the current pid is not
#: enough — ``pgrep``/``rg`` run as CHILDREN carry the pattern in their argv
#: too. So the match is anchored on argv[0]: a poller is a process that RUNS
#: the script, not one that talks about it.
_SEARCH_TOOLS = frozenset(
    {
        "ack",
        "ag",
        "awk",
        "egrep",
        "fgrep",
        "find",
        "grep",
        "less",
        "pgrep",
        "pkill",
        "ps",
        "rg",
        "ripgrep",
        "sed",
        "tail",
        "xargs",
    }
)

#: Env keys naming the agent that owns a poller, MOST AUTHORITATIVE FIRST.
#:
#: ``SAC_NAME`` leads, and the order is not cosmetic — it was measured. On
#: compute-04, 2026-08-22 17:02Z, this process::
#:
#:     pid 574160   CCT_AGENT_ID=handyman-01   SAC_NAME=handyman-06
#:
#: A ``CCT_AGENT_ID``-first order named it handyman-01, and a VIOLATION report
#: that names the wrong agent sends the remediation to an innocent one — worse
#: than naming nobody, because the rest of the row (pid, fingerprint) is right
#: and gives the reader no reason to doubt it.
#:
#: Note what the same measurement rules OUT: reading the PARENT instead would
#: not have helped. The parent claude process carried the SAME
#: ``CCT_AGENT_ID=handyman-01`` alongside ``SAC_NAME=handyman-06``, so the
#: defect is which KEY is trusted, not which process is read. ``CCT_AGENT_ID``
#: is a TELEGRAM identity — it can be, and here is, a different thing from the
#: agent — so it is kept last, as a name of last resort. ``SAC_NAME`` and
#: ``SCITEX_AGENT_CONTAINER_NAME`` are the runtime's own agent-name vars,
#: inherited by every stdio child (see :mod:`..._lifecycle._orphan_mcp_cleanup`),
#: and they agreed with the parent on all ten live servers measured.
_OWNER_ENV_KEYS: tuple[str, ...] = (
    "SAC_NAME",
    "SCITEX_AGENT_CONTAINER_NAME",
    _AGENT_ID_VAR,
)

#: WHY a poller yielded no fingerprint. THREE different facts wearing one
#: shape, and they need three different responses:
#:
#: * ``UNRESOLVED_ENVIRON`` — sac could not READ the environment. Vantage
#:   problem; re-run as the owning uid. UNKNOWN.
#: * ``UNRESOLVED_NO_TOKEN`` — the environment is readable and the variable is
#:   ABSENT. The process was started outside sac's env, so sac cannot tell
#:   whether it polls some token by another route. UNKNOWN.
#: * ``TOKEN_DISABLED`` — the variable is PRESENT and EMPTY. Somebody set it
#:   to nothing on purpose (the handyman family's spec does exactly this, to
#:   stop several agents sharing one bot). An empty string is not a bot token,
#:   so this process cannot collide with anything: it is excluded from the
#:   invariant rather than clouding it. NOT unknown.
#:
#: Measured on compute-04, 2026-08-22: of 10 live servers, 4 carried real and
#: distinct tokens, 6 were EMPTY by design, 1 was ABSENT. Folding EMPTY into
#: UNKNOWN made a perfectly healthy host report UNKNOWN on every single run —
#: a check that is never green gets muted, which is the gate-that-cannot-fail
#: arriving from the other direction.
UNRESOLVED_ENVIRON = "environ-unreadable"
UNRESOLVED_NO_TOKEN = "no-token-in-env"
TOKEN_DISABLED = "token-empty"


@dataclass(frozen=True)
class LivePoller:
    """One live poller process. Carries a fingerprint, never a token."""

    pid: int
    #: Opaque ``sha256:<12hex>`` of the bot token this process is polling with,
    #: or ``None`` when sac could not read it. ``None`` is what drives UNKNOWN.
    token_fp: str | None = None
    #: Owning agent where determinable, else ``""``. Best-effort: an orphan
    #: from a previous incarnation still carries its old env, which is exactly
    #: what makes it nameable.
    agent: str = ""
    #: Why the token is unresolved. Operator-facing, never a value.
    detail: str = ""
    #: :data:`UNRESOLVED_ENVIRON` / :data:`UNRESOLVED_NO_TOKEN` /
    #: :data:`TOKEN_DISABLED` when there is no fingerprint, ``""`` when
    #: resolved. Machine-readable so the remedy can branch without parsing
    #: prose.
    reason: str = ""

    @property
    def resolved(self) -> bool:
        """True iff a token fingerprint was obtained for this process."""
        return bool(self.token_fp)

    @property
    def disabled(self) -> bool:
        """True iff this process was deliberately given an EMPTY bot token.

        It holds no token, so it cannot be the second consumer of anyone's.
        Excluded from the invariant — see :data:`TOKEN_DISABLED`.
        """
        return self.reason == TOKEN_DISABLED

    def to_dict(self) -> dict:
        """JSON-friendly projection (for ``--json`` surfaces)."""
        return {
            "pid": self.pid,
            "token_fp": self.token_fp,
            "agent": self.agent,
            "reason": self.reason,
            "detail": self.detail,
        }


def is_poller_argv(argv: Sequence[str]) -> bool:
    """True iff ``argv`` is a process RUNNING the telegram poller script.

    Anchored on argv[0] so a ``pgrep``/``rg`` that merely carries the pattern
    is not counted as a poller — see :data:`_SEARCH_TOOLS` for why that trap is
    worth spending a constant on. Pure and total over its input: the seam the
    tests drive without inventing a ``/proc``.

    KNOWN LIMIT, stated rather than hidden: ``sh``/``bash`` are NOT excluded,
    because ``sh -c "exec bun run …/telegram-server.ts"`` is a shape the SDK
    channel config really emits — dropping it would lose real pollers. So a
    shell running ``rg telegram-server.ts`` is counted. It then carries no
    ``CCT_BOT_TOKEN``, which surfaces as UNKNOWN with a hint pointing at the
    pid — a mild, self-explaining false alarm, and the right side to err on
    when the alternative is missing a live 409.
    """
    tokens = [str(a) for a in argv if str(a)]
    if not tokens:
        return False
    if os.path.basename(tokens[0]) in _SEARCH_TOOLS:
        return False
    return any(marker in tok for tok in tokens for marker in POLLER_SCRIPT_MARKERS)


def _read_argv(pid_dir: Path) -> list[str]:
    """``/proc/<pid>/cmdline`` as an argv list; ``[]`` when unreadable.

    A process that exits mid-scan is normal, and its absence is not evidence
    about any other process — so an empty argv simply drops it from the
    population rather than making the whole verdict unknown.
    """
    # stx-allow: fallback (reason: a pid that exits between iterdir() and the
    # read is a normal race; it is not a poller we failed to measure, it is a
    # process that is no longer live, so dropping it is the correct answer.)
    try:
        raw = (pid_dir / "cmdline").read_bytes()
    except Exception:  # stx-allow: fallback (reason: see inline comment)
        return []
    return [tok for tok in raw.decode("utf-8", "replace").split("\0") if tok]


def read_process_env(pid_dir: Path) -> dict[str, str] | None:
    """``/proc/<pid>/environ`` as a dict, or ``None`` when it cannot be read.

    ``None`` is a REAL answer and must stay distinguishable from ``{}``: the
    file is mode 0400 owned by the process user, so an unreadable environ is
    the routine cross-uid case that drives UNKNOWN. Returning ``{}`` for it
    would silently claim the process has no token.
    """
    # stx-allow: fallback (reason: environ is owner-only; PermissionError is
    # the ORDINARY cross-uid case and must reach the caller as "could not
    # read", never as "no token present".)
    try:
        raw = (pid_dir / "environ").read_bytes()
    except Exception:  # stx-allow: fallback (reason: see inline comment)
        return None
    env: dict[str, str] = {}
    for entry in raw.decode("utf-8", "replace").split("\0"):
        key, sep, value = entry.partition("=")
        if sep and key:
            env[key] = value
    return env


def _owner_from_env(env: dict[str, str]) -> str:
    """The agent that owns this poller, or ``""`` when nothing names it."""
    for key in _OWNER_ENV_KEYS:
        value = str(env.get(key, "") or "").strip()
        if value:
            return value
    return ""


def poller_from_pid(pid: int, pid_dir: Path) -> LivePoller:
    """Fingerprint one already-identified poller process.

    The token is read from the PROCESS's own environment and nowhere else. The
    agent's materialised ``.env`` is deliberately NOT consulted as a fallback:
    that file says which token the agent WOULD get on its next start, while an
    orphan is by definition running on the token of a PREVIOUS one. Answering
    the second question with the first is how a detector reports a duplicate
    where there is none, and misses the one there is.
    """
    env = read_process_env(pid_dir)
    if env is None:
        return LivePoller(
            pid=pid,
            reason=UNRESOLVED_ENVIRON,
            detail=(
                f"/proc/{pid}/environ could not be read (it is owner-only), so "
                f"this poller's {_TOKEN_VAR} was never seen. sac cannot say "
                "which bot it polls — this is NOT a report that it has none."
            ),
        )
    agent = _owner_from_env(env)
    present = _TOKEN_VAR in env
    token = str(env.get(_TOKEN_VAR, "") or "").strip()

    if present and not token:
        return LivePoller(
            pid=pid,
            agent=agent,
            reason=TOKEN_DISABLED,
            detail=(
                f"{_TOKEN_VAR} is present and DELIBERATELY EMPTY, so this "
                "process holds no bot token and cannot be a second consumer "
                "of anyone's. Excluded from the one-poller-per-token "
                "invariant. Nothing to fix — the handyman family's spec "
                "empties it on purpose to stop several agents sharing a bot."
            ),
        )
    if not token:
        return LivePoller(
            pid=pid,
            agent=agent,
            reason=UNRESOLVED_NO_TOKEN,
            detail=(
                f"the process environment is readable and {_TOKEN_VAR} is "
                "ABSENT from it, so this process was started outside sac's "
                "env and sac cannot tell whether it polls some token by "
                "another route."
            ),
        )
    return LivePoller(pid=pid, token_fp=fingerprint_token(token), agent=agent)


def _numeric_pid_dirs(proc_root: Path) -> Iterable[Path]:
    """Every ``<proc_root>/<pid>`` directory, in ascending pid order."""
    entries = [e for e in proc_root.iterdir() if e.name.isdigit()]
    return sorted(entries, key=lambda e: int(e.name))


def scan_live_pollers(
    *,
    proc_root: Path | None = None,
    self_pid: int | None = None,
) -> tuple[LivePoller, ...]:
    """Every live telegram poller visible under ``proc_root``.

    ``self_pid`` (default: this process) is excluded — belt to
    :func:`is_poller_argv`'s braces, because the caller of a detector must
    never be able to appear in its own population.

    Raises ``OSError`` when ``proc_root`` itself cannot be enumerated. That is
    the "the scan did not run" case, and
    :func:`._cct_poller_singleton.check_poller_singleton` turns it into UNKNOWN
    rather than an empty — and falsely reassuring — result.
    """
    root = Path(proc_root) if proc_root is not None else Path("/proc")
    me = self_pid if self_pid is not None else os.getpid()
    found: list[LivePoller] = []
    for pid_dir in _numeric_pid_dirs(root):
        pid = int(pid_dir.name)
        if pid == me:
            continue
        if not is_poller_argv(_read_argv(pid_dir)):
            continue
        found.append(poller_from_pid(pid, pid_dir))
    return tuple(found)


__all__ = [
    "POLLER_SCRIPT_MARKERS",
    "TOKEN_DISABLED",
    "UNRESOLVED_ENVIRON",
    "UNRESOLVED_NO_TOKEN",
    "LivePoller",
    "is_poller_argv",
    "poller_from_pid",
    "read_process_env",
    "scan_live_pollers",
]
