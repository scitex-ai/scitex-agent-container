"""Install git-backed agent definitions as links into SAC's live spec root."""

from __future__ import annotations

from .._logging import render_rich
import hashlib
import json
import os
import subprocess
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

import click


_AGENTS_DIR_ENV = "SCITEX_AGENT_CONTAINER_AGENTS_DIR"


@dataclass(frozen=True)
class LinkPlan:
    agent: str
    source: str
    target: str
    state: str
    source_spec_sha256: str
    source_tree_sha256: str
    target_before: str | None
    backup: str | None


def _live_agents_root() -> Path:
    override = (os.environ.get(_AGENTS_DIR_ENV) or "").strip()
    return Path(override or "~/.scitex/agent-container/agents").expanduser()


def _validate_name(name: str) -> str:
    if not name or name in {".", ".."} or Path(name).name != name:
        raise click.ClickException(f"invalid agent name: {name!r}")
    return name


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _tree_sha256(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root).as_posix().encode()
        digest.update(relative + b"\0")
        if path.is_symlink():
            digest.update(b"link\0" + os.readlink(path).encode() + b"\0")
        elif path.is_dir():
            digest.update(b"dir\0")
        else:
            digest.update(b"file\0" + _sha256(path).encode() + b"\0")
        digest.update(oct(path.lstat().st_mode & 0o777).encode() + b"\0")
    return digest.hexdigest()


def _entry_fingerprint(path: Path) -> str | None:
    if path.is_symlink():
        return f"symlink:{os.readlink(path)}"
    if path.is_dir():
        return f"tree:{_tree_sha256(path)}"
    if path.exists():
        return f"file:{_sha256(path)}"
    return None


