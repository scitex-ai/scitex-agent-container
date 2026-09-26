"""Provision the direct PostgreSQL credentials used by Cards doorbells.

Cards data stays on the pooled ``SCITEX_CARDS_DB`` connection. PostgreSQL
``LISTEN`` needs a session connection, so ``SCITEX_CARDS_NOTIFY_DSN`` names a
second, direct endpoint. This command copies no credential from anywhere else:
for every selected spec it derives the direct row only from the exact role's
working pooled row in the same passfile.

The command is dry-run by default. It never prints a password, rejects
ambiguous or conflicting rows, validates the complete write set before
changing anything, and replaces each passfile atomically.
"""

from __future__ import annotations

import getpass
import json
import os
import stat
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path
from urllib.parse import unquote, urlsplit

import click
import yaml

from .._reconcile._pass import fleet_agents_dir, fleet_spec_paths
from ..runtimes._pg_identity_env import derive_pg_role


class NotifyCredentialError(RuntimeError):
    """A direct-listener credential cannot be derived safely."""


@dataclass(frozen=True)
class Endpoint:
    host: str
    port: str
    database: str


@dataclass(frozen=True)
class Request:
    agent: str
    role: str
    passfile: Path
    source: Endpoint
    target: Endpoint


@dataclass(frozen=True)
class Result:
    agent: str
    role: str
    passfile: str
    target: str
    status: str


@dataclass(frozen=True)
class _Row:
    decoded: tuple[str, str, str, str, str]
    raw_password: str


def _endpoint(value: object, *, variable: str, port: int) -> Endpoint:
    text = str(value or "").strip()
    try:
        parsed = urlsplit(text)
    except ValueError as exc:
        raise NotifyCredentialError(f"{variable} is not a valid PostgreSQL DSN") from exc
    database = unquote(parsed.path.lstrip("/").split("/", 1)[0])
    if (
        parsed.scheme not in {"postgres", "postgresql"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.port != port
        or not database
    ):
        raise NotifyCredentialError(
            f"{variable} must be a credential-free PostgreSQL DSN on port {port}"
        )
    return Endpoint(parsed.hostname, str(port), database)


def _raw_fields(line: str) -> tuple[str, ...] | None:
    fields: list[str] = []
    current: list[str] = []
    escaped = False
    for char in line.rstrip("\r\n"):
        if char == ":" and not escaped:
            fields.append("".join(current))
            current = []
        else:
            current.append(char)
        if char == "\\" and not escaped:
            escaped = True
        else:
            escaped = False
    fields.append("".join(current))
    return tuple(fields) if len(fields) == 5 else None


def _decode_field(field: str) -> str:
    out: list[str] = []
    escaped = False
    for char in field:
        if escaped:
            out.append(char)
            escaped = False
        elif char == "\\":
            escaped = True
        else:
            out.append(char)
    if escaped:
        out.append("\\")
    return "".join(out)


def _row(line: str) -> _Row | None:
    raw = _raw_fields(line)
    if raw is None:
        return None
    decoded = tuple(_decode_field(field) for field in raw)
    return _Row(decoded=decoded, raw_password=raw[4])  # type: ignore[arg-type]


def _escape_field(field: str) -> str:
    return field.replace("\\", "\\\\").replace(":", "\\:")


def _request(spec_path: Path, *, host_user: str, passfile: Path | None) -> Request:
    try:
        document = yaml.safe_load(spec_path.read_text(encoding="utf-8")) or {}
        spec = document["spec"]
        env = spec["apptainer"]["env"]
    except (OSError, TypeError, KeyError, yaml.YAMLError) as exc:
        raise NotifyCredentialError(f"cannot read agent spec {spec_path}") from exc
    agent = spec_path.parent.name
    source = _endpoint(env.get("SCITEX_CARDS_DB"), variable="SCITEX_CARDS_DB", port=55432)
    target = _endpoint(
        env.get("SCITEX_CARDS_NOTIFY_DSN"),
        variable="SCITEX_CARDS_NOTIFY_DSN",
        port=55433,
    )
    if (source.host, source.database) != (target.host, target.database):
        raise NotifyCredentialError(
            f"{agent}: Cards data and notify DSNs must name the same host and database"
        )
    labels = document.get("metadata", {}).get("labels", {}) or {}
    role = str(env.get("PGUSER") or "").strip() or derive_pg_role(
        agent,
        project_name=str(labels.get("project") or "").strip() or None,
        host_user=host_user,
    )
    declared_passfile = Path(str(env.get("PGPASSFILE") or "")).expanduser()
    selected_passfile = passfile or declared_passfile
    if not str(selected_passfile):
        raise NotifyCredentialError(f"{agent}: PGPASSFILE is not declared")
    return Request(agent, role, selected_passfile, source, target)


def _assert_private_regular(path: Path) -> None:
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise NotifyCredentialError(f"passfile is unavailable: {path}") from exc
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid != os.geteuid()
        or metadata.st_mode & 0o077
    ):
        raise NotifyCredentialError(
            f"passfile must be a non-symlink regular file owned by this user with mode 0600: {path}"
        )


