"""Configured local account inventory and hard-bounded metadata fanout.

Credential values stay in RAM and owned child processes, never output/cache/argv.
Four seconds bounds the entire collection, including all per-account endpoints.
At most four owned workers run concurrently; missed rows remain represented.
"""

from __future__ import annotations

import json
import multiprocessing
import os
import re
import tempfile
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from .provider_usage_http import fetch
from .provider_usage_projection import safe_snapshot

_ALIASES = re.compile(r"^(COMMANDCODE_API_KEY(?:_[A-Za-z0-9]+)?|OPENCODE_GO_API_KEY(?:_[A-Za-z0-9]+)?)$")


@dataclass
class UsageTarget:
    provider: str
    name: str
    aliases: list[str]
    secret: str = field(default="", repr=False)
    auth_path: Path | None = field(default=None, repr=False)
    account_type: str = "api"


def discover(openai_accounts, *, env=None, home=None):
    """Discover names from configured env aliases and the existing Codex store."""
    environment = os.environ if env is None else env
    targets = []
    for alias in sorted(name for name in environment if _ALIASES.fullmatch(name)):
        value = environment.get(alias)
        if not isinstance(value, str) or not value.strip():
            continue
        provider = "commandcode" if alias.startswith("COMMANDCODE") else "opencode-go"
        existing = next((row for row in targets if row.provider == provider and row.secret == value), None)
        if existing is not None:
            existing.aliases.append(alias)
        else:
            targets.append(UsageTarget(provider, alias, [alias], secret=value))
    if openai_accounts:
        from ..cli_pkg._account_list_build import openai_account_name
        from .codex_account import CodexAccountSyncError, _gateway_auth_paths

        try:
            paths = _gateway_auth_paths(home)
        except (OSError, CodexAccountSyncError):
            paths = [None] * len(openai_accounts)
        for meta, path in zip(openai_accounts, paths):
            name = openai_account_name(meta)
            targets.append(UsageTarget(
                "openai", name, [name], auth_path=path,
                account_type="subscription" if meta.get("auth_mode") == "chatgpt" else "api",
            ))
    return targets


def _cache_path(home, target):
    return home / ".scitex" / "cache" / "account-usage" / (target.provider + "-" + target.name + ".json")


def _cached(path, now):
    try:
        return safe_snapshot(json.loads(path.read_text()), now)
    except (OSError, ValueError, TypeError):
        return None


def _save(path, snapshot):
    """Store only normalized metrics atomically; cache failure is row-local."""
    temporary = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        descriptor, temporary = tempfile.mkstemp(prefix=".usage-", dir=path.parent)
        with os.fdopen(descriptor, "w") as stream:
            json.dump(snapshot, stream)
        os.replace(temporary, path)
    except OSError:
        if temporary is not None:
            Path(temporary).unlink(missing_ok=True)


def _worker(send, target, deadline, fetcher):
    try:
        send.send(fetcher(target, deadline))
    # stx-allow: fallback (reason: untrusted provider failure must only affect its row; never stringify private exception data)
    except Exception:
        send.send({"error": "metadata-worker-failed"})
    finally:
        send.close()


def _row(target, snapshot=None, error=None):
    return {
        "provider": target.provider, "name": target.name,
        "qualified_id": target.provider + ":" + target.name,
        "aliases": list(target.aliases),
        "account_type": target.account_type,
        "selection": {"provider": target.provider, "account": target.provider + ":" + target.name},
        "capabilities": {"explicit_selection": True, "automatic_rotation": "not-implemented"},
        "discovery_source": "configured-env-alias" if target.secret else "configured-codex-auth",
        "metering": "subscription-quota" if target.account_type == "subscription" else "provider-api",
        "usage_state": "unknown", "fetchedAt": None, "windows": [],
        "balances": {}, "currency": None, "cost": None, "subscription_fee": None,
        **(snapshot or {}), "error": error,
    }


def collect(targets, *, home=None, passive=False, refresh=False, budget=4.0,
            fetcher=fetch, now=None):
    """Keep each account visible, cache-only when passive, hard-stop owned probes."""
    root = Path(home) if home is not None else Path.home()
    observed = now or datetime.now(timezone.utc)
    # Reserve bounded worker cleanup inside the requested overall budget.
    deadline = time.monotonic() + max(0, min(4.0, budget) - 0.25)
    rows, pending = [], []
    for target in targets:
        cached = _cached(_cache_path(root, target), observed)
        rows.append(_row(target, cached, "metadata-not-observed" if cached is None else None))
        if not passive and (refresh or cached is None or cached["usage_state"] != "known"):
            pending.append((len(rows) - 1, target))
    if not pending:
        return rows
    context = multiprocessing.get_context("spawn")
    active = {}
    try:
        while pending or active:
            if time.monotonic() >= deadline:
                break
            while pending and len(active) < 4 and time.monotonic() < deadline:
                index, target = pending.pop(0)
                receive, send = context.Pipe(duplex=False)
                process = context.Process(target=_worker, args=(send, target, deadline, fetcher), daemon=True)
                try:
                    process.start()
                except (OSError, RuntimeError):
                    rows[index]["error"] = "metadata-worker-failed"
                    receive.close()
                    send.close()
                    continue
                send.close()
                active[index] = (process, receive, target)
            for index, (process, receive, target) in list(active.items()):
                if receive.poll():
                    try:
                        result = receive.recv()
                    except (EOFError, OSError):
                        result = {"error": "metadata-worker-failed"}
                    safe = safe_snapshot(result, now or datetime.now(timezone.utc))
                    error = result.get("error") if isinstance(result, dict) else None
                    if error not in {None, "metadata-worker-failed", "metadata-deadline",
                                     "metadata-unavailable", "authentication-refused",
                                     "subscription-auth-unavailable", "subscription-auth-unreadable",
                                     "credential-expired-refresh-not-performed"} and not (
                        isinstance(error, str) and re.fullmatch(r"http-[0-9]{3}", error)
                    ):
                        error = "metadata-unavailable"
                    if safe is not None and safe["usage_state"] == "known":
                        rows[index] = _row(target, safe, error)
                        _save(_cache_path(root, target), safe)
                    else:
                        if safe is not None and rows[index]["fetchedAt"] is None:
                            rows[index] = _row(target, safe, error or "metadata-not-observed")
                        rows[index]["error"] = error or "metadata-not-observed"
                        if rows[index]["usage_state"] == "known":
                            rows[index]["usage_state"] = "stale"
                            for item in rows[index]["windows"]:
                                if item["state"] == "known":
                                    item["state"] = "stale"
                    receive.close()
                    process.join(timeout=0.02)
                    if process.is_alive():
                        process.terminate()
                        process.join(timeout=0.05)
                    del active[index]
                elif not process.is_alive():
                    rows[index]["error"] = "metadata-worker-failed"
                    receive.close()
                    process.join(timeout=0.02)
                    del active[index]
            if time.monotonic() >= deadline:
                break
            if active:
                time.sleep(0.01)
    finally:
        for index, (process, receive, _) in active.items():
            rows[index]["error"] = "metadata-deadline"
            if rows[index]["fetchedAt"] is not None:
                rows[index]["usage_state"] = "stale"
            receive.close()
            if process.is_alive():
                process.terminate()
            process.join(timeout=0.02)
            if process.is_alive():
                process.kill()
                process.join(timeout=0.02)
        for index, _ in pending:
            rows[index]["error"] = "metadata-deadline"
    return rows
