"""Read non-secret account metadata from the local Codex login.

Codex stores its login at ``$CODEX_HOME/auth.json`` (default:
``~/.codex/auth.json``).  ChatGPT logins carry display metadata in JWT
claims; this module decodes those claims without verifying them because they
are used only for a local status display, never for authorization decisions.

The returned mapping is an explicit allowlist. Tokens and API keys cannot
leave this module, even when the auth file gains new fields.
"""

from __future__ import annotations

import base64
import json
import os
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_AUTH_CLAIM = "https://api.openai.com/auth"
_PROFILE_CLAIM = "https://api.openai.com/profile"
_ACCOUNT_STORE = Path(".scitex") / "agent-container" / "accounts" / "openai"
_SAFE_ALIAS = re.compile(r"^[a-z0-9][a-z0-9-]*$")


class CodexAccountSyncError(RuntimeError):
    """The active Codex credential could not be safely collected."""


def _auth_path(home: Path | None = None) -> Path:
    codex_home = os.environ.get("CODEX_HOME", "").strip()
    if codex_home:
        return Path(codex_home).expanduser() / "auth.json"
    root = Path(home) if home is not None else Path.home()
    return root / ".codex" / "auth.json"


def _gateway_auth_paths(home: Path | None = None) -> list[Path]:
    """Return the Codex auth files used by the local scitex-genai gateway."""
    configured = os.environ.get("SCITEX_GENAI_CODEX_HOMES", "").strip()
    if not configured:
        root = _openai_store_root(home)
        if root.exists():
            stored = sorted(root.glob("*/auth.json")) if root.is_dir() else []
            if not stored:
                raise CodexAccountSyncError(
                    f"OpenAI account store contains no auth files: {root}"
                )
            return stored
        return [_auth_path(home)]

    paths: list[Path] = []
    seen: set[Path] = set()
    for value in configured.split(os.pathsep):
        if not value.strip():
            continue
        candidate = Path(value).expanduser()
        path = candidate if candidate.name == "auth.json" else candidate / "auth.json"
        normalized = path.resolve(strict=False)
        if normalized not in seen:
            seen.add(normalized)
            paths.append(path)
    return paths


def _openai_store_root(home: Path | None = None) -> Path:
    root = Path(home) if home is not None else Path.home()
    return root / _ACCOUNT_STORE


def _account_alias(meta: dict[str, Any], requested: str | None) -> str:
    if requested is not None:
        alias = requested.strip().lower()
    else:
        email = meta.get("email_address")
        if not isinstance(email, str) or not email.strip():
            raise CodexAccountSyncError(
                "Codex account has no display email; pass an explicit account name"
            )
        alias = email.strip().lower().replace("@", "-").replace(".", "-")
    if not _SAFE_ALIAS.fullmatch(alias):
        raise CodexAccountSyncError(
            "Codex account name must contain only lowercase letters, digits, and hyphens"
        )
    return alias