def _derive_row(lines: list[str], request: Request) -> tuple[str | None, str]:
    parsed = [row for line in lines if (row := _row(line)) is not None]
    source = next(
        (
            row
            for row in parsed
            if row.decoded[0] in {"*", request.source.host}
            and row.decoded[1] == request.source.port
            and row.decoded[2] in {"*", request.source.database}
            and row.decoded[3] == request.role
        ),
        None,
    )
    target_rows = [
        row
        for row in parsed
        if row.decoded[:4]
        == (
            request.target.host,
            request.target.port,
            request.target.database,
            request.role,
        )
    ]
    target_label = (
        f"{request.target.host}:{request.target.port}:"
        f"{request.target.database}:{request.role}"
    )
    if source is None:
        raise NotifyCredentialError(
            f"{request.agent}: no matching port-55432 credential for {request.role}"
        )
    if target_rows:
        if any(row.raw_password != source.raw_password for row in target_rows):
            raise NotifyCredentialError(
                f"{request.agent}: conflicting existing direct credential for {target_label}"
            )
        return None, "ready"
    fields = (
        request.target.host,
        request.target.port,
        request.target.database,
        request.role,
    )
    rendered = ":".join(_escape_field(field) for field in fields)
    return f"{rendered}:{source.raw_password}\n", "planned"


def provision(
    requests: list[Request], *, apply_changes: bool
) -> list[Result]:
    """Validate and optionally atomically append every requested direct row."""
    by_file: dict[Path, list[Request]] = {}
    for request in requests:
        by_file.setdefault(request.passfile, []).append(request)

    staged: dict[Path, tuple[list[str], list[str]]] = {}
    statuses: dict[tuple[Path, str, str], str] = {}
    for path, members in by_file.items():
        _assert_private_regular(path)
        lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
        additions: dict[str, str] = {}
        for request in members:
            new_row, status = _derive_row(lines + list(additions.values()), request)
            statuses[(path, request.agent, request.role)] = status
            if new_row is not None:
                key = (
                    f"{request.target.host}:{request.target.port}:"
                    f"{request.target.database}:{request.role}"
                )
                additions[key] = new_row
        staged[path] = (lines, [additions[key] for key in sorted(additions)])

    if apply_changes:
        for path, (lines, additions) in staged.items():
            if not additions:
                continue
            prefix = "" if not lines or lines[-1].endswith(("\n", "\r")) else "\n"
            content = "".join(lines) + prefix + "".join(additions)
            descriptor, temporary = tempfile.mkstemp(prefix=".pgpass-notify-", dir=path.parent)
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
            for request in by_file[path]:
                key = (path, request.agent, request.role)
                if statuses[key] == "planned":
                    statuses[key] = "provisioned"

    return [
        Result(
            request.agent,
            request.role,
            str(request.passfile),
            f"{request.target.host}:{request.target.port}/{request.target.database}",
            statuses[(request.passfile, request.agent, request.role)],
        )
        for request in requests
    ]


@click.command("provision-cards-notify")
@click.option("--root", type=click.Path(path_type=Path, file_okay=False))
@click.option("--agent", "agents", multiple=True, help="Limit to exact agent names.")
@click.option(
    "--pgpass",
    type=click.Path(path_type=Path, dir_okay=False),
    help="Override the spec-declared PGPASSFILE.",
)
@click.option("--apply", "apply_changes", is_flag=True, help="Atomically write missing rows.")
@click.option("--check", is_flag=True, help="Exit 1 when any row still needs provisioning.")
@click.option("--json", "as_json", is_flag=True, help="Emit secret-free JSON.")
def provision_cards_notify(
    root: Path | None,
    agents: tuple[str, ...],
    pgpass: Path | None,
    apply_changes: bool,
    check: bool,
    as_json: bool,
) -> None:
    """Provision direct LISTEN credentials from pooled Cards credentials.

    Dry-run is the default. Specs must explicitly declare both Cards DSNs;
    this command never guesses a direct port at runtime.
    """
    if apply_changes and check:
        raise click.UsageError("--apply and --check are mutually exclusive")
    specs_root = (root or fleet_agents_dir()).expanduser()
    paths = fleet_spec_paths(specs_root)
    selected = set(agents)
    if selected:
        found = {path.parent.name for path in paths}
        missing = sorted(selected - found)
        if missing:
            raise click.ClickException(f"agent spec not found: {', '.join(missing)}")
        paths = [path for path in paths if path.parent.name in selected]
    try:
        requests = [
            _request(path, host_user=getpass.getuser(), passfile=pgpass)
            for path in paths
        ]
        results = provision(requests, apply_changes=apply_changes)
    except NotifyCredentialError as exc:
        raise click.ClickException(str(exc)) from exc
    if as_json:
        click.echo(json.dumps({"rows": [asdict(row) for row in results]}, indent=2))
    else:
        for row in results:
            click.echo(f"{row.agent}: {row.status} ({row.target}, role={row.role})")
        if not apply_changes:
            click.echo("Dry-run: no credentials were changed. Pass --apply to write.")
    if check and any(row.status == "planned" for row in results):
        raise SystemExit(1)


def register(group: click.Group) -> None:
    group.add_command(provision_cards_notify)


__all__ = [
    "Endpoint",
    "NotifyCredentialError",
    "Request",
    "Result",
    "provision",
    "provision_cards_notify",
    "register",
]
