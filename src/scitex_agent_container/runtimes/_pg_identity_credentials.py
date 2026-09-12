"""Materialize the least PostgreSQL credential needed by one agent."""

from __future__ import annotations

import hashlib
import os
import secrets
from pathlib import Path
from typing import Any, Iterable, Mapping

from ._board_identity_env import raw_args_env
from ._fleet_env import effective_env
from ._pg_identity_env import (
    PG_USER_ENV,
    PgIdentityCredentialError,
    _pgpass_fields,
    _pgpass_is_private_regular,
    _pgpass_target,
)

_POSTGRES_DSN_KEYS = (
    "SCITEX_CARDS_DB",
    "SCITEX_CARDS_NOTIFY_DSN",
    "SCITEX_STORE_DSN",
)
PG_PASSFILE_ENV = "PGPASSFILE"
DEFAULT_CONTAINER_PGPASSFILE = "/home/agent/.sac-pgpass"


def _matches(declared: str, actual: str) -> bool:
    return declared in {"*", actual}


def _selected_rows(
    source: Path,
    *,
    role: str,
    dsns: Iterable[str],
) -> list[str]:
    targets = tuple(target for dsn in dsns if (target := _pgpass_target(dsn)))
    selected: list[str] = []
    covered: set[tuple[str, str, str]] = set()
    if not _pgpass_is_private_regular(source):
        return []
    try:
        lines = source.read_text(encoding="utf-8").splitlines()
    except OSError:
        lines = []
    for line in lines:
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        fields = _pgpass_fields(line)
        # Never copy a wildcard-user credential into an agent.  The generated
        # file grants exactly the resolved project role and no neighbouring
        # role from the operator's multi-role passfile.
        if fields is None or fields[3] != role:
            continue
        matched = {
            target
            for target in targets
            if all(
                _matches(declared, actual)
                for declared, actual in zip(fields[:3], target, strict=True)
            )
        }
        if matched:
            selected.append(line)
            covered.update(matched)
    if not targets or len(covered) != len(set(targets)):
        return []
    return selected


def _marker(path: Path) -> Path:
    return path.with_name(f".{path.name}.sha256")


def _remove_owned_file(path: Path) -> None:
    marker = _marker(path)
    try:
        expected = marker.read_text(encoding="ascii").strip()
        actual = hashlib.sha256(path.read_bytes()).hexdigest()
        if secrets.compare_digest(expected, actual):
            path.unlink()
    except OSError:
        return
    finally:
        try:
            marker.unlink()
        except FileNotFoundError:
            pass


def _write_private(path: Path, rows: Iterable[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    rendered = "".join(f"{row}\n" for row in rows)
    temporary = path.with_name(f".{path.name}.{secrets.token_hex(8)}.tmp")
    marker = _marker(path)
    marker_temporary = marker.with_name(f".{marker.name}.{secrets.token_hex(8)}.tmp")
    installed = False
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(rendered)
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
        installed = True
        os.chmod(path, 0o600)
        marker_temporary.write_text(
            hashlib.sha256(rendered.encode()).hexdigest(), encoding="ascii"
        )
        marker_temporary.chmod(0o600)
        os.replace(marker_temporary, marker)
    except BaseException:
        for leftover in (temporary, marker_temporary):
            try:
                leftover.unlink()
            except FileNotFoundError:
                pass
        if installed:
            try:
                path.unlink()
            except FileNotFoundError:
                pass
        raise


def materialize_project_pgpass(
    config: Any,
    *,
    home_backings: Iterable[Path],
    servers: Mapping[str, Mapping[str, Any]],
    host_environ: Mapping[str, str] | None = None,
    fleet_defaults: Mapping[str, str] | None = None,
) -> list[Path]:
    """Write role-filtered credentials when SAC supplies its passfile path."""
    backings = list(dict.fromkeys(Path(path) for path in home_backings))
    destinations = [path / Path(DEFAULT_CONTAINER_PGPASSFILE).name for path in backings]
    apptainer = getattr(config, "apptainer", None)
    raw_args = getattr(apptainer, "raw_args", None) if apptainer is not None else None
    raw_env = raw_args_env(raw_args)
    spec_env = getattr(config, "env", None)
    explicitly_declared = (
        isinstance(spec_env, Mapping) and PG_PASSFILE_ENV in spec_env
    ) or PG_PASSFILE_ENV in raw_env
    dsns = tuple(
        value
        for server in servers.values()
        if isinstance((declared := server.get("env")), Mapping)
        if str(declared.get(PG_PASSFILE_ENV, "")).strip()
        == DEFAULT_CONTAINER_PGPASSFILE
        for key in _POSTGRES_DSN_KEYS
        if (value := str(declared.get(key, ""))).startswith(
            ("postgresql://", "postgres://")
        )
    )
    if explicitly_declared or not dsns:
        for destination in destinations:
            _remove_owned_file(destination)
        return []

    launch_env = effective_env(config, defaults=fleet_defaults)
    launch_env.update(raw_env)
    role = str(launch_env.get(PG_USER_ENV, "")).strip()
    environment = os.environ if host_environ is None else host_environ
    source = Path(environment.get(PG_PASSFILE_ENV) or "~/.pgpass").expanduser()
    rows = _selected_rows(source, role=role, dsns=dsns) if role else []
    if not rows:
        for destination in destinations:
            _remove_owned_file(destination)
        raise PgIdentityCredentialError(
            f"PostgreSQL identity {role!r} has no complete credential for the "
            "declared store targets; provision the exact project role before "
            "starting the agent"
        )
    for destination in destinations:
        _write_private(destination, rows)
    return destinations


__all__ = [
    "DEFAULT_CONTAINER_PGPASSFILE",
    "PG_PASSFILE_ENV",
    "materialize_project_pgpass",
]
