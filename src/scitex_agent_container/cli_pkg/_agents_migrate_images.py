"""Deterministic migration from managed SIF paths to logical image names."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from pathlib import Path

import click
import yaml

from ..runtimes._apptainer_image_ref import legacy_managed_image_name


def _mapping_value(node: yaml.Node, key: str) -> yaml.Node | None:
    if not isinstance(node, yaml.MappingNode):
        return None
    for key_node, value_node in node.value:
        if isinstance(key_node, yaml.ScalarNode) and key_node.value == key:
            return value_node
    return None


def migrate_spec_text(text: str) -> tuple[str, str | None, str | None]:
    """Return ``(new_text, old_reference, logical_name)`` without reformatting."""
    document = yaml.compose(text)
    spec = _mapping_value(document, "spec") if document is not None else None
    apptainer = _mapping_value(spec, "apptainer") if spec is not None else None
    image = _mapping_value(apptainer, "image") if apptainer is not None else None
    if not isinstance(image, yaml.ScalarNode):
        return text, None, None
    logical = legacy_managed_image_name(image.value)
    if logical is None:
        return text, None, None
    migrated = text[: image.start_mark.index] + logical + text[image.end_mark.index :]
    parsed = yaml.safe_load(migrated)
    actual = parsed["spec"]["apptainer"]["image"]
    if actual != logical:
        raise ValueError(f"post-migration image mismatch: {actual!r} != {logical!r}")
    return migrated, image.value, logical


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _atomic_write(path: Path, text: str) -> None:
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, path.stat().st_mode)
        os.replace(temporary, path)
    finally:
        try:
            Path(temporary).unlink()
        except FileNotFoundError:
            pass


def migration_inventory(roots: tuple[Path, ...], *, apply: bool) -> dict:
    """Inspect all roots and optionally atomically apply every exact rewrite."""
    paths = sorted({p.resolve() for root in roots for p in root.glob("*/spec.yaml")})
    rows: list[dict[str, str]] = []
    for path in paths:
        original = path.read_text(encoding="utf-8")
        migrated, old, logical = migrate_spec_text(original)
        if logical is None:
            continue
        row = {
            "path": str(path),
            "from": str(old),
            "to": logical,
            "before_sha256": _sha256(original),
            "after_sha256": _sha256(migrated),
        }
        if apply:
            _atomic_write(path, migrated)
        rows.append(row)
    return {
        "mode": "apply" if apply else "dry-run",
        "roots": [str(root.resolve()) for root in roots],
        "specs_scanned": len(paths),
        "migrations_required": len(rows),
        "migrations": rows,
    }


@click.command(name="migrate-images")
@click.option(
    "--root",
    "roots",
    multiple=True,
    type=click.Path(path_type=Path, exists=True, file_okay=False),
    required=True,
    help="Agents root containing <name>/spec.yaml; repeat for multiple roots.",
)
@click.option("--apply", "apply_changes", is_flag=True, help="Apply atomic rewrites.")
@click.option(
    "--check",
    is_flag=True,
    help="Exit 1 when migration is required; never writes.",
)
@click.option("--json", "as_json", is_flag=True, help="Emit the audit as JSON.")
def migrate_images(
    roots: tuple[Path, ...], apply_changes: bool, check: bool, as_json: bool
) -> None:
    """Replace host-specific managed image paths with logical SAC names."""
    if check and apply_changes:
        raise click.UsageError("--check and --apply are mutually exclusive")
    payload = migration_inventory(roots, apply=apply_changes)
    if as_json:
        click.echo(json.dumps(payload, indent=2, sort_keys=True))
    else:
        click.echo(
            f"{payload['mode']}: scanned={payload['specs_scanned']} "
            f"migration-required={payload['migrations_required']}"
        )
        for row in payload["migrations"]:
            click.echo(f"{row['path']}: {row['from']} -> {row['to']}")
    if check and payload["migrations_required"]:
        raise click.exceptions.Exit(1)


def register(agent_group: click.Group) -> None:
    agent_group.add_command(migrate_images)


__all__ = ["migrate_images", "migrate_spec_text", "migration_inventory", "register"]