def _git_identity(source: Path, names: tuple[str, ...]) -> dict[str, object]:
    proc = subprocess.run(
        ["git", "-C", str(source), "rev-parse", "--show-toplevel", "HEAD"],
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        raise click.ClickException(f"source is not inside a git worktree: {source}")
    lines = proc.stdout.splitlines()
    status = subprocess.run(
        [
            "git",
            "-C",
            str(source),
            "status",
            "--porcelain",
            "--",
            *(str(source / name) for name in names),
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    return {
        "worktree": lines[0],
        "head": lines[1],
        "source_dirty": bool(status.stdout.strip()),
    }


def _same_link(target: Path, source: Path) -> bool:
    if not target.is_symlink():
        return False
    try:
        return target.resolve(strict=True) == source.resolve(strict=True)
    except OSError:
        return False


def _existing_ancestor(path: Path) -> Path:
    """Return the nearest existing path used to predict rename semantics."""
    candidate = path
    while not candidate.exists() and not candidate.is_symlink():
        parent = candidate.parent
        if parent == candidate:
            raise click.ClickException(
                f"no existing ancestor found for archive path: {path}"
            )
        candidate = parent
    return candidate


def _validate_atomic_archives(plans: list[LinkPlan]) -> None:
    """Refuse archive plans whose rename would cross filesystem devices."""
    for plan in plans:
        if plan.backup is None:
            continue
        target = Path(plan.target)
        backup = Path(plan.backup)
        target_device = target.lstat().st_dev
        backup_device = _existing_ancestor(backup.parent).stat().st_dev
        if target_device != backup_device:
            raise click.ClickException(
                "backup must be on the same filesystem as its live target for "
                f"an atomic archive: target={target} backup={backup}. Choose a "
                "same-filesystem --backup-root and retry; nothing was changed."
            )
def build_link_plans(
    *,
    source_root: Path,
    target_root: Path,
    names: tuple[str, ...],
    backup_root: Path,
) -> list[LinkPlan]:
    source_root = source_root.expanduser().resolve(strict=True)
    target_root = target_root.expanduser().absolute()
    if source_root == target_root.resolve(strict=False):
        raise click.ClickException("source and target agent roots are identical")

    plans: list[LinkPlan] = []
    for raw_name in names:
        name = _validate_name(raw_name)
        source = source_root / name
        spec = source / "spec.yaml"
        if not source.is_dir() or not spec.is_file():
            raise click.ClickException(
                f"source agent must contain spec.yaml: {source}"
            )
        target = target_root / name
        if _same_link(target, source):
            state = "current"
            backup = None
        elif target.is_symlink():
            state = "replace_link"
            backup = backup_root / name
        elif target.exists():
            state = "archive_and_link"
            backup = backup_root / name
        else:
            state = "create_link"
            backup = None
        plans.append(
            LinkPlan(
                agent=name,
                source=str(source),
                target=str(target),
                state=state,
                source_spec_sha256=_sha256(spec),
                source_tree_sha256=_tree_sha256(source),
                target_before=_entry_fingerprint(target),
                backup=str(backup) if backup else None,
            )
        )
    return plans


def apply_link_plans(plans: list[LinkPlan]) -> None:
    actionable = [plan for plan in plans if plan.state != "current"]
    _validate_atomic_archives(actionable)
    for plan in actionable:
        source = Path(plan.source)
        target = Path(plan.target)
        backup = Path(plan.backup) if plan.backup else None
        if (
            _sha256(source / "spec.yaml") != plan.source_spec_sha256
            or _tree_sha256(source) != plan.source_tree_sha256
        ):
            raise click.ClickException(f"source changed after planning: {source}")
        if _entry_fingerprint(target) != plan.target_before:
            raise click.ClickException(f"target changed after planning: {target}")
        if backup is not None and (backup.exists() or backup.is_symlink()):
            raise click.ClickException(f"backup target already exists: {backup}")
        temporary = target.parent / f".{target.name}.sac-link-{os.getpid()}"
        if temporary.exists() or temporary.is_symlink():
            raise click.ClickException(f"temporary link already exists: {temporary}")

    completed: list[tuple[Path, Path | None]] = []
    try:
        for plan in actionable:
            source = Path(plan.source)
            target = Path(plan.target)
            backup = Path(plan.backup) if plan.backup else None
            target.parent.mkdir(parents=True, exist_ok=True)
            if backup is not None:
                backup.parent.mkdir(parents=True, exist_ok=True)
                os.replace(target, backup)

            temporary = target.parent / f".{target.name}.sac-link-{os.getpid()}"
            temporary.symlink_to(source, target_is_directory=True)
            os.replace(temporary, target)
            completed.append((target, backup))
    except BaseException:
        for target, backup in reversed(completed):
            target.unlink(missing_ok=True)
            if backup is not None:
                os.replace(backup, target)
        for plan in actionable:
            target = Path(plan.target)
            temporary = target.parent / f".{target.name}.sac-link-{os.getpid()}"
            temporary.unlink(missing_ok=True)
            backup = Path(plan.backup) if plan.backup else None
            if (
                backup is not None
                and (backup.exists() or backup.is_symlink())
                and not target.exists()
                and not target.is_symlink()
            ):
                os.replace(backup, target)
        raise


@click.command(name="link-specs")
@click.option(
    "--source",
    "source_root",
    type=click.Path(path_type=Path, file_okay=False, exists=True),
    required=True,
    help="Git-backed directory containing <agent>/spec.yaml trees.",
)
@click.option(
    "--only",
    "names",
    multiple=True,
    required=True,
    help="Agent name to link. Repeat for each intended agent.",
)
@click.option(
    "--target",
    "target_root",
    type=click.Path(path_type=Path, file_okay=False),
    default=None,
    help="Live agents root; defaults to SAC's configured user root.",
)
@click.option(
    "--backup-root",
    type=click.Path(path_type=Path, file_okay=False),
    default=None,
    help="Archive destination used before replacing existing entries.",
)
@click.option("--apply", is_flag=True, help="Apply the displayed plan.")
@click.option("--json", "as_json", is_flag=True, help="Emit structured output.")
def link_specs(
    source_root: Path,
    names: tuple[str, ...],
    target_root: Path | None,
    backup_root: Path | None,
    apply: bool,
    as_json: bool,
) -> None:
    """Make one git-backed definition tree the live per-agent SSOT.

    This command is dry-run by default. Existing directories and links are
    archived before replacement; currently-correct links are left untouched.
    Every selected source must exist and contain ``spec.yaml`` before any
    target is changed. Apply refuses uncommitted selected sources. A dry-run
    exits 1 while either link drift or selected-source git drift remains, so
    the same command is a deterministic deployment gate.
    """
    source = source_root.expanduser().resolve(strict=True)
    target = (target_root or _live_agents_root()).expanduser().absolute()
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup = (
        backup_root.expanduser().absolute()
        if backup_root
        else target.parent / "spec-backups" / stamp / "agents"
    )
    unique_names = tuple(dict.fromkeys(names))
    identity = _git_identity(source, unique_names)
    if apply and identity["source_dirty"]:
        raise click.ClickException(
            "selected source definitions have uncommitted changes; "
            "commit them before installing live links"
        )
    plans = build_link_plans(
        source_root=source,
        target_root=target,
        names=unique_names,
        backup_root=backup,
    )
    if apply:
        apply_link_plans(plans)

    needs_change = any(plan.state != "current" for plan in plans)
    payload = {
        "ok": apply or (not needs_change and not identity["source_dirty"]),
        "mode": "apply" if apply else "dry-run",
        "source_root": str(source),
        "target_root": str(target),
        "git": identity,
        "agents": [asdict(plan) for plan in plans],
    }
    if as_json:
        click.echo(json.dumps(payload, indent=2))
        if not payload["ok"]:
            raise SystemExit(1)
        return

    render_rich(f"[bold]agent spec links[/bold]  {payload['mode']}  "
        f"git={identity['head']} dirty={str(identity['source_dirty']).lower()}", __name__)
    for plan in plans:
        render_rich(f"  {plan.agent:<30} {plan.state}", __name__)
        if plan.backup:
            render_rich(f"    archive: {plan.backup}", __name__)
    if not apply:
        render_rich("[yellow]No paths changed. Re-run with --apply to install.[/yellow]", __name__)
        if not payload["ok"]:
            raise SystemExit(1)


def register(agent_group: click.Group) -> None:
    agent_group.add_command(link_specs)


__all__ = ["apply_link_plans", "build_link_plans", "link_specs", "register"]
