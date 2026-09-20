"""Resolve and validate the exact pre-launch home-materialization plan."""

from __future__ import annotations

import hashlib
import os
from dataclasses import asdict, dataclass
from pathlib import Path, PurePosixPath

from ..config import AgentConfig
from ..config._to_home_spec import ToHomeSpec


class HomeMaterializationError(RuntimeError):
    pass


@dataclass(frozen=True)
class ResolvedHomeImport:
    id: str
    source: str
    precedence: int
    apply: str
    destination: str
    mode: str
    conflict: str
    stale: str
    required: bool
    status: str
    content_digest: str | None


def _source_path(config: AgentConfig, raw: str) -> Path:
    path = Path(raw)
    if not path.is_absolute():
        if not config.config_path:
            raise HomeMaterializationError(
                f"to_home source {raw!r} is relative but config_path is unavailable"
            )
        path = Path(config.config_path).parent / path
    return path.resolve()


def _entries(root: Path) -> dict[PurePosixPath, tuple[str, Path]]:
    result: dict[PurePosixPath, tuple[str, Path]] = {}
    for parent, dirs, files in os.walk(root, followlinks=False):
        dirs.sort()
        files.sort()
        base = Path(parent)
        for name in dirs + files:
            path = base / name
            rel = PurePosixPath(path.relative_to(root).as_posix())
            kind = "symlink" if path.is_symlink() else ("dir" if path.is_dir() else "file")
            result[rel] = (kind, path)
    return result


def tree_digest(root: Path) -> str:
    """Digest names, entry types, modes, symlink targets and file bytes."""
    digest = hashlib.sha256()
    for rel, (kind, path) in _entries(root).items():
        digest.update(f"{kind}\0{rel}\0{path.lstat().st_mode & 0o777:o}\0".encode())
        if kind == "file":
            with path.open("rb") as stream:
                for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                    digest.update(chunk)
        elif kind == "symlink":
            digest.update(os.readlink(path).encode())
            resolved = path.resolve(strict=True)
            if resolved.is_file():
                with resolved.open("rb") as stream:
                    for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                        digest.update(chunk)
            elif resolved.is_dir():
                digest.update(tree_digest(resolved).encode())
        digest.update(b"\0")
    return f"sha256:{digest.hexdigest()}"


def resolve_home_imports(config: AgentConfig) -> list[ResolvedHomeImport]:
    """Resolve every source and reject all ambiguity before materialisation."""
    if not isinstance(config.to_home, ToHomeSpec):
        raise HomeMaterializationError(
            "legacy spec.to_home is not executable; migrate it to the explicit "
            "spec.to_home.imports materialization contract"
        )
    resolved: list[ResolvedHomeImport] = []
    owners: dict[PurePosixPath, str] = {}
    kinds: dict[PurePosixPath, str] = {}
    for layer in config.to_home.imports:
        source = _source_path(config, layer.source)
        if not source.is_dir():
            if layer.required:
                raise HomeMaterializationError(
                    f"required to_home import {layer.id!r} source is not a directory: {source}"
                )
            authored = asdict(layer)
            authored["source"] = str(source)
            resolved.append(
                ResolvedHomeImport(**authored, status="missing", content_digest=None)
            )
            continue
        entries = _entries(source)
        target_root = PurePosixPath(layer.destination)
        for rel, (kind, _path) in entries.items():
            destination = target_root / rel
            conflicts = {
                owner
                for prior, owner in owners.items()
                if (prior == destination and not (kinds[prior] == kind == "dir"))
                or (kinds[prior] != "dir" and prior in destination.parents)
                or (kind != "dir" and destination in prior.parents)
            }
            if conflicts and layer.conflict == "error":
                raise HomeMaterializationError(
                    f"to_home import {layer.id!r} conflicts at {destination} with "
                    f"earlier import(s) {sorted(conflicts)!r}; choose "
                    "conflict: higher-precedence-wins explicitly"
                )
            if conflicts:
                for prior in list(owners):
                    if prior == destination or prior in destination.parents or destination in prior.parents:
                        owners.pop(prior)
                        kinds.pop(prior)
            owners[destination] = layer.id
            kinds[destination] = kind
        authored = asdict(layer)
        authored["source"] = str(source)
        resolved.append(
            ResolvedHomeImport(
                **authored, status="present", content_digest=tree_digest(source)
            )
        )
    config.resolved_to_home_imports = [asdict(layer) for layer in resolved]
    return resolved
