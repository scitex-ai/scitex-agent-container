"""Automatic host gateway bootstrap for ``spec.claude.provider: codex``.

The Codex backend is a shared, host-local service.  Keeping its random
local-hop key and process lifecycle here lets ``sac agents start`` remain the
single operator command while preserving the gateway's authenticated surface.
"""

from __future__ import annotations

import json
import os
import secrets
import shutil
import subprocess
import time
import urllib.error
import urllib.request
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from scitex_config import PriorityConfig, load_dotenv

from ..config import AgentConfig

_AUTH_ENV = "SCITEX_GENAI_GATEWAY_API_KEY"
_DEFAULT_BASE_URL = "http://127.0.0.1:18765"


class CodexGatewayError(RuntimeError):
    """Raised when the local Codex gateway cannot be made ready safely."""


def _is_codex_backend(config: AgentConfig) -> bool:
    provider = getattr(getattr(config, "claude", None), "provider", None)
    return bool(
        provider is not None
        and getattr(provider, "auth_token_env", "") == _AUTH_ENV
        and getattr(provider, "base_url", "") == _DEFAULT_BASE_URL
    )


def _runtime_dir() -> Path:
    return Path.home() / ".scitex" / "agent-container" / "runtime" / "codex-gateway"


@contextmanager
def _launch_lock(runtime_dir: Path) -> Iterator[None]:
    """Serialize concurrent agent starts so only one gateway is spawned."""
    import fcntl

    runtime_dir.mkdir(parents=True, exist_ok=True)
    lock_path = runtime_dir / "launch.lock"
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        with os.fdopen(fd, "a+") as lock_file:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            yield
    finally:
        # fd is owned and closed by fdopen, including exceptional exits.
        pass


def _read_persisted_key(path: Path) -> str:
    try:
        value = path.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return ""
    except OSError as exc:
        raise CodexGatewayError(f"Cannot read Codex gateway key: {path}") from exc
    if value:
        try:
            path.chmod(0o600)
        except OSError:
            pass
    return value


def _write_private(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    fd = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(value)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        path.chmod(0o600)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _configured_key() -> str:
    load_dotenv(dotenv_path=str(Path.home() / ".env"))
    return str(
        PriorityConfig(auto_uppercase=False).resolve(key=_AUTH_ENV, default="") or ""
    ).strip()


def _health(base_url: str) -> bool:
    try:
        with urllib.request.urlopen(f"{base_url}/health", timeout=0.5) as response:
            payload = json.loads(response.read())
    except (OSError, ValueError, urllib.error.URLError):
        return False
    return bool(
        response.status == 200
        and payload.get("status") == "ok"
        and payload.get("provider") == "openai-codex"
    )


def _accepts_key(base_url: str, key: str) -> bool:
    request = urllib.request.Request(
        f"{base_url}/v1/messages/count_tokens",
        data=b"{}",
        headers={"Content-Type": "application/json", "x-api-key": key},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=1.0) as response:
            return response.status == 200
    except (OSError, urllib.error.URLError):
        return False


def _tail(path: Path, lines: int = 12) -> str:
    try:
        return "\n".join(
            path.read_text(encoding="utf-8", errors="replace").splitlines()[-lines:]
        )
    except OSError:
        return ""


def ensure_codex_gateway(config: AgentConfig, *, wait_seconds: float = 8.0) -> None:
    """Ensure the registered Codex gateway is authenticated and ready.

    Non-Codex configs are untouched.  The generated key is a host-local
    transport secret stored mode 0600 beneath SAC's runtime tree; OAuth
    credentials remain owned by Codex/scitex-genai and are never copied here.
    """
    if not _is_codex_backend(config):
        return

    provider = config.claude.provider
    base_url = str(provider.base_url).rstrip("/")
    runtime_dir = _runtime_dir()
    key_path = runtime_dir / "api-key"
    log_path = runtime_dir / "gateway.log"
    pid_path = runtime_dir / "gateway.pid"

    with _launch_lock(runtime_dir):
        key = _configured_key() or _read_persisted_key(key_path)
        if _health(base_url):
            if not key:
                raise CodexGatewayError(
                    f"A Codex gateway is already running at {base_url}, but SAC "
                    f"cannot recover its local-hop key. Set {_AUTH_ENV} in the "
                    "launch shell or $HOME/.env, or stop that gateway and retry."
                )
            if not _accepts_key(base_url, key):
                raise CodexGatewayError(
                    f"The Codex gateway at {base_url} rejects SAC's configured "
                    f"{_AUTH_ENV}. Use the key that started the gateway, or stop "
                    "that gateway and retry so SAC can bootstrap it."
                )
            # A shell-only key must survive into later ``sac agents start``
            # invocations while this shared gateway keeps running.
            _write_private(key_path, key)
            os.environ[_AUTH_ENV] = key
            return

        if not key:
            key = secrets.token_hex(32)
        # Persist configured keys too: the detached gateway outlives the
        # launch shell that may have supplied the environment variable.
        _write_private(key_path, key)
        os.environ[_AUTH_ENV] = key

        executable = shutil.which("scitex-genai-gateway")
        if not executable:
            raise CodexGatewayError(
                "Cannot auto-start the Codex gateway: scitex-genai-gateway is "
                "not installed. Install scitex-agent-container[codex]."
            )

        env = os.environ.copy()
        env[_AUTH_ENV] = key
        log_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with log_path.open("wb") as log_file:
                process = subprocess.Popen(
                    [
                        executable,
                        "--host",
                        "127.0.0.1",
                        "--port",
                        "18765",
                        "--log-level",
                        "warning",
                    ],
                    stdin=subprocess.DEVNULL,
                    stdout=log_file,
                    stderr=subprocess.STDOUT,
                    env=env,
                    start_new_session=True,
                    close_fds=True,
                )
        except OSError as exc:
            raise CodexGatewayError(
                f"Cannot start Codex gateway executable {executable}: {exc}"
            ) from exc
        _write_private(pid_path, str(process.pid))

        deadline = time.monotonic() + wait_seconds
        while time.monotonic() < deadline:
            if _health(base_url) and _accepts_key(base_url, key):
                return
            if process.poll() is not None:
                break
            time.sleep(0.1)

        detail = _tail(log_path)
        suffix = f"\nGateway log:\n{detail}" if detail else ""
        raise CodexGatewayError(
            f"Codex gateway did not become ready at {base_url}. Confirm that "
            "`sac accounts sync-openai` has a usable Codex login and that port "
            f"18765 is free.{suffix}"
        )


__all__ = ["CodexGatewayError", "ensure_codex_gateway"]
