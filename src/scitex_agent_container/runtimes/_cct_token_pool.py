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

Loud-but-honest contract: when the channel is requested and NO token
resolves, a scitex-logging WARNING names the pool source, the tried slot
names, and the fixes. The start itself proceeds — Telegram is a comms rail,
not a boot dependency — so the absence is loud but never silent, and never
DRESSED UP AS A STARTUP FAILURE. It used to log at ERROR, which made every
brand-new agent (no bot yet, by definition) look stillborn in its boot log
next to the genuinely fatal lines; WARNING + an explicit "the agent starts
normally" sentence keeps the signal without the false alarm.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

from ._sdk_channels import _TELEGRAMMER_CHANNEL

# The env-var the telegrammer MCP reads its bot token from (see the shared
# baseline ``.mcp.json``: ``"CCT_BOT_TOKEN": "${CCT_BOT_TOKEN}"``).
_TOKEN_VAR = "CCT_BOT_TOKEN"
# Per-agent telegram identity var, same runtime-expansion contract.
_AGENT_ID_VAR = "CCT_AGENT_ID"
# Pool naming convention: one ``CCT_BOT_TOKEN_<SLOT>`` per project.
_POOL_PREFIX = "CCT_BOT_TOKEN_"
# Optional explicit slot override in ``spec.apptainer.env``.
_SLOT_OVERRIDE_VAR = "CCT_BOT_TOKEN_SLOT"
# The .envrc secrets-preamble env var (shared with :mod:`._envrc`).
_SECRETS_ENVRC_VAR = "SAC_SECRETS_ENVRC"


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


def _read_env_file(path: Path) -> dict[str, str]:
    """Parse a plain ``KEY=VALUE``-per-line env file (the fold's format).

    Tolerates blank lines and ``#`` comments; no shell semantics (the fold
    writes raw values, no quoting/export).
    """
    env: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        key, sep, val = line.partition("=")
        if sep:
            env[key.strip()] = val
    return env


def _pool_env() -> dict[str, str]:
    """The pool: ``CCT_BOT_TOKEN_*`` vars from the launching env, overlaid
    with the secret files (daemon-start path).

    Resolves the secret files via :func:`._envrc.resolve_secret_files`, which
    honours an explicit ``SAC_SECRETS_ENVRC`` AND — the 2026-07-18 class fix —
    falls back to the canonical ``$HOME`` default pool when the var is unset, so
    a cron / raw-ssh / federated-timer restart that never had the var exported
    still finds the bot token instead of folding (and STRIPPING) it. Sourced in
    a strict bash with the same ``set -a`` semantics as the ``.envrc`` fold.
    Falls back to the plain process env when no secret file resolves, and
    degrades to the process env (rather than failing the deploy) if a resolved
    secret file cannot be sourced — the caller's missing-token WARNING then
    names the pool source anyway.
    """
    import shlex

    from ._envrc import EnvrcEvalError, _capture_env, resolve_secret_files

    files = resolve_secret_files()
    if not files:
        return dict(os.environ)
    preamble = [f". {shlex.quote(str(p))}" for p in files]
    try:
        return _capture_env(
            "\n".join(["set -a", *preamble, "set +a", "env -0"]), Path.cwd()
        )
    except EnvrcEvalError as exc:  # stx-allow: fallback (reason: pool read must not abort deploy; missing token is reported loudly by the caller)
        _logger().warning(
            "cct pool: failed to source %s (%s); falling back to the "
            "launching process env only.",
            _pool_source_label(),
            exc,
        )
        return dict(os.environ)


def _pool_source_label() -> str:
    """Human-readable pool location for log lines (paths only, no values)."""
    raw = os.environ.get(_SECRETS_ENVRC_VAR, "")
    if raw:
        return f"{_SECRETS_ENVRC_VAR}={raw}"
    # Class fix (2026-07-18): an unset var no longer means an empty pool — the
    # resolver falls back to the canonical ``$HOME`` default. Report THAT so the
    # missing-token WARN names where sac actually looked, not a pool it stopped
    # limiting itself to.
    from ._envrc import resolve_secret_files

    defaults = resolve_secret_files()
    if defaults:
        joined = ":".join(str(p) for p in defaults)
        return f"{_SECRETS_ENVRC_VAR} unset — using the canonical default pool {joined}"
    return (
        f"{_SECRETS_ENVRC_VAR} is UNSET and no canonical default pool files were "
        "found — pool limited to the launching process environment"
    )


def _write_env_file(path: Path, env: dict[str, str]) -> None:
    """Rewrite ``path`` as sorted ``KEY=VALUE`` lines, owner-only perms."""
    body = "".join(f"{k}={v}\n" for k, v in sorted(env.items()))
    path.write_text(body, encoding="utf-8")
    os.chmod(path, 0o600)


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
    claude_spec = getattr(config, "claude", None)
    channels = list(getattr(claude_spec, "channels", None) or [])
    if not any(str(c).strip() == _TELEGRAMMER_CHANNEL for c in channels):
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

    spec_env = getattr(config, "env", None) or {}
    override = str(spec_env.get(_SLOT_OVERRIDE_VAR, "") or "").strip()
    if override:
        candidates = [_upper_snake(override)]
    else:
        candidates = _slot_candidates(agent_name, workdir)

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
        "rail is down (the telegrammer MCP comes up without a token, so the "
        "bot stays silent until a token is provided). Expected for a "
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


__all__ = ["ensure_cct_bot_token"]