def _atomic_write(path: Path, content: bytes, mode: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.parent.chmod(0o700)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def _refresh_time(meta: dict[str, Any]) -> float | None:
    value = meta.get("last_refresh")
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


def _assert_safe_update(
    destination: Path, source_meta: dict[str, Any], source_raw: bytes
) -> bool:
    """Return whether a stored auth file should be replaced, or raise."""
    if not destination.exists():
        return True
    stored_raw = destination.read_bytes()
    if stored_raw == source_raw:
        return False
    stored_meta = _read_metadata_at(destination)
    if not _has_metadata(stored_meta):
        raise CodexAccountSyncError(
            f"Stored Codex auth file is malformed; refusing overwrite: {destination}"
        )
    source_id = source_meta.get("account_id")
    stored_id = stored_meta.get("account_id")
    if source_id and stored_id and source_id != stored_id:
        raise CodexAccountSyncError(
            f"Stored Codex account identity differs; refusing overwrite: {destination}"
        )
    source_refresh = _refresh_time(source_meta)
    stored_refresh = _refresh_time(stored_meta)
    if source_refresh is None or stored_refresh is None:
        raise CodexAccountSyncError(
            "Cannot compare Codex credential freshness; refusing overwrite"
        )
    return source_refresh > stored_refresh


def sync_codex_account(home: Path | None = None, *, name: str | None = None) -> Path:
    """Collect the active Codex login into the provider-qualified SAC store.

    The credential is written to ``accounts/openai/<slug>/auth.json`` and
    remains secret. The adjacent ``account.json`` contains only the same
    allowlisted display metadata returned by this module.
    """
    source = _auth_path(home)
    try:
        raw = source.read_bytes()
        parsed = json.loads(raw)
    except OSError as exc:
        raise CodexAccountSyncError(
            f"Cannot read active Codex auth file: {source}"
        ) from exc
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CodexAccountSyncError(
            f"Active Codex auth file is invalid: {source}"
        ) from exc
    if not isinstance(parsed, dict):
        raise CodexAccountSyncError(
            f"Active Codex auth file is not an object: {source}"
        )

    meta = _read_metadata_at(source)
    if not _has_metadata(meta):
        raise CodexAccountSyncError(
            f"Active Codex auth file has no usable metadata: {source}"
        )
    alias = _account_alias(meta, name)
    account_home = _openai_store_root(home) / alias
    auth_path = account_home / "auth.json"
    should_update = _assert_safe_update(auth_path, meta, raw)
    if should_update:
        _atomic_write(auth_path, raw, 0o600)
    else:
        meta = _read_metadata_at(auth_path)
    safe_meta = {
        **meta,
        "provider": "openai",
        "name": alias,
        "qualified_id": f"openai:{alias}",
    }
    _atomic_write(
        account_home / "account.json",
        (json.dumps(safe_meta, indent=2) + "\n").encode(),
        0o600,
    )
    return auth_path


def _jwt_claims(token: object) -> dict[str, Any]:
    """Decode a JWT payload for display metadata; return empty on failure."""
    if not isinstance(token, str) or token.count(".") < 2:
        return {}
    payload = token.split(".", 2)[1]
    payload += "=" * (-len(payload) % 4)
    try:
        decoded = base64.urlsafe_b64decode(payload.encode("ascii"))
        claims = json.loads(decoded)
    except (ValueError, UnicodeError, json.JSONDecodeError):
        return {}
    return claims if isinstance(claims, dict) else {}


def _mapping(value: object) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _has_metadata(meta: dict[str, Any]) -> bool:
    return any(value is not None for value in meta.values())


def _organization(auth_claims: dict[str, Any]) -> tuple[str | None, str | None]:
    organizations = auth_claims.get("organizations")
    if not isinstance(organizations, list):
        return None, None
    candidates = [item for item in organizations if isinstance(item, dict)]
    if not candidates:
        return None, None
    selected = next(
        (item for item in candidates if item.get("is_default")), candidates[0]
    )
    title = selected.get("title")
    role = selected.get("role")
    return (
        title if isinstance(title, str) else None,
        role if isinstance(role, str) else None,
    )


def _read_metadata_at(path: Path) -> dict[str, Any]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError):
        return {}
    if not isinstance(raw, dict):
        return {}

    tokens = _mapping(raw.get("tokens"))
    id_claims = _jwt_claims(tokens.get("id_token"))
    access_claims = _jwt_claims(tokens.get("access_token"))
    id_auth = _mapping(id_claims.get(_AUTH_CLAIM))
    access_auth = _mapping(access_claims.get(_AUTH_CLAIM))
    profile = _mapping(access_claims.get(_PROFILE_CLAIM))
    auth_claims = id_auth or access_auth
    org_name, org_role = _organization(id_auth)

    auth_mode = raw.get("auth_mode")
    last_refresh = raw.get("last_refresh")
    email = id_claims.get("email") or profile.get("email")
    display_name = id_claims.get("name") or profile.get("name")

    return {
        "auth_mode": auth_mode if isinstance(auth_mode, str) else None,
        "email_address": email if isinstance(email, str) else None,
        "display_name": display_name if isinstance(display_name, str) else None,
        "account_id": (
            auth_claims.get("chatgpt_account_id")
            if isinstance(auth_claims.get("chatgpt_account_id"), str)
            else tokens.get("account_id")
            if isinstance(tokens.get("account_id"), str)
            else None
        ),
        "plan_type": (
            auth_claims.get("chatgpt_plan_type")
            if isinstance(auth_claims.get("chatgpt_plan_type"), str)
            else None
        ),
        "organization_name": org_name,
        "organization_role": org_role,
        "subscription_active_start": (
            id_auth.get("chatgpt_subscription_active_start")
            if isinstance(id_auth.get("chatgpt_subscription_active_start"), str)
            else None
        ),
        "subscription_active_until": (
            id_auth.get("chatgpt_subscription_active_until")
            if isinstance(id_auth.get("chatgpt_subscription_active_until"), str)
            else None
        ),
        "last_refresh": last_refresh if isinstance(last_refresh, str) else None,
    }


def read_codex_account_metadata(home: Path | None = None) -> dict[str, Any]:
    """Return display-safe metadata for the active Codex/OpenAI login.

    Missing or malformed files return ``{}``. A readable auth file returns
    only allowlisted scalar account fields; secret-bearing source fields are
    deliberately never copied into the result.
    """
    return _read_metadata_at(_auth_path(home))


def read_codex_accounts_metadata(home: Path | None = None) -> list[dict[str, Any]]:
    """Return display-safe metadata for all gateway-configured Codex logins."""
    configured = bool(os.environ.get("SCITEX_GENAI_CODEX_HOMES", "").strip())
    stored = _openai_store_root(home).exists()
    accounts: list[dict[str, Any]] = []
    for path in _gateway_auth_paths(home):
        meta = _read_metadata_at(path)
        if not _has_metadata(meta):
            if configured or stored:
                raise CodexAccountSyncError(
                    f"Configured Codex auth file is missing or malformed: {path}"
                )
            continue
        alias = path.parent.name
        if alias != ".codex":
            meta["gateway_alias"] = alias
        accounts.append(meta)
    return accounts


__all__ = [
    "CodexAccountSyncError",
    "read_codex_account_metadata",
    "read_codex_accounts_metadata",
    "sync_codex_account",
]
