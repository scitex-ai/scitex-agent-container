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

An external channel bridge may consume the same JSON file with the same
``short``-field lookup rule — keeping the two implementations symmetric.
PR-A wires that bridge; this module wires the Python side.

The reader **never raises** — every failure mode (missing env, missing
file, malformed JSON, no matching account, wrong-typed entry fields)
collapses to a structured ``None`` / empty-dict so callers can degrade
gracefully. The operator's #16 brief is explicit: "fresh quota source,
read at SEND time" — stale data is a degradation, not a hard error.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

# In-container path the apptainer runtime binds the host file at. PR-A
# (the channel bridge) and PR-B (sac CLI / a2a metadata) both default to
# this same path so a single bind in
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


def _resolve_cache_path(override: Path | str | None) -> Path:
    if override is not None:
        return Path(override)
    env_path = os.environ.get(ENV_QUOTA_CACHE_PATH, "").strip()
    return Path(env_path) if env_path else Path(DEFAULT_QUOTA_CACHE_PATH)


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
    # convention (``ywata1989-gmail-com`` → ``ywata1989``,
    # ``ywatanabe-scitex-ai`` → ``ywatanabe``).
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


__all__ = [
    "DEFAULT_QUOTA_CACHE_PATH",
    "ENV_QUOTA_CACHE_PATH",
    "ENV_ACCOUNT",
    "META_KEY_ACCOUNT",
    "META_KEY_PCT_5H",
    "META_KEY_PCT_7D",
    "META_KEY_TTL_H",
    "read_quota_entry",
    "build_a2a_metadata",
]
