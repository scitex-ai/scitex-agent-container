"""Deterministic per-agent CCT bot-token injection from the fleet pool.

Closes the last open item of card ``sac-fleet-ux-misc-2026-06-24``: agents
whose spec requests the ``server:claude-code-telegrammer`` channel used to
come up WITHOUT a Telegram bot token unless a per-project ``.envrc``
happened to ``export CCT_BOT_TOKEN="$CCT_BOT_TOKEN_<SLOT>"`` — the exact
".envrc goodwill" anti-pattern the SCITEX_TODO_AGENT identity incident
(2026-07-05/06) established must instead be DETERMINISTICALLY injected by
sac at agent start.

Canonical pool convention (pre-existing, now first-class):

* The pool is the set of ``CCT_BOT_TOKEN_<SLOT>`` environment variables
  visible to the launching ``sac agents start`` process — the union of its
  own environment and the secret files listed in ``SAC_SECRETS_ENVRC``
  (colon-separated absolute paths; the same preamble mechanism
  :mod:`._envrc` already sources so daemon-started agents resolve secrets).
  On the fleet host that is the operator's
  ``~/.bash.d/secrets/010_scitex/01_claude-code-telegrammer.src``, wired
  into ``sac-listen.service`` via ``Environment=SAC_SECRETS_ENVRC=...``.
* One slot per BOT, named for the agent that owns it:
  ``CCT_BOT_TOKEN_PAPER_SCITEX_CLEW``, ``CCT_BOT_TOKEN_TODO``, …
  This used to read "one slot per PROJECT (per-project bot ⇒ no Telegram 409
  single-poller combat)". That justification was self-refuting: it assumed one
  agent per project, and the moment a project had siblings the scheme written
  to PREVENT 409 combat GUARANTEED it — every sibling resolved to the same
  slot and took the same bot. A slot belongs to an agent, not to a directory.

Resolution order for an agent (first hit wins):

1. An explicit non-empty ``CCT_BOT_TOKEN`` already folded into the agent's
   materialised ``$HOME/.env`` (the per-project ``.envrc`` cascade) — the
   hand-authored mapping stays authoritative.
2. ``spec.apptainer.env: CCT_BOT_TOKEN_SLOT: <SLOT>`` — explicit per-spec
   slot override for names that don't map mechanically (e.g. ``SAC``).
   When set, ONLY that slot is tried (fail-loud on a typo, no silent
   mechanical fallback).
3. Mechanical candidates derived from the AGENT NAME: upper-snake, plus the
   same with a leading ``scitex-`` stripped (``scitex-todo`` → ``TODO``).
   The workdir is NOT consulted — see :func:`_slot_candidates` for the
   2026-07-17 incident and the operator's ruling. A directory names a
   PROJECT, never an agent.

On a hit the token is appended to ``$HOME/.env`` (``chmod 0600``) — the
SAME carrier the ``.envrc`` fold uses, so the container receives it via
apptainer ``--env-file`` and the materialised ``.mcp.json``'s literal
``${CCT_BOT_TOKEN}`` / ``${CCT_AGENT_ID}`` refs expand at runtime. The
token deliberately NEVER rides an ``--env`` argv flag (visible in
``/proc/<pid>/cmdline``) and its VALUE is never logged — log lines carry
only the slot NAME, the pool source path(s), and the agent name.

Loud-but-honest contract, at TWO levels, because there are two different
facts to report and they do not deserve the same volume:

* RESOLUTION failed (:func:`ensure_cct_bot_token`) — a scitex-logging
  WARNING names the pool source, the tried slot names, and the fixes. The
  start itself proceeds — Telegram is a comms rail, not a boot dependency —
  so the absence is loud but never silent, and never DRESSED UP AS A STARTUP
  FAILURE. It used to log at ERROR, which made every brand-new agent (no bot
  yet, by definition) look stillborn in its boot log next to the genuinely
  fatal lines; WARNING + an explicit "the agent starts normally" sentence
  keeps the signal without the false alarm.
* A DECLARED mapping is broken (:func:`prune_tokenless_telegrammer_mcp`) —
  ERROR. Different fact, rarer, severe in consequence (the rail is REMOVED,
  not merely quiet), so it earns the loud level without reopening the
  demotion above. Keyed on the DECLARED slot, never on the channel request —
  see that function for the 80/14/66 measurement behind that choice.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from ._sdk_channels import _TELEGRAMMER_CHANNEL, _TELEGRAMMER_MCP_KEY
from ._secret_pool import (
    _SECRETS_ENVRC_VAR,
    _pool_env,
    _pool_source_label,
    _read_env_file,
    _write_env_file,
)

# The env-var the telegrammer MCP reads its bot token from (see the shared
# baseline ``.mcp.json``: ``"CCT_BOT_TOKEN": "${CCT_BOT_TOKEN}"``).
_TOKEN_VAR = "CCT_BOT_TOKEN"
# Per-agent telegram identity var, same runtime-expansion contract.
_AGENT_ID_VAR = "CCT_AGENT_ID"
# Pool naming convention: one ``CCT_BOT_TOKEN_<SLOT>`` per project.
_POOL_PREFIX = "CCT_BOT_TOKEN_"
# Optional explicit slot override in ``spec.apptainer.env``.
_SLOT_OVERRIDE_VAR = "CCT_BOT_TOKEN_SLOT"


def _logger():
    """scitex-logging logger, imported lazily (same rationale as
    ``config.__init__._config_logger``: the package auto-configures
    handlers on first import, which must not tax module import)."""
    import scitex_logging

    return scitex_logging.getLogger(__name__)


def _upper_snake(text: str) -> str:
    """``paper-scitex-clew`` → ``PAPER_SCITEX_CLEW`` (any non-alnum → _)."""
    return re.sub(r"[^A-Za-z0-9]+", "_", text.strip()).strip("_").upper()


def _slot_candidates(name: str, workdir: str) -> list[str]:
    """Ordered, deduped mechanical slot candidates for an agent.

    Derived from the AGENT NAME **only**. The upper-snake form, plus the same
    with a leading ``scitex-``/``scitex_`` stripped (the pool names the core
    scitex packages by their short slot: ``TODO``, ``DEV``, …).

    ``workdir`` is accepted and deliberately IGNORED — see below. The parameter
    stays for call-site compatibility and as a visible marker that ignoring it
    is a decision, not an oversight.

    *** A DIRECTORY NAMES A PROJECT, NEVER AN AGENT. ***

    This used to try the WORKDIR BASENAME first, on the reasoning that the bot
    is per-project. That reasoning holds only while a project has exactly one
    agent, and it does not degrade gracefully when that stops being true: the
    second agent in a repo does not collide with the first, it *becomes* the
    first. Identity computed from location means one repo = one identity = one
    agent, structurally, and no configuration can avoid it.

    Operator, 2026-07-17, naming the root after three of my wrong diagnoses:
    「問題はアイデンティティをプロジェクトルートに紐づけてしまっているから一つの
    レポジトリから一つが立ち上がっていること」 and, on the sibling `.envrc` that
    exported the same facts: 「Cctの場合はエージェント単位なので.envrcに含めては
    いけなかった」.

    That night's three symptoms were one root wearing three coats: a stolen bot,
    forged card authorship, and fork collision. This function was offender #1's
    mechanism — the `.envrc` and sac were making the identical mistake, and I
    spent the evening blaming the file while shipping the rule.

    MEASURED BLAST RADIUS of this change (12 live agents, dry-run before the
    edit): 9 unchanged — their workdir basename already equals their agent name,
    so both rules agree. Three differ, and in every case the OLD rule was the
    wrong one:
      * scitex-cards / scitex-cards-chat — workdir ~/proj/scitex-todo, so the
        old rule tried ``TODO`` FIRST. ``CCT_BOT_TOKEN_TODO`` exists in the pool
        and ``..._CARDS`` does not, so had their spec requested the channel, sac
        would have handed them the scitex-todo STEWARD's bot. Not hypothetical:
        the exact theft that actually happened via the `.envrc` route. After this
        change they resolve to ``CARDS``/``CARDS_CHAT``, which are unregistered —
        so they correctly get NO token and the existing fail-loud WARNING fires.
        Having no bot is the right answer for an agent with no bot.
      * scitex-hub — workdir ~/proj/scitex-cloud, so the old rule tried
        ``SCITEX_CLOUD``/``CLOUD`` before ``SCITEX_HUB``/``HUB``. Neither cloud
        slot exists, so it fell through to the correct one by luck. The resolved
        slot is unchanged; what changes is that it can no longer be stolen by
        registering a ``CLOUD`` bot.
    So: no agent loses a bot it should have; two agents lose the ability to take
    one that was never theirs.

    An agent whose project genuinely owns the bot and whose name does not match
    the slot uses the explicit ``spec.apptainer.env: CCT_BOT_TOKEN_SLOT``
    override — a DECLARED mapping in the spec, which is the point: identity is
    stated, never inferred from where the process happens to stand.
    """
    bases: list[str] = []
    if name:
        bases.append(name)
    candidates: list[str] = []
    for base in bases:
        snake = _upper_snake(base)
        if not snake:
            continue
        forms = [snake]
        if snake.startswith("SCITEX_") and len(snake) > len("SCITEX_"):
            forms.append(snake[len("SCITEX_") :])
        for form in forms:
            if form not in candidates:
                candidates.append(form)
    return candidates


def _channel_requested(config) -> bool:
    """True iff ``spec.claude.channels`` asks for the telegrammer rail.

    Read by :func:`ensure_cct_bot_token` (whether to resolve at all) and by
    :func:`prune_tokenless_telegrammer_mcp` (whether resolution was even
    attempted, before it blames a declared slot for coming up empty).
    """
    channels = list(getattr(getattr(config, "claude", None), "channels", None) or [])
    return any(str(c).strip() == _TELEGRAMMER_CHANNEL for c in channels)


def _declared_slot(config) -> str:
    """The DECLARED slot from ``spec.apptainer.env: CCT_BOT_TOKEN_SLOT``, else "".

    Upper-snaked exactly as the resolution path consumes it, so a log line names
    the slot sac really looked for rather than the spelling in the spec. It is
    the one signal here separating "somebody mapped this agent to a bot" from "a
    template default came along for the ride" — see
    :func:`prune_tokenless_telegrammer_mcp` for why that, and not the channel
    request, is what earns an ERROR.
    """
    spec_env = getattr(config, "env", None) or {}
    override = str(spec_env.get(_SLOT_OVERRIDE_VAR, "") or "").strip()
    return _upper_snake(override) if override else ""


def ensure_cct_bot_token(config, dest: Path) -> None:
    """Deterministically inject this agent's Telegram bot token into
    ``dest/.env`` when the spec requests the telegrammer channel.

    Called from :func:`._to_home.deploy_to_home` AFTER the ``.envrc``
    cascade fold, so an explicit hand-authored mapping always wins. No-op
    when the spec does not request ``server:claude-code-telegrammer``.
    Never raises for a missing token — it WARNs (scitex-logging) with the
    pool path and the fixes instead, and says in so many words that the
    agent starts normally: a missing bot token degrades one comms rail, it
    does not fail a boot. The token VALUE is never logged; only slot names,
    paths, and the agent name appear.
    """
    if not _channel_requested(config):
        return
    agent_name = getattr(config, "name", "") or ""
    workdir = getattr(config, "workdir", "") or ""
    env_file = dest / ".env"
    existing = _read_env_file(env_file) if env_file.is_file() else {}

    if existing.get(_TOKEN_VAR):
        # Hand-authored .envrc (or a prior deploy) already provided the
        # token — authoritative. Only backfill the identity default.
        if not existing.get(_AGENT_ID_VAR):
            existing[_AGENT_ID_VAR] = _default_agent_id(agent_name, workdir)
            _write_env_file(env_file, existing)
            _logger().info(
                "cct: %s already provided for agent %r (value not logged); "
                "backfilled %s=%s into %s.",
                _TOKEN_VAR,
                agent_name,
                _AGENT_ID_VAR,
                existing[_AGENT_ID_VAR],
                env_file,
            )
        return

    declared = _declared_slot(config)
    candidates = [declared] if declared else _slot_candidates(agent_name, workdir)

    pool = _pool_env()
    for slot in candidates:
        value = pool.get(f"{_POOL_PREFIX}{slot}", "")
        if value:
            existing[_TOKEN_VAR] = value
            existing.setdefault(_AGENT_ID_VAR, _default_agent_id(agent_name, workdir))
            _write_env_file(env_file, existing)
            _logger().info(
                "cct: resolved bot token for agent %r from pool slot "
                "%s%s (value not logged) -> %s; %s=%s.",
                agent_name,
                _POOL_PREFIX,
                slot,
                env_file,
                _AGENT_ID_VAR,
                existing[_AGENT_ID_VAR],
            )
            return

    _logger().warning(
        "cct: no Telegram bot token for agent %r although spec.claude.channels "
        "requests %r. Tried pool slot(s) %s against the pool (%s). THE AGENT "
        "STARTS NORMALLY — this is NOT a startup failure; only the Telegram "
        "rail is down, and it is down in BOTH directions: the telegrammer MCP "
        "entry is REMOVED from the materialised .mcp.json (a server that "
        "cannot start is worse than an absent one), so this agent is MUTE and "
        "DEAF on Telegram until a token is provided. Expected for a "
        "brand-new agent that has no bot yet. To wire one up, do ONE of: "
        "(1) add %s<SLOT>=<token> for slot %r to a secrets file listed in %s "
        "(canonical pool; restart `sac listen` afterwards if it provides the "
        "env), (2) set spec.apptainer.env %s: <existing-slot> to reuse "
        "another project's bot, or (3) export %s via the project's .envrc "
        "(%s/.envrc). Or drop %r from spec.claude.channels if this agent "
        "needs no Telegram rail.",
        agent_name,
        _TELEGRAMMER_CHANNEL,
        ", ".join(f"{_POOL_PREFIX}{c}" for c in candidates) or "(none)",
        _pool_source_label(),
        _POOL_PREFIX,
        candidates[0] if candidates else "<PROJECT>",
        _SECRETS_ENVRC_VAR,
        _SLOT_OVERRIDE_VAR,
        _TOKEN_VAR,
        workdir or "<workdir>",
        _TELEGRAMMER_CHANNEL,
    )


def _default_agent_id(agent_name: str, workdir: str) -> str:
    """The agent's telegram identity: **its own name**. Always.

    ``workdir`` is accepted and deliberately IGNORED (see
    :func:`_slot_candidates` for the full reasoning and the operator's ruling).

    This is the more dangerous half of the same bug, and it was the harder one
    to see because the old docstring described it as a feature: "the PROJECT
    (workdir basename) — matching the per-project ``.envrc`` convention". sac
    was not merely vulnerable to the `.envrc` anti-pattern; it had DELIBERATELY
    COPIED it, and said so in prose. An agent working in ``~/proj/scitex-todo``
    was assigned ``CCT_AGENT_ID = "scitex-todo"`` by sac itself — the exact
    impersonation, from this function, with no `.envrc` in the path at all.

    A slot is a resource and stealing one is loud (Telegram 409s until someone
    notices). An IDENTITY is a claim, and a wrong one is silent: it produces no
    error, only a wrong author, and a forged author is indistinguishable from a
    real one after the fact. So this default must be the one thing that cannot
    be borrowed from the surroundings.

    Found by grepping every consumer of the workdir-derived rule rather than
    stopping at the first — the reported instance is a sample, not the
    population. Fixing only ``_slot_candidates`` would have left the identity
    itself still keyed on location, which is the part that actually matters.
    """
    return agent_name


def prune_tokenless_telegrammer_mcp(dest: Path, *, config=None) -> bool:
    """Drop the telegrammer MCP server from ``dest/.mcp.json`` when no token resolved.

    Card ``sac-omit-telegram-mcp-when-no-cct-bot-token-20260702`` (operator
    decision 2026-07-02, option 1). The SHARED baseline ``.mcp.json`` declares
    ``claude-code-telegrammer`` for every agent, with ``"CCT_BOT_TOKEN":
    "${CCT_BOT_TOKEN}"``. An agent that intentionally has NO bot (most library /
    tool agents) therefore launches that server with an EMPTY token, and
    claude-code-telegrammer correctly refuses to start on an empty token. The
    result is a permanent ``✘ failed`` row in the MCP panel — on every agent,
    forever — which is noise in the one view the operator actually checks.

    That fail-loud is right for a MISCONFIGURED agent and wrong for a
    deliberately bot-less one, and the ENTRY cannot tell them apart, so the fix
    is to not emit the entry: no token → no server → nothing to fail. An agent
    WITH a token is untouched.

    Removing it SILENTLY, however, is how a misconfigured agent hides. Measured
    2026-08-10, card ``sac-cct-prune-hides-misconfigured-telegram-agent-20260810``:
    four agents on a new host went MUTE **and** DEAF on Telegram behind one INFO
    line each, and the operator — getting no answers — concluded they were
    ignoring him. Deafness is the half that surprises: the entry's absence kills
    inbound too, and the agent cannot even self-diagnose, because ``health`` is
    itself a tool on the very MCP server that just went away.

    So ``config`` is read and the LEVEL splits on what the spec DECLARED:
    an explicit ``spec.apptainer.env: CCT_BOT_TOKEN_SLOT: <X>`` whose pool slot
    is absent/empty → ERROR (a stated mapping is broken; unambiguous, and rare
    enough that ERROR stays quiet); no declared slot → INFO, unchanged, the
    intentional no-bot path. ``config=None`` keeps the pre-2026-08-10 blind
    behaviour; the real call site in :func:`._to_home.deploy_to_home` passes one.

    The trigger is deliberately NOT "the spec requests the channel". On
    compute-04, 2026-08-10: 80 specs request it, 14 resolve a token, 66 do not —
    and the 66 include ``_template_generalist``, ``_template_python_developer``
    and ``_template_researcher``. The request is INHERITED FROM THE TEMPLATES,
    so it measures scaffolding, not intent; keying ERROR on it would print 66
    red lines into the one panel the operator actually checks — recreating, as
    a "fix", the exact noise this prune was written to remove. A declared slot
    is the one thing here that somebody had to type on purpose, and it composes
    with the operational remedy: give an agent that SHOULD have a bot an
    explicit override, and from then on any breakage screams.

    Ordering is load-bearing: this must run AFTER :func:`ensure_cct_bot_token`,
    because that is what resolves a pool token into ``dest/.env``. Running it
    before would read an env that has not been populated yet and prune the entry
    from agents that do in fact have a bot.

    Returns True iff the entry was removed (so the caller can log a deploy).
    Never raises — a malformed ``.mcp.json`` is left exactly as-is for the
    deploy's own fail-loud JSON handling to report.
    """
    mcp_path = dest / ".mcp.json"
    if not mcp_path.is_file():
        return False
    env_file = dest / ".env"
    env = _read_env_file(env_file) if env_file.is_file() else {}
    if str(env.get(_TOKEN_VAR, "") or "").strip():
        return False  # real bot token — keep the server.
    try:
        doc = json.loads(mcp_path.read_text() or "{}")
    except (
        OSError,
        json.JSONDecodeError,
    ) as exc:  # stx-allow: fallback (reason: the .mcp.json deploy owns JSON fail-loud; pruning must not double-report or mask it)
        _logger().warning(
            "cct: could not read %s to prune the tokenless telegrammer entry "
            "(%s); leaving it untouched.",
            mcp_path,
            exc,
        )
        return False
    servers = doc.get("mcpServers") if isinstance(doc, dict) else None
    if not isinstance(servers, dict) or _TELEGRAMMER_MCP_KEY not in servers:
        return False
    del servers[_TELEGRAMMER_MCP_KEY]
    mcp_path.write_text(json.dumps(doc, indent=2) + "\n")
    declared = _declared_slot(config) if config is not None else ""
    if declared and _channel_requested(config):
        slot_var = f"{_POOL_PREFIX}{declared}"
        _logger().error(
            "cct: agent %r DECLARES pool slot %s via spec.apptainer.env %s, but "
            "that slot is absent or empty in the pool (%s) — a declared mapping "
            "that does not work, i.e. a MISCONFIGURATION, not the intentional "
            "no-bot path. The %r MCP server was REMOVED from %s, so the "
            "Telegram rail is down BOTH ways: this agent is MUTE (cannot send) "
            "and DEAF (never receives), and it cannot even self-diagnose, "
            "because `health` is itself a tool on the server that just went "
            "away. Fix by EITHER adding %s=<token> to a secrets file listed in "
            "%s (restart `sac listen` afterwards if it provides the env), OR "
            "correcting %s to a slot that exists, OR removing the override if "
            "this agent needs no Telegram rail.",
            getattr(config, "name", "") or "",
            slot_var,
            _SLOT_OVERRIDE_VAR,
            _pool_source_label(),
            _TELEGRAMMER_MCP_KEY,
            mcp_path,
            slot_var,
            _SECRETS_ENVRC_VAR,
            _SLOT_OVERRIDE_VAR,
        )
        return True
    _logger().info(
        "cct: no %s resolved for this agent — omitted the %r MCP server from "
        "%s so it does not start and fail on an empty token. This is the "
        "intentional no-bot path, not an error.",
        _TOKEN_VAR,
        _TELEGRAMMER_MCP_KEY,
        mcp_path,
    )
    return True


__all__ = ["ensure_cct_bot_token", "prune_tokenless_telegrammer_mcp"]
