"""Is this agent's Telegram rail UP, DOWN, or unobserved? Three answers, never two.

WHAT THIS EXISTS TO CLOSE
-------------------------
:func:`._cct_token_pool.prune_tokenless_telegrammer_mcp` REMOVES the
``claude-code-telegrammer`` MCP entry when no bot token resolved. That deletion
is deliberate and correct (operator ruling, card
``sac-omit-telegram-mcp-when-no-cct-bot-token-20260702``): a server that starts
on an empty token and fails on every boot is worse than an absent one.

But it removes the rail in BOTH directions, and it does so at the one moment
nothing can report it. The agent starts perfectly. Health reports green. The
operator simply stops hearing from it — and the agent cannot say so, because
the tool it would say it with is the tool that was just deleted. That is how
the 2026-08-12 outage was found: by the operator noticing silence.

This module computes the fact. :mod:`._cct_rail_alarm` is what makes it loud,
and it deliberately does NOT ride Telegram — see that module.

THE CLASS, NOT THE INSTANCE
---------------------------
Slot names are chosen by whoever writes the pool. Candidates are derived
mechanically from the agent name by :func:`._cct_token_pool._slot_candidates`.
Nothing checks the two agree, and measured on the live fleet they routinely do
not::

    scitex-agent-container   derives SCITEX_AGENT_CONTAINER / AGENT_CONTAINER
                             pool has SAC
    scitex-cards             derives SCITEX_CARDS / CARDS
                             pool has TODO                     (legacy name)
    neurovista               derives NEUROVISTA
                             pool has PAPER_NEUROVISTA
    neurovista-paper-writer  derives NEUROVISTA_PAPER_WRITER
                             pool has PAPER_NEUROVISTA_WRITER  (WORD ORDER)

The last one is why "derive harder" is not the answer and why this module does
not attempt one: no stripping rule bridges a word-order difference, and a rule
that guessed well enough to bridge it would also guess wrong somewhere else and
hand an agent a bot that is not its own — the exact theft
``_slot_candidates`` was rewritten in 2026-07 to make impossible. Which side
should move is a live operator decision (card
``sac-cct-token-slot-mismatch-and-env-fold-20260812``). This module's whole job
is to make the mismatch LOUD, never to resolve it.

:func:`near_miss_slots` is the one concession, and it resolves nothing: it
names pool slots that SHARE A WORD with the agent, as a "did you mean", so a
human fixing the spec does not have to go reading the secrets file. sac will
not use them.

TOKEN VALUES NEVER APPEAR HERE
------------------------------
Presence is checked with ``bool(str(v).strip())`` and nothing else. No verdict
field, log line, message or audit row carries a value. Slot NAMES and pool
source PATHS are the only pool-derived strings that leave this module — the
same contract :mod:`._cct_token_pool` already keeps.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from ._cct_token_pool import (
    _POOL_PREFIX,
    _SLOT_OVERRIDE_VAR,
    _TOKEN_VAR,
    _channel_requested,
    _declared_slot,
    _slot_candidates,
    _upper_snake,
)
from ._sdk_channels import _TELEGRAMMER_CHANNEL
from ._secret_pool import (
    _SECRETS_ENVRC_VAR,
    PoolRead,
    _pool_source_label,
    _read_env_file,
    read_pool,
)

#: The rail is up: a token is present. (Which SOURCE provided it is in
#: :attr:`CctRailVerdict.source`; the value itself is never recorded.)
RAIL_UP = "up"
#: The rail is down, CONCLUSIVELY: the channel is requested, no token is
#: present, and sac read the pool it meant to read.
RAIL_DOWN = "down"
#: sac could not tell. NOT a soft "down", and never rendered as fine — the
#: pool read was untrusted, or the agent's ``.env`` could not be read.
RAIL_UNKNOWN = "unknown"
#: The spec does not ask for the rail, so there is nothing to be wrong.
RAIL_NOT_REQUESTED = "not-requested"

#: Words too generic to make a slot a plausible "did you mean".
_STOPWORDS = frozenset({"SCITEX", "AGENT", "PAPER", "PROJ", "TEST", "BOT"})


@dataclass(frozen=True)
class CctRailVerdict:
    """One agent's Telegram-rail verdict. Carries no secret material.

    ``state`` is one of :data:`RAIL_UP` / :data:`RAIL_DOWN` /
    :data:`RAIL_UNKNOWN` / :data:`RAIL_NOT_REQUESTED`.
    """

    agent: str
    state: str
    #: Where the token came from when ``state`` is UP: ``"env-file"``
    #: (precedence #1, the ``.envrc`` fold) or ``"pool"`` (#2/#3).
    source: str = ""
    #: The pool slot that resolved. Empty for the ``.env`` path — that route
    #: never names a slot — and empty when nothing resolved.
    resolved_slot: str = ""
    #: The slot DECLARED in ``spec.apptainer.env: CCT_BOT_TOKEN_SLOT``.
    #: Non-empty means somebody typed this mapping on purpose.
    declared_slot: str = ""
    #: Slot names sac actually tried, in order. Names only.
    candidates: tuple[str, ...] = ()
    #: Pool slots sharing a word with the agent name — a "did you mean" for a
    #: human. sac does NOT use these. See :func:`near_miss_slots`.
    near_misses: tuple[str, ...] = ()
    #: Human-readable pool location (paths only).
    pool_source: str = ""
    #: Whether a MISS against the pool read was conclusive.
    pool_trusted: bool = True
    #: Why the state is what it is. Operator-facing.
    detail: str = ""
    extra: dict = field(default_factory=dict)

    @property
    def is_alarming(self) -> bool:
        """True for the two states a human must be told about."""
        return self.state in (RAIL_DOWN, RAIL_UNKNOWN)

    def remedy(self) -> str:
        """The fix, named. An error that says what to DO is worth several
        that say what broke.

        The first option is deliberately the per-spec override: it is
        precedence #2, it is one line, it is declared in the spec rather than
        inferred from surroundings, and — unlike a pool rename or a project
        ``.envrc`` — it travels with the agent when it relocates, which is
        precisely what the 2026-08-12 outage proved the other routes do not.
        """
        want = self.declared_slot or (self.candidates[0] if self.candidates else "")
        hint = ""
        if self.near_misses:
            hint = (
                "\n  Pool slots sharing a word with this agent's name (a "
                "'did you mean', NOT something sac will use on its own): "
                + ", ".join(f"{_POOL_PREFIX}{s}" for s in self.near_misses)
                + ". If one of these is this agent's bot, name it with "
                "option (1)."
            )
        return (
            "Fix by ONE of:\n"
            f"  (1) PREFERRED — declare the slot in the spec, one line under "
            f"spec.apptainer.env:\n"
            f"        {_SLOT_OVERRIDE_VAR}: <SLOT>\n"
            f"      (precedence #2; the slot name WITHOUT the "
            f"{_POOL_PREFIX} prefix). This is the documented override for "
            f"names that do not map mechanically, and it is the only route "
            f"that survives a relocation.\n"
            f"  (2) add {_POOL_PREFIX}{want or '<SLOT>'}=<token> to a secrets "
            f"file in the pool, then restart `sac listen` if it is what "
            f"provides the env.\n"
            f"  (3) if this agent needs no Telegram rail at all, drop "
            f"{_TELEGRAMMER_CHANNEL!r} from spec.claude.channels — then it is "
            f"bot-less BY DECLARATION and stops being reported here."
            f"{hint}"
        )


def near_miss_slots(agent_name: str, pool_env: dict) -> tuple[str, ...]:
    """Pool slot names sharing a significant word with ``agent_name``.

    A REPORTING aid only — nothing resolves through this. It exists because
    the two hardest live mismatches are a word-order swap
    (``NEUROVISTA_PAPER_WRITER`` vs ``PAPER_NEUROVISTA_WRITER``) and a prefix
    (``NEUROVISTA`` vs ``PAPER_NEUROVISTA``), which a human recognises
    instantly and a derivation rule cannot bridge without also guessing wrong
    elsewhere. Handing the human the shortlist is safe; handing it to the
    resolver is how an agent takes another agent's bot.

    Generic words are dropped (:data:`_STOPWORDS`) so ``SCITEX``-anything does
    not "match" every scitex slot in the pool. Returns slot names with the
    ``CCT_BOT_TOKEN_`` prefix already stripped, sorted, at most five.
    """
    words = {w for w in _upper_snake(agent_name).split("_") if w} - _STOPWORDS
    if not words:
        return ()
    hits: set[str] = set()
    for key in pool_env:
        if not key.startswith(_POOL_PREFIX):
            continue
        slot = key[len(_POOL_PREFIX) :]
        if not slot:
            continue
        if words & ({w for w in slot.split("_") if w} - _STOPWORDS):
            hits.add(slot)
    return tuple(sorted(hits)[:5])


def _env_token_state(dest: Path | None) -> tuple[str, str]:
    """Precedence #1: does the materialised ``.env`` already carry a token?

    Returns ``(state, detail)`` where ``state`` is ``"present"``, ``"absent"``
    or ``"unreadable"``. A MISSING file is ``absent`` — that is the normal
    state before any fold has run. A file that EXISTS and cannot be read is
    ``unreadable``, which must reach the caller as UNKNOWN rather than as a
    missing token: this is the exact route that carried the token on
    ywata-note-win and vanished on relocation, so failing to read it is not
    the same fact as it not being there.
    """
    if dest is None:
        return "unreadable", (
            "sac could not resolve this agent's materialised home, so the "
            f"{_TOKEN_VAR} that a project .envrc may have folded there "
            "(precedence #1) was never checked"
        )
    env_file = Path(dest) / ".env"
    if not env_file.is_file():
        return "absent", ""
    # stx-allow: fallback (reason: an unreadable .env is the UNKNOWN verdict this function reports, not a crash — the caller must be able to say "could not tell" instead of "no token". UnicodeDecodeError is included because a .env sac cannot DECODE is every bit as unread as one it cannot OPEN, and it is a ValueError rather than an OSError.)
    try:
        env = _read_env_file(env_file)
    except (
        OSError,
        UnicodeDecodeError,
    ) as exc:  # stx-allow: fallback (reason: see inline comment)
        return "unreadable", f"{env_file} exists but could not be read ({exc})"
    return ("present" if str(env.get(_TOKEN_VAR, "") or "").strip() else "absent"), ""


def materialised_home(config) -> Path | None:
    """The host-side directory ``deploy_to_home`` writes this agent's ``.env``
    into, or ``None`` when it cannot be resolved.

    ``None`` is a real answer, not a failure to have one: it drives the
    UNKNOWN branch of :func:`assess_cct_rail` rather than being silently
    treated as "no token".
    """
    # stx-allow: fallback (reason: a stub/partial config in a non-start context has no resolvable state dir; that is reported as UNKNOWN by the caller, never as an absent token)
    try:
        from .tui_session import state_dir_for_config

        return state_dir_for_config(config) / "home"
    except Exception:  # stx-allow: fallback (reason: see inline comment)
        return None


def assess_cct_rail(
    config,
    *,
    dest: Path | None = None,
    pool: PoolRead | None = None,
) -> CctRailVerdict:
    """Decide whether ``config``'s Telegram rail is UP, DOWN or UNOBSERVED.

    ``dest`` is the agent's materialised home (where ``.env`` lives); omit it
    and it is resolved via :func:`materialised_home`. ``pool`` is the injection
    seam for a pre-read pool — the fleet audit reads it ONCE and reuses it
    across ~90 specs rather than forking a bash per agent.

    Ordering mirrors :func:`._cct_token_pool.ensure_cct_bot_token` exactly, so
    this reports the resolution that actually happens rather than a second
    opinion about it: ``.env`` first (precedence #1), then the declared slot
    (#2) or the mechanical candidates (#3).
    """
    name = getattr(config, "name", "") or ""
    if not _channel_requested(config):
        return CctRailVerdict(
            agent=name,
            state=RAIL_NOT_REQUESTED,
            detail=(
                f"spec.claude.channels does not request {_TELEGRAMMER_CHANNEL!r}; "
                "this agent is bot-less by declaration"
            ),
        )

    home = dest if dest is not None else materialised_home(config)
    env_state, env_detail = _env_token_state(home)
    if env_state == "present":
        return CctRailVerdict(
            agent=name,
            state=RAIL_UP,
            source="env-file",
            declared_slot=_declared_slot(config),
            detail=(
                f"{_TOKEN_VAR} is already present in {Path(home) / '.env'} "
                "(precedence #1 — the .envrc cascade fold); sac did not need "
                "the pool. Value not read."
            ),
        )

    declared = _declared_slot(config)
    workdir = getattr(config, "workdir", "") or ""
    candidates = tuple([declared] if declared else _slot_candidates(name, workdir))
    read = pool if pool is not None else read_pool()

    for slot in candidates:
        if str(read.env.get(f"{_POOL_PREFIX}{slot}", "") or "").strip():
            return CctRailVerdict(
                agent=name,
                state=RAIL_UP,
                source="pool",
                resolved_slot=slot,
                declared_slot=declared,
                candidates=candidates,
                pool_source=_pool_source_label(),
                pool_trusted=read.trusted,
                detail=(
                    f"resolved from pool slot {_POOL_PREFIX}{slot} "
                    f"({'declared in the spec' if declared else 'derived from the agent name'}). "
                    "Value not read."
                ),
            )

    near = near_miss_slots(name, read.env)
    tried = ", ".join(f"{_POOL_PREFIX}{c}" for c in candidates) or "(no candidate)"

    if env_state == "unreadable":
        return CctRailVerdict(
            agent=name,
            state=RAIL_UNKNOWN,
            declared_slot=declared,
            candidates=candidates,
            near_misses=near,
            pool_source=_pool_source_label(),
            pool_trusted=read.trusted,
            detail=(
                f"no pool slot resolved (tried {tried}) AND the precedence-#1 "
                f"route could not be checked: {env_detail}. sac cannot say "
                "whether this agent has a bot token — this is NOT a report "
                "that it lacks one."
            ),
        )

    if not any(k.startswith(_POOL_PREFIX) for k in read.env):
        # The read SUCCEEDED and contains no bot slots AT ALL. That is not a
        # pool in which this agent's slot is missing; it is not the bot pool.
        # Distinguishing the two is free here and the difference is total: on
        # a host whose CCT secrets file never made it into the launching
        # environment, EVERY agent would otherwise be reported DOWN, and 80-odd
        # confident false alarms are indistinguishable from a broken fleet.
        return CctRailVerdict(
            agent=name,
            state=RAIL_UNKNOWN,
            declared_slot=declared,
            candidates=candidates,
            pool_source=_pool_source_label(),
            pool_trusted=read.trusted,
            detail=(
                f"no slot resolved (tried {tried}), and the pool sac read "
                f"({_pool_source_label()}) contains NO {_POOL_PREFIX}* slots at "
                "all — so it is not the bot pool. sac cannot distinguish 'no "
                "bot for this agent' from 'not looking at the bot pool'. Check "
                f"that the CCT secrets file is in {_SECRETS_ENVRC_VAR} for "
                "whatever process starts agents here."
            ),
        )

    if not read.trusted:
        return CctRailVerdict(
            agent=name,
            state=RAIL_UNKNOWN,
            declared_slot=declared,
            candidates=candidates,
            near_misses=near,
            pool_source=_pool_source_label(),
            pool_trusted=False,
            detail=(
                f"no slot resolved (tried {tried}), but the pool read is NOT "
                f"conclusive: {read.detail}. sac cannot distinguish 'this "
                "agent has no bot' from 'sac could not see the pool'. Treat "
                "this as an unread instrument, not an all-clear."
            ),
        )

    return CctRailVerdict(
        agent=name,
        state=RAIL_DOWN,
        declared_slot=declared,
        candidates=candidates,
        near_misses=near,
        pool_source=_pool_source_label(),
        pool_trusted=True,
        detail=(
            f"spec.claude.channels requests {_TELEGRAMMER_CHANNEL!r} but NO bot "
            f"token resolves: tried {tried} against the pool "
            f"({_pool_source_label()}), and no {_TOKEN_VAR} was folded into the "
            "agent's .env. The telegrammer MCP entry is REMOVED from the "
            "materialised .mcp.json, so this agent is MUTE (cannot send) and "
            "DEAF (never receives) on Telegram, and it cannot self-diagnose — "
            "`health` is a tool on the server that just went away."
        ),
    )


__all__ = [
    "RAIL_DOWN",
    "RAIL_NOT_REQUESTED",
    "RAIL_UNKNOWN",
    "RAIL_UP",
    "CctRailVerdict",
    "assess_cct_rail",
    "materialised_home",
    "near_miss_slots",
]
