"""Single-flight boot-window lock for the shared OAuth credential file.

Card sac-multi-start-queue-oauth (2026-06-25). Every agent binds the same
``~/.claude/.credentials.json`` ``:rw``; the in-container ``claude`` refreshes
the rotating OAuth token. When many agents boot at once they race that refresh
and clobber each other's freshly-rotated (single-use) token -> the loser gets a
401 -> mass re-login.

sac never holds the OAuth token material (it only reads safe metadata such as
``expiresAt`` — see ``_account/credentials.py``). It can therefore only
*serialize access*, not perform the refresh. This module serializes the
credential-sensitive BOOT window across processes with a BLOCKING
``fcntl.flock(LOCK_EX)`` so only one agent boots through its initial OAuth
refresh at a time.

Subtlety: a BACKGROUND ``sac agents start`` returns as soon as the container is
kicked off — BEFORE the in-container ``claude`` has refreshed. So holding the
lock only for the launch subprocess would not actually cover the refresh. The
gate therefore holds the lock through a bounded *settle window* after the
launch, releasing the next waiter as soon as the shared file's ``expiresAt``
changes (refresh observed) or the timeout elapses. The settle wait is engaged
only when a refresh is actually imminent (token at/near expiry); a healthy
far-from-expiry token boots through with just the brief flock.

Mirrors the proven flock pattern in ``_single_instance.py`` but BLOCKING (we
want to wait our turn, not fail fast) and not port-scoped (one credential file
for the whole fleet on a host). NOT covered here: the ~1h-later in-flight
rotation of already-running agents — that needs per-agent snapshots or a
long-lived broker and is tracked separately.
"""

from __future__ import annotations

import asyncio
import fcntl
import os
import subprocess
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

_LOCK_NAME = "creds-refresh.lock"
# Hold the lock at most this long after a launch while waiting for the
# holder's OAuth refresh to land. Operator-overridable via
# SAC_CREDS_SETTLE_SECONDS; 0 disables the gate entirely (no serialization).
_DEFAULT_SETTLE_SECONDS = 20.0
# Only engage the settle wait when the token expires within this window — a
# far-from-expiry token will not refresh at boot, so there is nothing to race.
_DEFAULT_IMMINENT_WINDOW_SECONDS = 300.0
_POLL_INTERVAL_SECONDS = 0.5

__all__ = [
    "credential_lock_path",
    "credential_boot_gate",
    "gate_settings_from_env",
    "run_brokered_launch",
]


def credential_lock_path(lock_dir: Path) -> Path:
    """Return the fleet-wide credential-refresh lock path under ``lock_dir``."""
    return lock_dir / _LOCK_NAME


def _read_expires_at_ms() -> int | None:
    """Best-effort read of ``claudeAiOauth.expiresAt`` (unix-ms) from the
    shared credential file. Returns ``None`` when unreadable — the caller
    treats unknown as "be safe, serialize". Never reads token material."""
    try:
        from .._account.credentials import read_credentials_metadata

        meta = read_credentials_metadata()
        value = meta.get("expiresAt")
        return int(value) if value is not None else None
    except Exception:  # stx-allow: fallback (reason: metadata read is advisory; unknown -> serialize)
        return None


def _refresh_imminent(expires_at_ms: int | None, now_ms: float, window_ms: float) -> bool:
    """True iff a boot-time refresh is plausible (token at/near expiry).

    Unknown expiry returns False: if we cannot read ``expiresAt`` there is
    either no OAuth token to rotate (api-key / no-creds setups) or no way to
    observe a refresh landing, so settling would only delay without
    coordinating. The flock still serializes the brief launch window in that
    case; the settle wait engages only when we can actually watch the rotation."""
    if expires_at_ms is None:
        return False
    return (expires_at_ms - now_ms) <= window_ms


def gate_settings_from_env() -> tuple[Path, float, float]:
    """Resolve ``(lock_dir, settle_seconds, imminent_window_seconds)`` from env.

    ``SAC_CREDS_SETTLE_SECONDS`` overrides the settle cap (0 disables the gate).
    ``SAC_CREDS_REFRESH_IMMINENT_SECONDS`` overrides the imminence window. The
    lock dir mirrors the listen single-instance lock dir."""
    from ._single_instance import default_lock_dir

    settle = _env_float("SAC_CREDS_SETTLE_SECONDS", _DEFAULT_SETTLE_SECONDS)
    window = _env_float(
        "SAC_CREDS_REFRESH_IMMINENT_SECONDS", _DEFAULT_IMMINENT_WINDOW_SECONDS
    )
    return default_lock_dir(), settle, window


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return float(raw)
    except ValueError:  # stx-allow: fallback (reason: a malformed knob must not crash the launch path; fall back to the default)
        return default


