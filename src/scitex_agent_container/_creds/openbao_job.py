"""Host-side lifecycle for a single project's OpenBao database role.

Construct one broker per trusted project assignment. Keep its state directory
on a private host filesystem outside every job mount. Only the issued DB login
crosses the worker boundary. The database role must enforce the maximum TTL.
"""

from __future__ import annotations

import fcntl
import json
import os
import re
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass, field
from http.client import HTTPConnection, HTTPSConnection
from pathlib import Path
from urllib.parse import urlsplit
from uuid import UUID


@dataclass(frozen=True)
class DatabaseLogin:
    username: str = field(repr=False)
    password: str = field(repr=False)


class JobDatabaseBroker:
    """Issue once per host-assigned incarnation and revoke by its exact lease."""

    def __init__(self, address: str, token: str, role: str, state: Path):
        endpoint = urlsplit(address)
        if endpoint.scheme == "https":
            transport = HTTPSConnection
        elif endpoint.scheme == "http" and endpoint.hostname in {"127.0.0.1", "::1"}:
            transport = HTTPConnection
        else:
            raise ValueError("OpenBao requires HTTPS or a loopback test endpoint")
        if not endpoint.hostname or endpoint.username or endpoint.password or (
            endpoint.path.strip("/") or endpoint.query or endpoint.fragment
        ):
            raise ValueError("Expected a bare OpenBao origin")
        if not token or not re.fullmatch(r"[A-Za-z0-9_-]+", role):
            raise ValueError("A broker token and fixed database role are required")
        state.mkdir(mode=0o700, parents=True, exist_ok=True)
        info = state.lstat()
        if state.is_symlink() or not state.is_dir() or info.st_uid != os.geteuid() or info.st_mode & 0o077:
            raise PermissionError("Broker state requires a private host-owned directory")
        self._connection = transport
        self._endpoint = endpoint
        self._token = token
        self._state = state
        self._role = role

    def _request(self, method: str, path: str, body: dict | None = None) -> dict:
        conn = self._connection(self._endpoint.hostname, self._endpoint.port, timeout=8)
        try:
            conn.request(method, "/v1/" + path, json.dumps(body) if body else None,
                         {"X-Vault-Token": self._token, "Content-Type": "application/json"})
            response = conn.getresponse()
            if response.status not in {200, 204}:
                raise RuntimeError(f"OpenBao {method} failed (HTTP {response.status})")
            return json.loads(response.read() or b"{}")
        finally:
            conn.close()

    @contextmanager
    def _locked(self, incarnation_id: UUID):
        key = str(UUID(str(incarnation_id)))
        with (self._state / (key + ".lock")).open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            yield self._state / (key + ".json")

    @staticmethod
    def _save(path: Path, status: str, lease: str = "") -> None:
        with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, delete=False) as out:
            json.dump({"status": status, "lease": lease}, out)
            out.flush()
            os.fsync(out.fileno())
        os.replace(out.name, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)

    def issue(self, incarnation_id: UUID) -> DatabaseLogin:
        """Reserve before contacting OpenBao. Never retry an ambiguous issuance."""
        with self._locked(incarnation_id) as path:
            if path.exists():
                raise RuntimeError("Incarnation already registered or stopped")
            self._save(path, "issuing")
            prefix = "database/creds/" + self._role
            reply = self._request("GET", prefix)
            lease = reply["lease_id"]
            if not re.fullmatch(re.escape(prefix) + r"/[A-Za-z0-9_-]+", lease):
                raise RuntimeError("OpenBao returned an unexpected database lease")
            self._save(path, "issued", lease)
            try:
                if not 0 < reply["lease_duration"] <= 900:
                    raise RuntimeError("Issued lease exceeds the permitted duration")
                return DatabaseLogin(reply["data"]["username"], reply["data"]["password"])
            except (KeyError, TypeError, RuntimeError):
                self._revoke(path, lease)
                raise

    def _revoke(self, path: Path, lease: str) -> None:
        self._request("PUT", "sys/leases/revoke/" + lease, {"sync": True})
        self._save(path, "closed")

    def stop(self, incarnation_id: UUID) -> None:
        """Handle stop-before-start, duplicate stop and broker process restart."""
        with self._locked(incarnation_id) as path:
            if path.exists():
                record = json.loads(path.read_text())
                if record["status"] == "closed":
                    return
                if record["status"] == "issuing":
                    raise RuntimeError("Issuance unresolved; reconcile at issuer or await role TTL")
                if record["status"] != "issued" or not record["lease"]:
                    raise RuntimeError("Unexpected broker state; manual reconciliation required")
                self._revoke(path, record["lease"])
                return
            self._save(path, "closed")

    @contextmanager
    def job(self, incarnation_id: UUID):
        """Wrap execution in the trusted host supervisor, including error cleanup."""
        login = self.issue(incarnation_id)
        try:
            yield login
        finally:
            self.stop(incarnation_id)
