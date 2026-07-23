"""Quota-aware credential rotation monitor.

Monitors 5h/7d quota usage and rotates to a stored account when threshold
is exceeded. Designed to run as a background loop in tmux or as a launchd
daemon.

Usage:
    scitex-agent-container watch-quota [--threshold 80] [--interval 300]
    scitex-agent-container watch-quota --daemon   # detach, write to log
"""

from __future__ import annotations

import logging
import os
import sys
import time
from pathlib import Path

from .._state.account_store import list_accounts, switch_account
from .claude_usage import fetch_usage
from .credentials import read_credentials_metadata

logger = logging.getLogger(__name__)

DEFAULT_THRESHOLD = 80.0  # rotate when usage exceeds this %
DEFAULT_INTERVAL = 300  # check every 5 minutes
SURVIVAL_THRESHOLD = 50.0  # warn in survival mode when usage exceeds this %
_DEFAULT_LOG_PATH = (
    Path.home() / ".scitex" / "agent-container" / "logs" / "quota-watch.log"
)


def _select_next_account(
    accounts: list[dict],
    current_email: str | None,
    *,
    store_dir: Path | None = None,
    home: Path | None = None,
    now: float | None = None,
) -> dict | None:
    """Pick the HEALTHY account with lowest 5h usage that isn't the current one.

    Health is credential-snapshot freshness — a non-expired
    ``claudeAiOauth.expiresAt`` — reusing
    :func:`.._creds._pick_healthy.account_health` (the same predicate the
    boot-time picker uses). An account whose stored credential is EXPIRED
    or ABSENT is NEVER selected, even when its usage reads 0.0%.

    This is the fix for the 2026-07-06 rotation bug: the selector rotated
    the live account to the EXPIRED account ``alpha-example-com`` purely
    because its usage read 0.0%, handing the fleet a dead token. Freshness
    now gates the candidate set before quota ordering.

    Returns ``None`` when NO healthy non-current candidate exists — the
    caller must stay put rather than rotate to an unhealthy account.
    """
    from .._creds._pick_healthy import account_health

    others = [a for a in accounts if a.get("email_address") != current_email]
    healthy = [
        a
        for a in others
        if account_health(
            a.get("name", ""), store_dir=store_dir, home=home, now=now
        ).is_healthy
    ]
    if not healthy:
        return None
    # Sort by quota_5h_used_pct (None treated as 0 = fresh) among HEALTHY only.
    return sorted(healthy, key=lambda a: a.get("quota_5h_used_pct") or 0)[0]


def check_and_rotate(
    threshold: float = DEFAULT_THRESHOLD,
    store_dir: Path | None = None,
    home: Path | None = None,
    dry_run: bool = False,
) -> dict:
    """Single check-and-rotate cycle.

    Args:
        threshold: Rotate when either 5h or 7d usage percentage exceeds this.
        store_dir: Override for the account store directory.
        home: Override for the home directory.
        dry_run: If True, check but do not actually rotate.

    Returns:
        Dict with keys:
            action:       "rotated" | "rotated(dry_run)" | "warning" | "ok"
                          | "no_accounts" | "error"
            quota_5h_pct: float | None
            quota_7d_pct: float | None
            switched_to:  str | None
            message:      str

    Never raises.
    """
    # stx-allow: fallback (reason: quota check must never raise; caller expects a dict with action="error" on any failure)
    try:
        usage = fetch_usage(home=home)
        meta = read_credentials_metadata(home=home)
        current_email = meta.get("email_address")

        q5 = usage.get("used_pct_5h")
        q7 = usage.get("used_pct_7d")
        error = usage.get("error")

        if error:
            return {
                "action": "error",
                "quota_5h_pct": None,
                "quota_7d_pct": None,
                "switched_to": None,
                "message": f"fetch_usage error: {error}",
            }

        # Check if rotation needed
        needs_rotation = (q5 is not None and q5 >= threshold) or (
            q7 is not None and q7 >= threshold
        )

        if not needs_rotation:
            warn_level = threshold * 0.75
            level = (
                "warning"
                if (q5 or 0) >= warn_level or (q7 or 0) >= warn_level
                else "ok"
            )
            return {
                "action": level,
                "quota_5h_pct": q5,
                "quota_7d_pct": q7,
                "switched_to": None,
                "message": f"5h={q5}% 7d={q7}% — below threshold {threshold}%",
            }

        # Rotation needed — find another account
        accounts = list_accounts(store_dir=store_dir, home=home)
        if not accounts:
            return {
                "action": "no_accounts",
                "quota_5h_pct": q5,
                "quota_7d_pct": q7,
                "switched_to": None,
                "message": (
                    f"ALERT: quota {q5}% (5h) — no stored accounts to rotate to. "
                    "Add with: scitex-agent-container account save <name>"
                ),
            }

        next_acct = _select_next_account(
            accounts, current_email, store_dir=store_dir, home=home
        )
        if next_acct is None:
            # No HEALTHY non-current candidate. This covers both "only one
            # account stored" and "every other account's credential is
            # EXPIRED/ABSENT". Staying put is the safe choice — rotating to
            # an unhealthy account would hand the fleet a dead token
            # (the 2026-07-06 expired-account bug). Never rotate here.
            return {
                "action": "no_accounts",
                "quota_5h_pct": q5,
                "quota_7d_pct": q7,
                "switched_to": None,
                "message": (
                    f"ALERT: quota {q5}% (5h) — no HEALTHY account to rotate to "
                    "(other accounts absent or credential-expired); staying put. "
                    "Refresh a stored account with `claude /login` + "
                    "`sac accounts sync-live`."
                ),
            }

        if dry_run:
            return {
                "action": "rotated(dry_run)",
                "quota_5h_pct": q5,
                "quota_7d_pct": q7,
                "switched_to": next_acct.get("name"),
                "message": "dry-run: would rotate",
            }

        result = switch_account(
            next_acct["name"],
            store_dir=store_dir,
            home=home,
            event="auto-rotate",
            reason=f"quota threshold {threshold:.0f}% hit (5h={q5}% 7d={q7}%)",
            from_account=current_email,
        )
        return {
            "action": "rotated",
            "quota_5h_pct": q5,
            "quota_7d_pct": q7,
            "switched_to": next_acct.get("name"),
            "message": (
                f"Rotated from {current_email} to {next_acct.get('name')} "
                f"(quota was {q5}% 5h)"
            ),
        }

    # stx-allow: fallback (reason: catch-all safety net — see inline comment for context)
    except Exception as exc:
        return {
            "action": "error",
            "quota_5h_pct": None,
            "quota_7d_pct": None,
            "switched_to": None,
            "message": f"unexpected error: {exc}",
        }


