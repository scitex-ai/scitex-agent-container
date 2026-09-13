"""CLI face for ``sac image distribute``."""

from __future__ import annotations

import json
import os
import uuid
from pathlib import Path

import click

from .._state.host_config import load as _load_config
from ._image_distribute_core import (
    DEFAULT_TARGET_ROOT,
    LAYERS,
    SshImageTransport,
    distribute,
    resolve_artifact,
)


def _transport(peers, timeout: int):
    return SshImageTransport(peers, timeout=timeout)


def _write_receipt(path: Path, payload: dict) -> None:
    """Atomically write one requested audit receipt, never via a broad path."""
    path = path.expanduser().resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f".{path.name}.tmp.{os.getpid()}.{uuid.uuid4().hex}")
    try:
        with temp.open("x", encoding="utf-8") as stream:
            json.dump(payload, stream, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


@click.command("distribute")
@click.argument("source", type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.option("--layer", required=True, type=click.Choice(LAYERS))
@click.option(
    "--host",
    "hosts",
    multiple=True,
    required=True,
    help="Configured target peer. Repeat for every intended host; there is no --all.",
)
@click.option(
    "--target-root",
    default=DEFAULT_TARGET_ROOT,
    show_default=True,
    help="Remote containers directory (absolute or ~/...).",
)
@click.option("--timeout", type=click.IntRange(min=1), default=3600, show_default=True)
@click.option(
    "--dry-run", is_flag=True, help="Resolve and print the complete plan only."
)
@click.option("--json", "as_json", is_flag=True, help="Print the receipt as JSON.")
@click.option(
    "--receipt",
    type=click.Path(dir_okay=False, path_type=Path),
    help="Also atomically write the structured JSON receipt to this local file.",
)
def image_distribute(
    source: Path,
    layer: str,
    hosts: tuple[str, ...],
    target_root: str,
    timeout: int,
    dry_run: bool,
    as_json: bool,
    receipt: Path | None,
) -> None:
    """Distribute one explicit SIF to explicitly named configured hosts.

    The local file is resolved and hashed before transport. Each host verifies
    size and SHA-256 before and after its atomic temp-to-final rename. No live
    image link changes until every host verifies; a partial activation is
    restored to the preflight link states. Existing artifacts are never pruned.

    \b
      sac image distribute ./sac-base.sif --layer base \\
        --host compute-01 --host compute-02 --json
    """
    try:
        artifact = resolve_artifact(source, layer)
        cfg = _load_config()
        unknown = [host for host in hosts if cfg.peers.get(host) is None]
        if unknown:
            configured = ", ".join(cfg.peers.keys()) or "(none)"
            raise click.UsageError(
                f"target host(s) not configured in {cfg.source_path}: "
                f"{', '.join(unknown)}; configured peers: {configured}"
            )
        result = distribute(
            artifact=artifact,
            hosts=hosts,
            target_root=target_root,
            transport=None if dry_run else _transport(cfg.peers, timeout),
            dry_run=dry_run,
        )
    except ValueError as exc:
        raise click.UsageError(str(exc)) from exc

    payload = result.as_dict()
    if receipt is not None and not dry_run:
        _write_receipt(receipt, payload)
    if as_json:
        click.echo(json.dumps(payload, indent=2, sort_keys=True))
    else:
        click.echo(
            f"{'DRY RUN ' if dry_run else ''}image {artifact.sha256} "
            f"({artifact.size} bytes)"
        )
        for row in result.hosts:
            detail = f": {row.error}" if row.error else ""
            click.echo(f"  {row.host}: {row.status}{detail}")
        if receipt is not None and not dry_run:
            click.echo(f"receipt: {receipt.expanduser().resolve()}")
    if not result.success:
        raise click.exceptions.Exit(1)


__all__ = ["image_distribute"]
