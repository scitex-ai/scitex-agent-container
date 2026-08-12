"""Read the in-container quota-cache.json bound by the apptainer runtime.

Single source of truth on the Python side for issue #16's quota-visibility
requirements:

* the a2a transport (``_mcp/_channel_tools._wrap_message_send``) attaches
  ``account`` + ``used_pct_5h`` + ``used_pct_7d`` + ``token_ttl_hours``
  to EVERY outbound message so peers can detect impending quota
  exhaustion and adapt (back-pressure, route-around);
* the ``sac account quota`` CLI exposes the same lookup to the in-agent
  Claude session for self-awareness ("am I about to hit the wall?");
* the apptainer runtime binds the host's
  ``/home/ywatanabe/.scitex/quota-cache.json`` at
  ``/var/sac/quota-cache.json`` (read-only) so both consumers see the
  same file with the same path.

The TS bridge (``claude-code-telegrammer/ts/lib/signature.ts``) consumes
the same JSON file with the same ``short``-field lookup rule — keeping
the two implementations symmetric. PR-A wires the bridge; this module
wires the Python side.

The reader **never raises** — every failure mode (missing env, missing
file, malformed JSON, no matching account, wrong-typed entry fields)
collapses to a structured ``None`` / empty-dict so callers can degrade
gracefully. The operator's #16 brief is explicit: "fresh quota source,
read at SEND time" — stale data is a degradation, not a hard error.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

# In-container path the apptainer runtime binds the host file at. PR-A
# (telegrammer) and PR-B (sac CLI / a2a metadata) both default to this
# same path so a single bind in
# ``_apptainer_runtime.ApptainerContainerRuntime.build_run_argv`` makes
# every consumer work without per-component plumbing.
DEFAULT_QUOTA_CACHE_PATH = "/var/sac/quota-cache.json"

# Env overrides — primarily for tests / host-side use of `sac account
# quota` where the cache lives at its canonical host location
# ``/home/ywatanabe/.scitex/quota-cache.json`` rather than the bound
# container path. Both empty / unset fall back to the default.
ENV_QUOTA_CACHE_PATH = "SAC_QUOTA_CACHE_PATH"
ENV_ACCOUNT = "CLAUDE_AGENT_ACCOUNT"

# Metadata field names emitted on outbound a2a payloads + by `sac
# account quota --json`. Chosen to match the existing usage-tracking
# nomenclature in ``_account/claude_usage.py`` (``used_pct_5h``,
# ``used_pct_7d``) and to read clearly in TTY output (``token_ttl_hours``
# vs. the cache's compact ``ttl_h``). Centralised here so a future
# rename is a one-place change.
META_KEY_ACCOUNT = "account"
META_KEY_PCT_5H = "used_pct_5h"
META_KEY_PCT_7D = "used_pct_7d"
META_KEY_TTL_H = "token_ttl_hours"


# Host-side quota-cache locations, most-canonical first. sac's quota cache
# is sac RUNTIME STATE, so its home is under sac's OWN runtime dir
# (``~/.scitex/agent-container/runtime/``) per the constitution §3
# per-package runtime convention — NOT the shared ``~/.scitex`` root
# (operator, 2026-07-11: "sac 管轄で使うランタイム state … .scitex 以下に
# 直で置くな"). The legacy top-level path is kept as a transitional
# back-compat read until the populator + apptainer bind are migrated too
# (tracked separately so the bind move can't strand running containers).
HOST_RUNTIME_CACHE_SUBPATH = (
    Path(".scitex") / "agent-container" / "runtime" / "quota-cache.json"
)
LEGACY_HOST_CACHE_SUBPATH = Path(".scitex") / "quota-cache.json"


def host_cache_candidates(home: Path | None = None) -> tuple[Path, ...]:
    """Ordered host quota-cache paths, canonical (runtime) first, legacy last."""
    _home = home if home is not None else Path.home()
    return (
        _home / HOST_RUNTIME_CACHE_SUBPATH,
        _home / LEGACY_HOST_CACHE_SUBPATH,
    )


def _first_existing(paths: tuple[Path, ...]) -> Path | None:
    for p in paths:
        if p.exists():
            return p
    return None


def _resolve_cache_path(override: Path | str | None) -> Path:
    if override is not None:
        return Path(override)
    env_path = os.environ.get(ENV_QUOTA_CACHE_PATH, "").strip()
    if env_path:
        return Path(env_path)
    # No override / env. The reader's historical default is the in-container
    # bind path (/var/sac/quota-cache.json). But the SAME reader runs HOST-side
    # for every `sac agents start` / `sac account quota` the operator (or the
    # listen daemon) invokes — and on the host that bind path does NOT exist,
    # so without a fallback the picker reads "5h=? 7d=?" for every account and
    # can no longer avoid a quota-blocked one (2026-07-11 incident: host
    # `sac-start` landed agents on the 5h-exhausted account). Fail-INFORMED,
    # not fail-open (constitution §2): container bind first (freshest inside a
    # capsule), then the host runtime canonical, then the legacy path; only if
    # none exist return the container default so the caller degrades to an
    # honest None.
    container = Path(DEFAULT_QUOTA_CACHE_PATH)
    if container.exists():
        return container
    host = _first_existing(host_cache_candidates())
    return host if host is not None else container


def quota_cache_present(cache_path: Path | str | None = None) -> bool:
    """Whether a quota-cache FILE actually exists for the reader to consult.

    * ``True`` — a cache source exists (the container bind, or a host
      ``quota-cache.json`` a populator/cron writes). On such a host an
      all-UNKNOWN pick is a POPULATOR failure (an empty/stale cache), so the
      boot should fail loud (constitution §2 — unknown is not "OK") rather than
      silently launch on an unverifiable, possibly quota-exhausted account
      (2026-07-20 incident).
    * ``False`` — no cache source exists at all (a fresh install, CI, or a
      quota-cron-less host such as a Spartan compute node).

    NOT the boot gate's discriminator, though it was used as one. The start
    preflight armed :func:`_creds.pick_healthy_account`'s
    ``require_quota_evidence`` with this predicate directly, which meant the
    gate could not fire in the situation it was built for: it protects against
    "the cache tells us nothing", yet it was armed only when a cache FILE
    already existed, so a host with NO cache — the blind case — ran DISARMED
    (2026-08-06, scitex-02: an agent booted onto a d7=100% account and answered
    "You've hit your weekly limit" on every turn while startup reported
    success). :func:`_lifecycle._quota_evidence.pick_with_quota_evidence` owns
    that decision now: a ``False`` here means "try to BUILD the evidence", and
    only a build that genuinely cannot run degrades to freshness-only — loudly.

    The never-block invariant this predicate exists to protect is unchanged and
    deliberate: a boot is NEVER blocked merely because this host runs no quota
    system. The defect was never that sac declined to block; it was that sac
    went silent.

    Mirrors :func:`_resolve_cache_path`'s resolution order (container bind →
    host runtime → legacy). A missing source resolves to the non-existent
    container default, hence ``False``.
    """
    return _resolve_cache_path(cache_path).exists()


def quota_cache_entry_count(cache_path: Path | str | None = None) -> int:
    """How many per-account entries the resolved cache actually holds.

    :func:`quota_cache_present` answers "is there a FILE"; this answers "does
    that file say anything". The two differ for exactly one input — a cache
    written with zero accounts (``{"accounts": {}}``) — and that difference
    is what lets the boot picker's blind-pick refusal name the right remedy:
    zero entries means the POPULATOR produced nothing (re-running it is not
    obviously the fix), a non-zero count means the cache is populated but
    STALE or mismatched for this fleet (re-running it is exactly the fix).

    Missing / unreadable / malformed → ``0``. Never raises.
    """
    path = _resolve_cache_path(cache_path)
    try:
        parsed = json.loads(path.read_text(encoding="utf-8"))
    except (
        OSError,
        ValueError,
        TypeError,
    ):  # stx-allow: fallback (reason: mirrors read_quota_entry — an absent or corrupt cache is a normal cold-start state, and "0 entries" is the honest answer for it rather than an exception the caller must special-case)
        return 0
    accounts = parsed.get("accounts") if isinstance(parsed, dict) else None
    return len(accounts) if isinstance(accounts, dict) else 0


def diagnose_quota_cache(
    cache_path: Path | str | None = None,
) -> tuple[str, int, Path]:
    """Classify WHAT the reader finds at the resolved cache path.

    :func:`quota_cache_entry_count` answers ``0`` for four different worlds —
    absent, unreadable, malformed, and genuinely empty — which is fine for a
    counter and wrong for anything that explains a failure to a human. A caller
    branching on ``== 0`` has to invent a cause, and on 2026-07-29 one did: the
    blind-pick remedy told the operator "the cache exists but holds ZERO account
    entries, so the populator has never written a successful one" while the cron
    populator was writing three accounts every five minutes and the file held
    them. Both halves of that sentence were unmeasured; the operator re-ran the
    refresh twice on its advice and nothing changed.

    Returns ``(state, entries, path)`` where ``state`` is one of ``absent`` /
    ``unreadable`` / ``malformed`` / ``empty`` / ``populated``. The resolved
    PATH is returned because "which file did it actually read" is the first
    question anyone asks of this failure, and neither helper could answer it.

    Never raises — same contract as the rest of this module.
    """
    path = _resolve_cache_path(cache_path)
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return ("absent", 0, path)
    except OSError:
        # Permissions, a dangling symlink, an unreadable mount — the file is
        # THERE as far as ``quota_cache_present`` is concerned, so this state
        # is invisible to a present/count pair and must be named separately.
        return ("unreadable", 0, path)
    try:
        parsed = json.loads(raw)
    except ValueError:
        return ("malformed", 0, path)
    accounts = parsed.get("accounts") if isinstance(parsed, dict) else None
    if not isinstance(accounts, dict):
        # Valid JSON, wrong shape — a truncated or hand-edited file reaches
        # here and is NOT "the populator never ran".
        return ("malformed", 0, path)
    return ("populated" if accounts else "empty", len(accounts), path)


def _resolve_account(override: str | None) -> str:
    if override is not None:
        return override.strip()
    return os.environ.get(ENV_ACCOUNT, "").strip()


def _is_number(v: Any) -> bool:
    # bool is an int in Python — explicitly reject so True doesn't
    # silently surface as 1.0% utilisation downstream. Mirrors
    # ``_account/claude_usage._coerce_utilization_pct``.
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def read_quota_entry(
    *,
    account: str | None = None,
    cache_path: Path | str | None = None,
) -> dict[str, Any] | None:
    """Return the per-account quota entry for THIS agent, or ``None``.

    Match rule mirrors the TS bridge's ``readQuotaEntry``: iterate
    ``accounts.values()`` and return the first entry whose ``short`` field
    equals the first dash-segment of the account dirname. This is robust
    to multi-dot TLDs (``gmail.com``, ``scitex.ai``) without parsing the
    domain side.

    Args:
        account: Override the account dirname. Defaults to
            ``$CLAUDE_AGENT_ACCOUNT``. Empty / whitespace-only disables
            the lookup (returns ``None``).
        cache_path: Override the cache file path. Defaults to
            ``$SAC_QUOTA_CACHE_PATH`` → ``DEFAULT_QUOTA_CACHE_PATH``.

    Returns:
        Dict copy of the cache entry with keys ``short``, ``h5``, ``d7``,
        ``ttl_h`` (and any other entry-level fields the host adds in the
        future — we copy the whole dict so additions surface to callers
        without a code change). ``None`` on any failure mode.

    Never raises.
    """
    dirname = _resolve_account(account)
    if not dirname:
        return None
    # First dash-segment is the email local-part per operator's stated
    # convention (``alpha-example-com`` → ``alpha``,
    # ``researcher-example-org`` → ``researcher``).
    short = dirname.split("-", 1)[0]
    if not short:
        return None

    path = _resolve_cache_path(cache_path)
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:  # stx-allow: fallback (reason: quota cache may legitimately not exist yet on a fresh host or in CI; None signals the caller to degrade — quota visibility is non-critical for delivery)
        return None
    try:
        parsed = json.loads(raw)
    except (
        ValueError,
        TypeError,
    ):  # stx-allow: fallback (reason: a corrupt cache file is recoverable on the next cron tick — failing the send/render would be worse than degrading once)
        return None

    accounts = parsed.get("accounts") if isinstance(parsed, dict) else None
    if not isinstance(accounts, dict):
        return None

    for v in accounts.values():
        if (
            isinstance(v, dict)
            and v.get("short") == short
            and _is_number(v.get("h5"))
            and _is_number(v.get("d7"))
            and _is_number(v.get("ttl_h"))
        ):
            # Return a shallow copy so callers can mutate freely (e.g.
            # the a2a metadata path tags additional fields onto the dict
            # before forwarding to peers).
            return dict(v)
    return None


def build_a2a_metadata() -> dict[str, Any]:
    """Return account+quota metadata to merge into outbound a2a payloads.

    Empty dict when no entry is resolvable — callers can safely
    ``metadata.update(build_a2a_metadata())`` without leaking a flock of
    ``None``-valued fields onto the wire. The lead's #16 brief asks for
    STRUCTURED fields (not text) precisely so peers can branch on
    ``"account" in meta`` cleanly; an empty dict preserves that
    contract.

    Field shape:
        {
          "account":          <short, str>,
          "used_pct_5h":      <h5, float>,
          "used_pct_7d":      <d7, float>,
          "token_ttl_hours":  <ttl_h, float>,
        }
    """
    entry = read_quota_entry()
    if entry is None:
        return {}
    return {
        META_KEY_ACCOUNT: entry["short"],
        META_KEY_PCT_5H: entry["h5"],
        META_KEY_PCT_7D: entry["d7"],
        META_KEY_TTL_H: entry["ttl_h"],
    }


# ---------------------------------------------------------------------------
# Writer side — the POPULATOR that produces the aggregate quota-cache.json
# the reader above consumes. Its default MUST be the SAME path the reader's
# first candidate resolves to (``HOST_RUNTIME_CACHE_SUBPATH``), or the two
# silently diverge: a prior split (writer → legacy ~/.scitex/quota-cache.json,
# reader → runtime) meant `sac accounts refresh-quota-cache` wrote a file the
# picker never read, so the picker stayed BLIND and the fail-loud boot gate's
# own actionable hint ("run refresh-quota-cache") could not clear the block
# (2026-07-20 incident: `sac-restart scitex-dev` hard-failed and the documented
# fix did nothing). Writer default == reader first candidate == apptainer bind
# is the SSOT that makes that hint actually work. ``SAC_QUOTA_CACHE_PATH``
# overrides both ends so host readers/writers and tests co-locate on one path.
DEFAULT_HOST_QUOTA_CACHE_SUBPATH = HOST_RUNTIME_CACHE_SUBPATH


def default_host_cache_path(home: Path | None = None) -> Path:
    """Canonical HOST path the populator writes.

    ``~/.scitex/agent-container/runtime/quota-cache.json`` — the SAME path
    :func:`host_cache_candidates` returns first (the reader) and the apptainer
    bind resolves to, so a plain ``sac accounts refresh-quota-cache`` populates
    exactly the file the boot picker reads.
    """
    _home = home if home is not None else Path.home()
    return _home / DEFAULT_HOST_QUOTA_CACHE_SUBPATH


def _resolve_write_cache_path(
    override: Path | str | None,
    home: Path | None,
) -> Path:
    """Resolve where the populator writes: explicit → env → host default.

    Deliberately distinct from :func:`_resolve_cache_path` (the reader): the
    reader's no-env default is the in-container bind path, but the writer's
    no-env default is the host canonical file. ``SAC_QUOTA_CACHE_PATH`` is the
    shared override so a host that reads AND writes (or a test) lines both up.
    """
    if override is not None:
        return Path(override)
    env_path = os.environ.get(ENV_QUOTA_CACHE_PATH, "").strip()
    if env_path:
        return Path(env_path)
    return default_host_cache_path(home)


def write_quota_cache(
    accounts: dict[str, Any],
    *,
    cache_path: Path | str | None = None,
    home: Path | None = None,
    written_at: float | None = None,
) -> Path:
    """Atomically write the aggregate quota cache and return the path written.

    ``accounts`` is the ``{"<key>": {"short", "h5", "d7", "ttl_h"}}`` mapping
    the reader's :func:`read_quota_entry` iterates. The file is wrapped as
    ``{"written_at": <epoch>, "accounts": {...}}`` — the exact shape the
    reader (and the TS bridge) expect. Write is tmp+rename atomic and the file
    is chmod 0o600 (conservative: it holds only percentages + TTL hours, no
    token material, but it lives under the operator's home so we match the
    credential store's private-by-default posture).
    """
    path = _resolve_write_cache_path(cache_path, home)
    payload = {
        "written_at": written_at if written_at is not None else time.time(),
        "accounts": dict(accounts),
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = Path(str(path) + ".tmp")
    tmp.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    # stx-allow: fallback (reason: chmod is a best-effort hardening step; on a
    # filesystem that doesn't support POSIX modes the atomic rename below still
    # publishes the cache, which holds no secrets.)
    try:
        os.chmod(tmp, 0o600)
    except OSError:
        pass
    tmp.rename(path)
    return path


__all__ = [
    "DEFAULT_QUOTA_CACHE_PATH",
    "DEFAULT_HOST_QUOTA_CACHE_SUBPATH",
    "ENV_QUOTA_CACHE_PATH",
    "ENV_ACCOUNT",
    "META_KEY_ACCOUNT",
    "META_KEY_PCT_5H",
    "META_KEY_PCT_7D",
    "META_KEY_TTL_H",
    "read_quota_entry",
    "build_a2a_metadata",
    "default_host_cache_path",
    "diagnose_quota_cache",
    "host_cache_candidates",
    "quota_cache_entry_count",
    "quota_cache_present",
    "write_quota_cache",
]
