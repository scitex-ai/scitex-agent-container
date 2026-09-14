"""Synchronize one exact Cards store credential to selected fleet hosts.

The password travels only on an SSH stdin pipe.  It is never placed in argv,
JSON output, logs, or a temporary file.  The receiver changes only the exact
``(host, port, database, role)`` row and atomically preserves every unrelated
passfile row.
"""

from __future__ import annotations

import getpass
import json
import os
import re
import subprocess
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable

import click

from .._state.host_config import ssh_control_options
from ..runtimes._pg_identity_env import derive_pg_role
from ._agents_provision_cards_notify import (
    Endpoint,
    NotifyCredentialError,
    _assert_private_regular,
    _escape_field,
    _row,
)

_ROLE = re.compile(r"[A-Za-z0-9_.-]+\Z")
_ENDPOINT = Endpoint("scitex-primary", "55432", "scitex")


@dataclass(frozen=True)
class SyncResult:
    peer: str
    role: str
    target: str
    status: str


Runner = Callable[..., subprocess.CompletedProcess[str]]


def _source_password(path: Path, *, endpoint: Endpoint, role: str) -> str:
    """Return one unambiguous raw passfile password for an exact role."""
    _assert_private_regular(path)
    matches = []
    for line in path.read_text(encoding="utf-8").splitlines(keepends=True):
        parsed = _row(line)
        if parsed is None:
            continue
        host, port, database, user, _ = parsed.decoded
        if (
            host in {"*", endpoint.host}
            and port == endpoint.port
            and database in {"*", endpoint.database}
            and user == role
        ):
            matches.append(parsed.raw_password)
    passwords = set(matches)
    if not passwords:
        raise NotifyCredentialError(
            f"no {endpoint.host}:{endpoint.port}/{endpoint.database} credential for {role}"
        )
    if len(passwords) != 1:
        raise NotifyCredentialError(f"ambiguous source credentials for {role}")
    return passwords.pop()


def install_exact_row(
    path: Path,
    *,
    endpoint: Endpoint,
    role: str,
    raw_password: str,
    apply_changes: bool,
) -> str:
    """Install or update one exact row, preserving the rest of ``path``."""
    _assert_private_regular(path)
    lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
    key = (endpoint.host, endpoint.port, endpoint.database, role)
    exact = [
        i
        for i, line in enumerate(lines)
        if (item := _row(line)) and item.decoded[:4] == key
    ]
    rendered = ":".join(_escape_field(field) for field in key) + f":{raw_password}\n"
    if len(exact) == 1 and lines[exact[0]].rstrip("\r\n") == rendered.rstrip("\n"):
        return "ready"
    status = "planned" if not exact else "update-planned"
    if not apply_changes:
        return status

    kept = [line for i, line in enumerate(lines) if i not in exact]
    prefix = "" if not kept or kept[-1].endswith(("\n", "\r")) else "\n"
    content = "".join(kept) + prefix + rendered
    descriptor, temporary = tempfile.mkstemp(prefix=".pgpass-store-", dir=path.parent)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
    return "provisioned" if status == "planned" else "updated"


def _remote(
    peer: str,
    *,
    role: str,
    raw_password: str,
    apply_changes: bool,
    runner: Runner = subprocess.run,
) -> SyncResult:
    argv = [
        "ssh",
        *ssh_control_options(),
        "-o",
        "BatchMode=yes",
        peer,
        "sac",
        "agents",
        "receive-cards-store-credential",
        "--role",
        role,
    ]
    if apply_changes:
        argv.append("--apply")
    payload = json.dumps({"password": raw_password})
    completed = runner(argv, input=payload, text=True, capture_output=True, timeout=30)
    if completed.returncode:
        detail = completed.stderr.strip().splitlines()[-1:] or ["remote command failed"]
        raise NotifyCredentialError(f"{peer}: {detail[0]}")
    try:
        response = json.loads(completed.stdout)
        status = str(response["status"])
    except (ValueError, KeyError, TypeError) as exc:
        raise NotifyCredentialError(
            f"{peer}: invalid secret-free receiver response"
        ) from exc
    return SyncResult(
        peer, role, f"{_ENDPOINT.host}:{_ENDPOINT.port}/{_ENDPOINT.database}", status
    )


def sync_to_peers(
    peers: tuple[str, ...],
    *,
    role: str,
    source_passfile: Path,
    apply_changes: bool,
    runner: Runner = subprocess.run,
) -> list[SyncResult]:
    """Preflight every peer, then apply only when the complete set is valid."""
    password = _source_password(source_passfile, endpoint=_ENDPOINT, role=role)
    planned = [
        _remote(
            peer, role=role, raw_password=password, apply_changes=False, runner=runner
        )
        for peer in peers
    ]
    if not apply_changes:
        return planned
    return [
        _remote(
            peer, role=role, raw_password=password, apply_changes=True, runner=runner
        )
        for peer in peers
    ]


@click.command("receive-cards-store-credential", hidden=True)
@click.option("--role", required=True)
@click.option("--apply", "apply_changes", is_flag=True)
def receive_cards_store_credential(role: str, apply_changes: bool) -> None:
    """Receive one credential over stdin; intended only for the SSH sync rail."""
    if not _ROLE.fullmatch(role):
        raise click.ClickException("invalid PostgreSQL role")
    try:
        payload = json.load(click.get_text_stream("stdin"))
        password = payload["password"]
        if not isinstance(password, str) or "\n" in password or "\r" in password:
            raise ValueError
        status = install_exact_row(
            Path("~/.pgpass").expanduser(),
            endpoint=_ENDPOINT,
            role=role,
            raw_password=password,
            apply_changes=apply_changes,
        )
    except (NotifyCredentialError, OSError, ValueError, KeyError, TypeError) as exc:
        raise click.ClickException("credential receiver refused the request") from exc
    click.echo(json.dumps({"role": role, "status": status}))


@click.command("sync-cards-store-credential")
@click.option(
    "--agent", required=True, help="Agent/project identity used to derive PGUSER."
)
@click.option(
    "--to", "peers", multiple=True, required=True, help="SSH peer to reconcile."
)
@click.option(
    "--pgpass", type=click.Path(path_type=Path, dir_okay=False), default="~/.pgpass"
)
@click.option(
    "--apply", "apply_changes", is_flag=True, help="Write after all peers pass dry-run."
)
@click.option("--json", "as_json", is_flag=True, help="Emit secret-free JSON.")
def sync_cards_store_credential(
    agent: str, peers: tuple[str, ...], pgpass: Path, apply_changes: bool, as_json: bool
) -> None:
    """Sync one role's canonical 55432 credential from this host to peers."""
    role = derive_pg_role(agent, host_user=getpass.getuser())
    try:
        results = sync_to_peers(
            peers,
            role=role,
            source_passfile=pgpass.expanduser(),
            apply_changes=apply_changes,
        )
    except NotifyCredentialError as exc:
        raise click.ClickException(str(exc)) from exc
    if as_json:
        click.echo(json.dumps({"rows": [asdict(item) for item in results]}, indent=2))
    else:
        for item in results:
            click.echo(f"{item.peer}: {item.status} ({item.target}, role={item.role})")
        if not apply_changes:
            click.echo("Dry-run: no credentials were changed. Pass --apply to write.")


def register(group: click.Group) -> None:
    group.add_command(sync_cards_store_credential)
    group.add_command(receive_cards_store_credential)


__all__ = ["SyncResult", "install_exact_row", "sync_to_peers"]
