"""WHICH bot does a spec take? One derivation, and every caller uses it.

WHY THIS IS ITS OWN MODULE
--------------------------
Three different questions need the same answer:

* :func:`._cct_token_pool.ensure_cct_bot_token` — *which token do I WRITE into
  this agent's ``.env``?* (the writer, and the only one with a side effect)
* :mod:`._cct_token_collision` — *do two specs take the SAME bot?* (the
  fleet-wide static census)
* the ownership ledger (:mod:`.._state.state_db_token_owner`) — *who holds
  this bot right now?*

They used to be able to disagree, because only the first one existed and the
other two would have had to re-derive it. A second agent-to-slot derivation is
exactly how a census reports collisions that do not exist and misses ones that
do, so this module holds the derivation and the writer calls it. The writer is
now this function plus a file write, and nothing else.

FOUR OUTCOMES, NOT TWO
----------------------
"this agent has no bot" is three different facts and only one is a defect:

* :data:`TOKEN_DISABLED` — the spec sets ``CCT_BOT_TOKEN`` to an EXPLICITLY
  EMPTY value. **Designed, not broken**: seven of the eight handymen carry it
  so that only ``handyman-06`` polls the shared handyman bot. Their spec says
  so in a comment — "Do NOT re-add CCT_BOT_TOKEN / CCT_AGENT_ID here: an
  explicit empty OVERRIDES the pool-injected value" — and that arrangement IS
  the one-token-one-poller invariant, upheld by hand. A census that flagged
  that family would be flagging the thing it exists to check.
* :data:`TOKEN_NO_CHANNEL` — the spec never asks for the rail.
* :data:`TOKEN_UNRESOLVED` — it asks and nothing resolves. Not a collision (an
  agent with no token cannot take anybody's), but not "fine" either: it is the
  mute-and-deaf condition ``sac agents cct-audit`` and :mod:`._cct_rail_alarm`
  report.

NO TOKEN VALUE LEAVES THIS MODULE
---------------------------------
:func:`resolve_cct_token` holds a value only for as long as it takes to hash
it. :class:`CctTokenResolution` carries ``token_fp`` — the opaque
``sha256:<12hex>`` that :func:`.._account._rotation_audit.fingerprint_token`
already produces for the account audit — and no field, log line or projection
here carries anything else pool-derived except slot NAMES and pool source
PATHS, the same strings :mod:`._cct_token_pool` already logs.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .._account._rotation_audit import fingerprint_token
from ._cct_token_pool import (
    _POOL_PREFIX,
    _TOKEN_VAR,
    _channel_requested,
    _declared_slot,
    _slot_candidates,
)
from ._sdk_channels import _TELEGRAMMER_CHANNEL
from ._secret_pool import PoolRead, _pool_source_label, _read_env_file, read_pool

#: A real bot token resolves for this agent. :attr:`CctTokenResolution.source`
#: says which precedence step produced it; ``token_fp`` fingerprints it.
TOKEN_RESOLVED = "resolved"
#: ``spec.apptainer.env`` sets ``CCT_BOT_TOKEN`` to an EXPLICITLY EMPTY value.
#: Deliberately not a Telegram consumer — see the module docstring.
TOKEN_DISABLED = "disabled"
#: The spec never requests the telegrammer channel. Nothing to resolve.
TOKEN_NO_CHANNEL = "no-channel"
#: The channel IS requested and NO token resolves.
TOKEN_UNRESOLVED = "unresolved"

#: ``spec.apptainer.env: CCT_BOT_TOKEN`` — typed into the spec. It rides an
#: apptainer ``--env`` flag, which is emitted AFTER ``--env-file`` and
#: overrides it (``_apptainer_build_argv``), so it beats every other route at
#: runtime — including an empty value, which is what makes DISABLED work.
SOURCE_SPEC_ENV = "spec-env"
#: The agent's materialised ``$HOME/.env`` — the ``.envrc`` cascade fold.
SOURCE_ENV_FILE = "env-file"
#: A ``CCT_BOT_TOKEN_<SLOT>`` from the fleet secrets pool.
SOURCE_POOL = "pool"


@dataclass(frozen=True)
class CctTokenResolution:
    """WHICH bot a spec takes — as a FINGERPRINT, never as a value."""

    agent: str
    #: One of :data:`TOKEN_RESOLVED` / :data:`TOKEN_DISABLED` /
    #: :data:`TOKEN_NO_CHANNEL` / :data:`TOKEN_UNRESOLVED`.
    outcome: str
    #: For a RESOLVED outcome: which precedence step won.
    source: str = ""
    #: The pool slot that resolved — for ``source == "pool"`` only.
    slot: str = ""
    #: ``spec.apptainer.env: CCT_BOT_TOKEN_SLOT``, upper-snaked. Non-empty
    #: means somebody typed this mapping on purpose.
    declared_slot: str = ""
    #: Slot names tried, in order. Names only.
    candidates: tuple[str, ...] = ()
    #: ``sha256:<12hex>`` of the resolved token; ``""`` when none resolved.
    token_fp: str = ""
    #: Whether a MISS against the pool read was conclusive (:class:`PoolRead`).
    pool_trusted: bool = True
    #: Why the outcome is what it is. Operator-facing.
    detail: str = ""

    @property
    def claims_a_token(self) -> bool:
        """True iff this agent would hold a real bot token at runtime.

        The predicate the collision census counts on: only a claimed token can
        be claimed TWICE. DISABLED, NO_CHANNEL and UNRESOLVED all hold nothing
        and so cannot collide with anything, including each other.
        """
        return self.outcome == TOKEN_RESOLVED and bool(self.token_fp)

    def to_dict(self) -> dict:
        """JSON-friendly projection. Holds no token value, by construction."""
        return {
            "agent": self.agent,
            "outcome": self.outcome,
            "source": self.source,
            "slot": self.slot,
            "declared_slot": self.declared_slot,
            "candidates": list(self.candidates),
            "token_fp": self.token_fp,
            "pool_trusted": self.pool_trusted,
            "detail": self.detail,
        }


def resolve_cct_token(
    config,
    *,
    dest: Path | None = None,
    pool: PoolRead | None = None,
) -> CctTokenResolution:
    """WHICH bot token would ``config`` take? Pure — it reads, and writes nothing.

    ``dest`` is the agent's materialised home. ``None`` means "do not consult
    a ``.env``" — the caller has none, or is auditing a spec whose home lives
    on another host. ``pool`` is the read-once injection seam: a fleet sweep
    reads the pool ONCE and passes it in, rather than forking a bash per agent
    to source ~28 secret files.

    Order, first hit wins:

    0. an EXPLICITLY EMPTY ``spec.apptainer.env: CCT_BOT_TOKEN`` →
       :data:`TOKEN_DISABLED`. First because it is the loudest statement
       available: the apptainer ``--env`` flag overrides ``--env-file``, so an
       empty spec value beats any token sac folds into ``.env`` and the agent
       holds nothing at runtime whatever the pool says.
    1. no telegrammer channel → :data:`TOKEN_NO_CHANNEL`.
    2. a non-empty ``spec.apptainer.env: CCT_BOT_TOKEN`` →
       :data:`SOURCE_SPEC_ENV`, for the same override reason.
    3. ``dest/.env`` already carries one → :data:`SOURCE_ENV_FILE`
       (precedence #1, the ``.envrc`` cascade fold).
    4. the DECLARED slot, else the mechanical candidates, against the pool →
       :data:`SOURCE_POOL` (precedence #2/#3).
    5. otherwise :data:`TOKEN_UNRESOLVED`.

    Steps 1, 3, 4 and 5 are :func:`._cct_token_pool.ensure_cct_bot_token`'s own
    order, unchanged — that function now calls this one, so the two cannot
    drift. PRESENCE IS TESTED WITH BARE TRUTHINESS, not ``.strip()``, for the
    same reason: it is what the writer did. The two can only differ on a
    whitespace-only token, which is broken either way.

    MEASURED BLAST RADIUS of routing the writer through here (122 live specs,
    2026-08-22): 24 request the channel, 7 carry an explicit empty
    ``CCT_BOT_TOKEN``, and ZERO carry both — so step 0, the only branch the
    writer did not already have, changes nothing for any agent on the fleet
    today. ZERO specs pin a non-empty ``CCT_BOT_TOKEN`` either; step 2 exists
    so the census cannot MISS a real claim on a token, not to alter a start.
    """
    name = getattr(config, "name", "") or ""
    spec_env = getattr(config, "env", None) or {}
    declared = _declared_slot(config)
    pinned = str(spec_env.get(_TOKEN_VAR, "") or "")

    if _TOKEN_VAR in spec_env and not pinned.strip():
        return CctTokenResolution(
            agent=name,
            outcome=TOKEN_DISABLED,
            source=SOURCE_SPEC_ENV,
            declared_slot=declared,
            detail=(
                f"spec.apptainer.env sets {_TOKEN_VAR} to an EXPLICITLY EMPTY "
                "value, which rides an apptainer --env flag and overrides "
                "anything in --env-file. This agent holds no bot token at "
                "runtime BY DECLARATION and cannot collide with any other."
            ),
        )

    if not _channel_requested(config):
        return CctTokenResolution(
            agent=name,
            outcome=TOKEN_NO_CHANNEL,
            declared_slot=declared,
            detail=(
                f"spec.claude.channels does not request {_TELEGRAMMER_CHANNEL!r}; "
                "this agent is bot-less by declaration"
            ),
        )

    if pinned:
        return CctTokenResolution(
            agent=name,
            outcome=TOKEN_RESOLVED,
            source=SOURCE_SPEC_ENV,
            declared_slot=declared,
            token_fp=fingerprint_token(pinned) or "",
            detail=(
                f"{_TOKEN_VAR} is pinned directly in spec.apptainer.env, which "
                "overrides --env-file at runtime. Value not logged."
            ),
        )

    env_file = (Path(dest) / ".env") if dest is not None else None
    existing = (
        _read_env_file(env_file) if env_file is not None and env_file.is_file() else {}
    )
    folded = existing.get(_TOKEN_VAR, "")
    if folded:
        return CctTokenResolution(
            agent=name,
            outcome=TOKEN_RESOLVED,
            source=SOURCE_ENV_FILE,
            declared_slot=declared,
            token_fp=fingerprint_token(folded) or "",
            detail=(
                f"{_TOKEN_VAR} is already present in {env_file} (precedence #1 "
                "— the .envrc cascade fold). Value not logged."
            ),
        )

    workdir = getattr(config, "workdir", "") or ""
    candidates = tuple([declared] if declared else _slot_candidates(name, workdir))
    read = pool if pool is not None else read_pool()
    for slot in candidates:
        value = read.env.get(f"{_POOL_PREFIX}{slot}", "")
        if value:
            how = "declared in the spec" if declared else "derived from the agent name"
            return CctTokenResolution(
                agent=name,
                outcome=TOKEN_RESOLVED,
                source=SOURCE_POOL,
                slot=slot,
                declared_slot=declared,
                candidates=candidates,
                token_fp=fingerprint_token(value) or "",
                pool_trusted=read.trusted,
                detail=(
                    f"resolved from pool slot {_POOL_PREFIX}{slot} ({how}). "
                    "Value not logged."
                ),
            )

    tried = ", ".join(f"{_POOL_PREFIX}{c}" for c in candidates) or "(no candidate)"
    return CctTokenResolution(
        agent=name,
        outcome=TOKEN_UNRESOLVED,
        declared_slot=declared,
        candidates=candidates,
        pool_trusted=read.trusted,
        detail=(
            f"spec.claude.channels requests {_TELEGRAMMER_CHANNEL!r} but no bot "
            f"token resolves: tried {tried} against the pool "
            f"({_pool_source_label()}), and no {_TOKEN_VAR} was folded into the "
            "agent's .env. This agent holds no token, so it cannot collide — "
            "but it is MUTE and DEAF on Telegram; see `sac agents cct-audit`."
        ),
    )


__all__ = [
    "SOURCE_ENV_FILE",
    "SOURCE_POOL",
    "SOURCE_SPEC_ENV",
    "TOKEN_DISABLED",
    "TOKEN_NO_CHANNEL",
    "TOKEN_RESOLVED",
    "TOKEN_UNRESOLVED",
    "CctTokenResolution",
    "resolve_cct_token",
]
