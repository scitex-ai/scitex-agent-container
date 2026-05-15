"""Claude quota + account-identity + auth-rotation helpers.

Extracted from ``agent_meta.py`` to keep that module under the 512-line
hook ceiling. The single ``collect_quota_and_account`` entry point
returns a dict that ``collect_rich`` splats into its result so the
output JSON shape stays byte-identical.

Internal — no public API. ``agent_meta`` does not re-export these
helpers (no caller references them by name).
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from ..._account.claude_usage import fetch_usage


def _quota_from_statusline(sl: dict) -> dict[str, Any]:
    """Pull the exact rate-limit values out of the persisted statusline JSON.

    Returns ``{}`` if the statusline dict is empty or lacks rate_limits.
    Live values from Claude Code; ``quota_from_cache=False`` is implied.
    """
    if not sl:
        return {}
    rl = sl.get("rate_limits") or {}
    fh = rl.get("five_hour") or {}
    sd = rl.get("seven_day") or {}
    out: dict[str, Any] = {}
    if fh.get("used_percentage") is not None:
        out["quota_5h_used_pct"] = round(float(fh["used_percentage"]), 1)
    if sd.get("used_percentage") is not None:
        out["quota_7d_used_pct"] = round(float(sd["used_percentage"]), 1)
    out["quota_5h_reset_at"] = fh.get("resets_at") or None
    out["quota_7d_reset_at"] = sd.get("resets_at") or None
    out["quota_from_cache"] = False  # live from statusline, not cached
    return out


def _quota_from_fetch_usage() -> dict[str, Any]:
    """Fall back to the credentials-API scrape when statusline is absent.

    Best-effort: ``quota_error`` carries the exception text on failure.
    """
    out: dict[str, Any] = {}
    # stx-allow: fallback (reason: fetch_usage may fail on network timeout
    # or missing credentials; quota_error captures the reason for callers)
    try:
        usage = fetch_usage()
        out["quota_5h_used_pct"] = usage.get("used_pct_5h")
        out["quota_7d_used_pct"] = usage.get("used_pct_7d")
        out["quota_5h_reset_at"] = usage.get("reset_at_5h")
        out["quota_7d_reset_at"] = usage.get("reset_at_7d")
        out["quota_from_cache"] = bool(usage.get("from_cache", False))
        out["quota_error"] = usage.get("error")
    except Exception as exc:  # stx-allow: fallback
        out["quota_error"] = f"fetch_usage raised: {exc}"
    return out


def _read_account_identity() -> dict[str, Any]:
    """Pull non-secret credential fields for the heartbeat payload.

    Always returns the full set of keys (with ``None`` / ``[]`` for
    missing fields) so the output dict shape is stable on agents
    without credentials.json yet.
    """
    out: dict[str, Any] = {
        "account_email": None,
        "account_plan_label": None,
        "account_subscription_type": None,
        "account_rate_limit_tier": None,
        "account_organization_name": None,
        "account_uuid": None,
        "oauth_expires_at": None,
        "installed_plugins": [],
        "status_line_command": None,
    }
    # stx-allow: fallback (reason: credentials file absent on freshly
    # provisioned agents — account_email stays None until auth completes)
    try:
        from ..._account.credentials import read_credentials_metadata

        cred = read_credentials_metadata()
        out["account_email"] = cred.get("email_address")
        out["account_plan_label"] = cred.get("plan_label")
        out["account_subscription_type"] = cred.get("subscription_type")
        out["account_rate_limit_tier"] = cred.get("rate_limit_tier")
        out["account_organization_name"] = cred.get("organization_name")
        out["account_uuid"] = cred.get("account_uuid")
        expires = cred.get("oauth_expires_at")
        if isinstance(expires, int):
            out["oauth_expires_at"] = expires
        plugins = cred.get("installed_plugins")
        if isinstance(plugins, list):
            out["installed_plugins"] = plugins
        slc = cred.get("status_line_command")
        if isinstance(slc, str):
            out["status_line_command"] = slc
    except Exception:  # stx-allow: fallback (reason: catch-all safety net — see inline comment for context)
        pass
    return out


def _log_auth_rotation(account: dict[str, Any]) -> None:
    """Append one NDJSON line per observed OAuth-token rotation.

    Local-only side-effect: the hub dedupes from per-heartbeat
    ``oauth_expires_at`` so this log is for archival / debugging only.
    Best-effort — silently ignores filesystem / parse errors.
    """
    account_email = account.get("account_email")
    oauth_expires_at = account.get("oauth_expires_at")
    if not (account_email and isinstance(oauth_expires_at, int)):
        return
    try:
        # hook-bypass: line-limit (rotations migrated under accounts/_rotations/ — see GITIGNORED/REFACTORING.md)
        from scitex_config._ecosystem import local_state as _local_state

        rot_dir = _local_state.path("agent-container", "accounts", "_rotations")
        rot_dir.mkdir(parents=True, exist_ok=True)
        rot_file = rot_dir / f"{account_email}.ndjson"
        last_expires: int | None = None
        if rot_file.is_file():
            try:
                for line in reversed(rot_file.read_text().splitlines()):
                    line = line.strip()
                    if not line:
                        continue
                    obj = json.loads(line)
                    if isinstance(obj, dict) and isinstance(
                        obj.get("oauth_expires_at"), int
                    ):
                        last_expires = obj["oauth_expires_at"]
                        break
            except Exception:
                last_expires = None
        if last_expires != oauth_expires_at:
            entry = {
                "ts": datetime.now(tz=timezone.utc).isoformat(),
                "email": account_email,
                "account_uuid": account.get("account_uuid"),
                "oauth_expires_at": oauth_expires_at,
                "plan_label": account.get("account_plan_label"),
            }
            with rot_file.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(entry) + "\n")
    except Exception:
        pass


def collect_quota_and_account(sl: dict) -> dict[str, Any]:
    """Return quota + account fields for ``collect_rich``.

    Combines:
      * statusline-derived rate limits (preferred when present),
      * fetch_usage fallback (when statusline absent),
      * credentials.json identity fields,
      * appends to the auth-rotation NDJSON as a side effect.

    Output dict has stable keys (defaults to ``None`` / ``False`` / ``[]``
    when underlying source missing) so callers can splat unconditionally.
    """
    quota: dict[str, Any] = {
        "quota_5h_used_pct": None,
        "quota_7d_used_pct": None,
        "quota_5h_reset_at": None,
        "quota_7d_reset_at": None,
        "quota_from_cache": False,
        "quota_error": None,
    }
    # Prefer statusline JSON rate-limits over fetch_usage scrape.
    sl_quota = _quota_from_statusline(sl)
    quota.update(sl_quota)
    if quota.get("quota_5h_used_pct") is None:
        quota.update(_quota_from_fetch_usage())

    account = _read_account_identity()
    _log_auth_rotation(account)

    out: dict[str, Any] = {}
    out.update(quota)
    out.update(account)
    return out
