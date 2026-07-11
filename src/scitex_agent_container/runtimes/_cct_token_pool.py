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
* One slot per PROJECT (per-project bot ⇒ no Telegram 409 single-poller
  combat): ``CCT_BOT_TOKEN_PAPER_SCITEX_CLEW``, ``CCT_BOT_TOKEN_TODO``, …

Resolution order for an agent (first hit wins):

1. An explicit non-empty ``CCT_BOT_TOKEN`` already folded into the agent's
   materialised ``$HOME/.env`` (the per-project ``.envrc`` cascade) — the
   hand-authored mapping stays authoritative.
2. ``spec.apptainer.env: CCT_BOT_TOKEN_SLOT: <SLOT>`` — explicit per-spec
   slot override for names that don't map mechanically (e.g. ``SAC``).
   When set, ONLY that slot is tried (fail-loud on a typo, no silent
   mechanical fallback).
3. Mechanical candidates derived from the workdir basename (the project)
   then the agent name: upper-snake of the base, plus the same with a
   leading ``scitex-`` prefix stripped (``scitex-todo`` → ``TODO``).

On a hit the token is appended to ``$HOME/.env`` (``chmod 0600``) — the
SAME carrier the ``.envrc`` fold uses, so the container receives it via
apptainer ``--env-file`` and the materialised ``.mcp.json``'s literal
``${CCT_BOT_TOKEN}`` / ``${CCT_AGENT_ID}`` refs expand at runtime. The
token deliberately NEVER rides an ``--env`` argv flag (visible in
``/proc/<pid>/cmdline``) and its VALUE is never logged — log lines carry
only the slot NAME, the pool source path(s), and the agent name.

Fail-loud contract: when the channel is requested and NO token resolves,
a scitex-logging ERROR names the pool source, the tried slot names, and
the three fixes. The start itself proceeds (Telegram is a comms rail, not
a boot dependency) — but the absence is loud, never silent.
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

    Workdir basename first (the bot is per-PROJECT), then the agent name;
    each base contributes its upper-snake form plus the same with a leading
    ``scitex-``/``scitex_`` prefix stripped (the pool names the core scitex
    packages by their short slot: ``TODO``, ``DEV``, …).
    """
    bases: list[str] = []
    wd = (workdir or "").strip()
    if wd:
        bases.append(Path(wd).expanduser().name)
    if name and name not in bases:
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
    with the ``SAC_SECRETS_ENVRC`` secret files (daemon-start path).

    Reuses :func:`._envrc._capture_env` + the shared preamble so the pool
    read has EXACTLY the same semantics as the ``.envrc`` fold's secret
    resolution. Falls back to the plain process env when no secret file is
    configured/present, and degrades to the process env (rather than
    failing the deploy) if a listed secret file cannot be sourced — the
    caller's missing-token ERROR then names the pool source anyway.
    """
    from ._envrc import EnvrcEvalError, _capture_env, _secrets_preamble_lines

    preamble = _secrets_preamble_lines()
    if not preamble:
        return dict(os.environ)
    try:
        return _capture_env(
            "\n".join(["set -a", *preamble, "set +a", "env -0"]), Path.cwd()
        )
    except EnvrcEvalError as exc:  # stx-allow: fallback (reason: pool read must not abort deploy; missing token is reported loudly by the caller)
        _logger().warning(
            "cct pool: failed to source %s=%s (%s); falling back to the "
            "launching process env only.",
            _SECRETS_ENVRC_VAR,
            os.environ.get(_SECRETS_ENVRC_VAR, ""),
            exc,
        )
        return dict(os.environ)


def _pool_source_label() -> str:
    """Human-readable pool location for log lines (paths only, no values)."""
    raw = os.environ.get(_SECRETS_ENVRC_VAR, "")
    if raw:
        return f"{_SECRETS_ENVRC_VAR}={raw}"
    return (
        f"{_SECRETS_ENVRC_VAR} is UNSET — pool limited to the launching "
        "process environment"
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
    Never raises for a missing token — it ERRORs loudly (scitex-logging)
    with the pool path and the fix instead. The token VALUE is never
    logged; only slot names, paths, and the agent name appear.
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
            existing.setdefault(
                _AGENT_ID_VAR, _default_agent_id(agent_name, workdir)
            )
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

    _logger().error(
        "cct: NO bot token for agent %r although spec.claude.channels "
        "requests %r. Tried pool slot(s) %s against the pool (%s). The "
        "telegrammer MCP will start WITHOUT a token — the bot is dead until "
        "fixed. Fix ONE of: (1) add %s<SLOT>=<token> for slot %r to a "
        "secrets file listed in %s (canonical pool; restart `sac listen` "
        "afterwards if it provides the env), (2) set spec.apptainer.env "
        "%s: <existing-slot> to reuse another project's bot, or (3) export "
        "%s via the project's .envrc (%s/.envrc).",
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
    )


def _default_agent_id(agent_name: str, workdir: str) -> str:
    """Default telegram identity: the PROJECT (workdir basename) — matching
    the per-project ``.envrc`` convention — falling back to the agent name.
    """
    wd = (workdir or "").strip()
    if wd:
        base = Path(wd).expanduser().name
        if base:
            return base
    return agent_name


__all__ = ["ensure_cct_bot_token"]