@contextmanager
def credential_boot_gate(
    *,
    lock_dir: Path,
    settle_seconds: float = _DEFAULT_SETTLE_SECONDS,
    imminent_window_seconds: float = _DEFAULT_IMMINENT_WINDOW_SECONDS,
) -> Iterator[None]:
    """Serialize the credential-sensitive boot window across processes.

    Acquires a BLOCKING exclusive flock on ``<lock_dir>/creds-refresh.lock``,
    runs the wrapped body (the launch), then — only when a refresh was
    imminent at acquire time — holds the lock until the shared credential
    file's ``expiresAt`` changes (refresh landed) or ``settle_seconds``
    elapses, before releasing the next waiter. ``settle_seconds <= 0`` makes
    the whole gate a no-op (no flock, no wait)."""
    if settle_seconds <= 0:
        yield
        return

    lock_dir.mkdir(parents=True, exist_ok=True)
    path = credential_lock_path(lock_dir)
    fd = os.open(str(path), os.O_CREAT | os.O_RDWR, 0o644)
    fcntl.flock(fd, fcntl.LOCK_EX)  # BLOCKING — wait our turn through the window
    baseline_expires = _read_expires_at_ms()
    imminent = _refresh_imminent(
        baseline_expires, time.time() * 1000.0, imminent_window_seconds * 1000.0
    )
    try:
        os.ftruncate(fd, 0)
        os.write(fd, f"{os.getpid()}\n".encode("utf-8"))
        yield
    finally:
        if imminent:
            _wait_for_refresh(baseline_expires, settle_seconds)
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        except OSError:  # stx-allow: fallback (reason: best-effort; kernel releases on close)
            pass
        try:
            os.close(fd)
        except OSError:  # stx-allow: fallback (reason: fd already invalid — non-fatal)
            pass


def _wait_for_refresh(baseline_expires_ms: int | None, settle_seconds: float) -> None:
    """Hold until the shared credential ``expiresAt`` changes or timeout.

    A changed ``expiresAt`` means the holder's in-container ``claude`` rotated
    the token, so the next waiter will read the fresh value instead of racing
    it. Polls cheaply; the cap bounds the worst case (e.g. the token was not
    actually near expiry, or refresh failed)."""
    deadline = time.monotonic() + settle_seconds
    while time.monotonic() < deadline:
        current = _read_expires_at_ms()
        if (
            current is not None
            and baseline_expires_ms is not None
            and current != baseline_expires_ms
        ):
            return
        time.sleep(_POLL_INTERVAL_SECONDS)


def _run_locked(
    inner_argv: list[str],
    child_env: dict[str, str],
) -> "subprocess.CompletedProcess[str]":
    lock_dir, settle, window = gate_settings_from_env()
    with credential_boot_gate(
        lock_dir=lock_dir,
        settle_seconds=settle,
        imminent_window_seconds=window,
    ):
        return subprocess.run(
            inner_argv,
            capture_output=True,
            text=True,
            check=False,
            env=child_env,
        )


async def run_brokered_launch(
    inner_argv: list[str],
    child_env: dict[str, str],
    *,
    foreground: bool,
    one_shot: bool,
) -> "subprocess.CompletedProcess[str]":
    """Run the brokered ``sac agents start`` subprocess off the event loop.

    Background launches go through :func:`credential_boot_gate` so concurrent
    brokered spawns serialize through the OAuth-refresh boot window. Foreground
    / one-shot launches BYPASS the gate: they are single, interactive, and
    block for the whole session — holding a fleet-wide lock that long would
    serialize everything for no safety gain (the mass-launch race is the
    background path)."""
    if foreground or one_shot:
        return await asyncio.to_thread(
            subprocess.run,
            inner_argv,
            capture_output=True,
            text=True,
            check=False,
            env=child_env,
        )
    return await asyncio.to_thread(_run_locked, inner_argv, child_env)
