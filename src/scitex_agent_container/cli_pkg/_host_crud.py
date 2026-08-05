"""``sac host add / remove / set`` — peer CRUD against config.yaml.

Split out of :mod:`host_group` to keep that file under the project's
512-line ceiling. The Click commands are registered onto ``host_group``
by :func:`register` (called from ``host_group.py``).

Design notes:
- Writes go through ``ruamel.yaml`` in round-trip mode so operator
  comments + key order survive a sac-side edit.
- After every write the resulting file is re-validated via
  :meth:`Config.validate`; on failure the pre-edit text is restored.
- ``add`` auto-scaffolds a minimal config.yaml when none exists; the
  other verbs require a pre-existing file.
"""

from __future__ import annotations

import json
from pathlib import Path

import click

from .._state.host_config import _default_config_path
from .._state.host_config import load as _load_cfg
from ._helpers import _json_flag, console


def _parse_via_csv(via: str | None) -> list[str]:
    """Split ``--via foo,bar`` into a clean list."""
    if not via:
        return []
    return [item.strip() for item in via.split(",") if item.strip()]


def _refuse_if_generated(path: Path) -> None:
    """ADR-0021 guard: never CRUD-edit a GENERATED client config.

    On a client host, config.yaml is renderer output pushed from the
    master (`sac host push-config`); an in-place edit here is drift the
    next `--check` shouts about and the next push refuses to overwrite.
    The fix belongs on the MASTER, so the refusal names it and stops
    BEFORE any bytes change. Reads through symlinks (``read_text``
    follows them), so shared-config layouts are guarded too.
    """
    from .._hostsync import is_generated

    if not path.is_file():
        return
    if not is_generated(path.read_text()):
        return
    console.print(
        "[red]error:[/red] this host's config is GENERATED (client). "
        "Edit the MASTER's config.yaml and run "
        "`sac host push-config <this-host>` from the master."
    )
    raise SystemExit(2)


def _ensure_config_scaffold(path: Path) -> None:
    """Create a minimal config.yaml at ``path`` if absent."""
    if path.is_file():
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("host:\n  aliases: {}\npeers: {}\n")


def _round_trip_yaml():
    """Return a ruamel ``YAML`` configured for round-trip preservation."""
    from ruamel.yaml import YAML

    yaml_rt = YAML()
    yaml_rt.preserve_quotes = True
    yaml_rt.indent(mapping=2, sequence=4, offset=2)
    return yaml_rt


def _load_round_trip(path: Path):
    """Load ``path`` via ruamel round-trip; return ``(yaml, data)``."""
    from ruamel.yaml.comments import CommentedMap

    yaml_rt = _round_trip_yaml()
    text = path.read_text() if path.is_file() else ""
    data = yaml_rt.load(text) if text.strip() else CommentedMap()
    if data is None:
        data = CommentedMap()
    return yaml_rt, data


def _resolve_write_target(path: Path) -> Path:
    """Return the effective write target for ``path``.

    When ``path`` is a symlink (typical in shared-config layouts where
    ``~/.scitex/agent-container/config.yaml`` is symlinked to a
    fleet-shared ``config.yaml``), we follow the link and
    write through to the resolved target. Opening the symlink path
    directly for writing would replace the symlink with a regular file
    and silently break the shared-config relationship — see PA-foundation
    bug 3.
    """
    if path.is_symlink():
        return path.resolve()
    return path


def _dump_round_trip(yaml_rt, data, path: Path) -> None:
    """Round-trip-dump ``data`` to ``path``, following symlinks.

    The write goes to ``path.resolve()`` when ``path`` is a symlink so
    that shared-config setups (one shared file referenced from each
    machine's config dir) keep the symlink intact. Writing to the
    symlink path directly would replace the link with a regular file.
    """
    target = _resolve_write_target(path)
    with target.open("w") as fh:
        yaml_rt.dump(data, fh)


def _peers_block(data):
    """Return ``data['peers']`` (CommentedMap), creating it if missing."""
    from ruamel.yaml.comments import CommentedMap

    peers = data.get("peers")
    if peers is None:
        peers = CommentedMap()
        data["peers"] = peers
    return peers


def _make_peer_entry(ssh: str | None, via: list[str] | None):
    """Build a fresh peer ``CommentedMap`` from CLI options."""
    from ruamel.yaml.comments import CommentedMap

    entry = CommentedMap()
    if ssh is not None:
        entry["ssh"] = ssh
    if via:
        entry["via"] = list(via)
    return entry


def _validate_or_revert(path: Path, original_text: str | None) -> list[str]:
    """Re-load + validate; on errors revert ``path`` to ``original_text``.

    Returns the validation error list (empty = clean). When the file
    was newly created (``original_text is None``) and validation
    fails we unlink it so we don't leave a half-baked scaffold behind.
    """
    cfg = _load_cfg(path)
    errors = cfg.validate()
    if errors:
        if original_text is None:
            try:
                path.unlink()
            except FileNotFoundError:  # stx-allow: fallback (reason: race-safe cleanup)
                pass
        else:
            # Write through any symlink — see _resolve_write_target for
            # why we never want to replace the link with a regular file.
            _resolve_write_target(path).write_text(original_text)
    return errors


def _emit_ok(
    ctx: click.Context,
    as_json: bool,
    *,
    action: str,
    peer: str,
    path: Path,
) -> None:
    if _json_flag(ctx, as_json):
        click.echo(
            json.dumps(
                {"ok": True, "action": action, "peer": peer, "config_path": str(path)},
                indent=2,
            )
        )
    else:
        console.print(f"[green]ok[/green]  {action} peer '{peer}'")