def survival_mode_check(
    store_dir: Path | None = None,
    home: Path | None = None,
    warn_threshold: float = SURVIVAL_THRESHOLD,
) -> dict:
    """Check whether the fleet is in single-account survival mode.

    Single-account survival mode: only 1 account available in the store, and
    the current 5h quota usage has exceeded *warn_threshold*.  In this state
    agents should suppress non-critical fleet chatter to preserve quota for
    critical work.

    Returns:
        Dict with keys:
            survival_mode: bool — True when single-account AND quota > warn_threshold
            account_count:  int
            quota_5h_pct:   float | None
            message:        str
    """
    try:
        _home = home or Path.home()
        accounts = list_accounts(store_dir=store_dir, home=_home)
        count = len(accounts)
        usage = fetch_usage(home=_home)
        q5 = usage.get("used_pct_5h")

        if count <= 1 and q5 is not None and q5 >= warn_threshold:
            msg = (
                f"SURVIVAL MODE: only {count} account(s) available, "
                f"5h quota at {q5}%. Suppress non-critical agent chatter."
            )
            return {
                "survival_mode": True,
                "account_count": count,
                "quota_5h_pct": q5,
                "message": msg,
            }

        return {
            "survival_mode": False,
            "account_count": count,
            "quota_5h_pct": q5,
            "message": (f"ok: {count} account(s), 5h quota={q5}%"),
        }
    except Exception as exc:
        return {
            "survival_mode": False,
            "account_count": 0,
            "quota_5h_pct": None,
            "message": f"survival_mode_check error: {exc}",
        }


def _daemonize(log_path: Path) -> None:
    """Fork the process into a background daemon (UNIX double-fork)."""
    log_path.parent.mkdir(parents=True, exist_ok=True)

    # First fork
    pid = os.fork()
    if pid > 0:
        sys.exit(0)  # exit parent

    os.setsid()

    # Second fork (prevent reacquiring controlling terminal)
    pid = os.fork()
    if pid > 0:
        sys.exit(0)

    # Redirect stdio to log file
    log_fd = open(log_path, "a", encoding="utf-8")
    sys.stdout.flush()
    sys.stderr.flush()
    os.dup2(log_fd.fileno(), sys.stdout.fileno())
    os.dup2(log_fd.fileno(), sys.stderr.fileno())
    devnull = open(os.devnull, "r")
    os.dup2(devnull.fileno(), sys.stdin.fileno())


def run_loop(
    threshold: float = DEFAULT_THRESHOLD,
    interval: int = DEFAULT_INTERVAL,
    store_dir: Path | None = None,
    home: Path | None = None,
    daemon: bool = False,
    log_path: Path | None = None,
) -> None:
    """Run indefinitely, checking quota every interval seconds.

    Args:
        threshold: Rotate when usage exceeds this %.
        interval:  Seconds between checks.
        store_dir: Override for account store directory.
        home:      Override for home directory.
        daemon:    If True, double-fork into background before looping.
        log_path:  Log file path used when daemon=True (default: ~/.scitex/agent-container/logs/quota-watch.log).
    """
    if daemon:
        _log = log_path or _DEFAULT_LOG_PATH
        _daemonize(_log)
        # After fork we continue here in the daemon child
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s"
    )
    logger.info(
        "quota-watch started: threshold=%.0f%% interval=%ds", threshold, interval
    )
    while True:
        result = check_and_rotate(threshold=threshold, store_dir=store_dir, home=home)
        action = result["action"]
        msg = result["message"]
        if action in ("rotated", "no_accounts", "error"):
            logger.warning("[%s] %s", action.upper(), msg)
        else:
            logger.info("[%s] %s", action, msg)

        # Survival mode check — emit a warning if needed
        sv = survival_mode_check(store_dir=store_dir, home=home)
        if sv["survival_mode"]:
            logger.warning("[SURVIVAL] %s", sv["message"])

        time.sleep(interval)