def _emit_validation_failure(
    ctx: click.Context,
    as_json: bool,
    *,
    peer: str,
    errors: list[str],
    path: Path,
) -> None:
    if _json_flag(ctx, as_json):
        click.echo(
            json.dumps(
                {
                    "ok": False,
                    "peer": peer,
                    "config_path": str(path),
                    "errors": errors,
                },
                indent=2,
            )
        )
    else:
        for e in errors:
            console.print(f"[red]error:[/red] {e}")
        console.print("[red]aborted[/red]  config.yaml reverted to pre-edit state")


@click.command("add")
@click.argument("name", required=True)
@click.option(
    "--ssh", "ssh_target", required=True, help="ssh target (user@host[:port])."
)
@click.option("--via", default=None, help="Comma-separated ProxyJump peer chain.")
@click.option("--json", "as_json", is_flag=True, help="Output as JSON.")
@click.pass_context
def host_add(
    ctx: click.Context,
    name: str,
    ssh_target: str,
    via: str | None,
    as_json: bool,
) -> None:
    """Add a new peer entry to config.yaml.

    \b
    Examples:
      $ sac host add gpu-box --ssh ywatanabe@gpu.lan
      $ sac host add bm198 --ssh bm198 --via mba,spartan
    """
    path = _default_config_path()
    _refuse_if_generated(path)
    pre_existing = path.is_file()
    _ensure_config_scaffold(path)
    original_text = path.read_text() if pre_existing else None
    yaml_rt, data = _load_round_trip(path)
    peers = _peers_block(data)
    if name in peers:
        click.echo(
            f"error: peer '{name}' already exists in {path}. "
            f"Use `sac host set {name} ...` to overwrite.",
            err=True,
        )
        raise SystemExit(2)
    peers[name] = _make_peer_entry(ssh_target, _parse_via_csv(via))
    _dump_round_trip(yaml_rt, data, path)
    errors = _validate_or_revert(path, original_text)
    if errors:
        _emit_validation_failure(ctx, as_json, peer=name, errors=errors, path=path)
        raise SystemExit(1)
    _emit_ok(ctx, as_json, action="added", peer=name, path=path)


@click.command("remove")
@click.argument("name", required=True)
@click.option("--json", "as_json", is_flag=True, help="Output as JSON.")
@click.pass_context
def host_remove(ctx: click.Context, name: str, as_json: bool) -> None:
    """Remove a peer entry from config.yaml.

    \b
    Example:
      $ sac host remove gpu-box
    """
    path = _default_config_path()
    _refuse_if_generated(path)
    if not path.is_file():
        click.echo(f"error: no config.yaml found at {path}.", err=True)
        raise SystemExit(2)
    original_text = path.read_text()
    yaml_rt, data = _load_round_trip(path)
    peers = _peers_block(data)
    if name not in peers:
        click.echo(f"error: peer '{name}' not found in {path}.", err=True)
        raise SystemExit(2)
    del peers[name]
    _dump_round_trip(yaml_rt, data, path)
    errors = _validate_or_revert(path, original_text)
    if errors:
        _emit_validation_failure(ctx, as_json, peer=name, errors=errors, path=path)
        raise SystemExit(1)
    _emit_ok(ctx, as_json, action="removed", peer=name, path=path)


@click.command("set")
@click.argument("name", required=True)
@click.option(
    "--ssh", "ssh_target", default=None, help="ssh target (user@host[:port])."
)
@click.option("--via", default=None, help="Comma-separated ProxyJump peer chain.")
@click.option("--json", "as_json", is_flag=True, help="Output as JSON.")
@click.pass_context
def host_set(
    ctx: click.Context,
    name: str,
    ssh_target: str | None,
    via: str | None,
    as_json: bool,
) -> None:
    """Update fields on an existing peer entry in config.yaml.

    \b
    Examples:
      $ sac host set gpu-box --ssh new-user@gpu.lan
      $ sac host set bm198 --via mba,spartan
    """
    path = _default_config_path()
    _refuse_if_generated(path)
    if not path.is_file():
        click.echo(f"error: no config.yaml found at {path}.", err=True)
        raise SystemExit(2)
    original_text = path.read_text()
    yaml_rt, data = _load_round_trip(path)
    peers = _peers_block(data)
    if name not in peers:
        click.echo(
            f"error: peer '{name}' not found in {path}. "
            f"Use `sac host add {name} ...` to create it.",
            err=True,
        )
        raise SystemExit(2)
    entry = peers[name]
    if ssh_target is not None:
        entry["ssh"] = ssh_target
    if via is not None:
        via_list = _parse_via_csv(via)
        if via_list:
            entry["via"] = via_list
        elif "via" in entry:
            del entry["via"]
    _dump_round_trip(yaml_rt, data, path)
    errors = _validate_or_revert(path, original_text)
    if errors:
        _emit_validation_failure(ctx, as_json, peer=name, errors=errors, path=path)
        raise SystemExit(1)
    _emit_ok(ctx, as_json, action="updated", peer=name, path=path)


def register(host_group) -> None:
    """Attach the CRUD commands to the parent ``host`` Click group."""
    host_group.add_command(host_add)
    host_group.add_command(host_remove)
    host_group.add_command(host_set)


__all__ = ["host_add", "host_remove", "host_set", "register"]
