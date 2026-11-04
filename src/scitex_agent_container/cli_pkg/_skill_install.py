"""Thin SAC adapters over the shared, validated Dev skill installer."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import uuid
from pathlib import Path

import click

PACKAGE = "scitex-agent-container"


def _require_link_slot(link: Path) -> None:
    if link.exists() and not link.is_symlink():
        raise click.ClickException(
            f"refusing to replace unmanaged skill projection {link}"
        )


def _link_projection(link: Path, target: Path) -> None:
    _require_link_slot(link)
    link.parent.mkdir(parents=True, exist_ok=True)
    temporary = link.with_name(f".{link.name}.{uuid.uuid4().hex}.tmp")
    try:
        temporary.symlink_to(target.resolve(strict=True), target_is_directory=True)
        os.replace(temporary, link)
    finally:
        if temporary.is_symlink():
            temporary.unlink()


def _require_shared_installer(*, python: str | None = None) -> None:
    probe = (
        "import json\n"
        "from scitex_dev.skill_registry import INSTALL_CAPABILITIES\n"
        "print(json.dumps(sorted(INSTALL_CAPABILITIES)))\n"
    )
    try:
        result = subprocess.run(
            [python or sys.executable, "-c", probe],
            capture_output=True,
            text=True,
            timeout=30,
        )
        capabilities = json.loads(result.stdout) if result.returncode == 0 else []
    except (OSError, ValueError, subprocess.TimeoutExpired):
        capabilities = []
    if "transactional-projection-v1" not in capabilities:
        raise click.ClickException(
            "SAC skill installation requires the protected scitex-dev skills "
            "installer capability transactional-projection-v1. Update scitex-dev "
            "in this interpreter; no skill destination was changed."
        )


def _audit_installed_projection(payload: dict) -> Path:
    from scitex_dev.skill_registry import (
        RegistryReport,
        SkillRecord,
        audit_projection,
    )

    base = Path(payload["destination"]).resolve(strict=True)
    document = json.loads(Path(payload["manifest"]).read_text(encoding="utf-8"))
    if document.get("projection_sha256") != payload.get("projection_sha256"):
        raise click.ClickException(
            "shared skill projection digest does not match installer result"
        )
    records = tuple(
        SkillRecord(
            item["name"],
            item["owner"],
            Path(item["source"]),
            Path(item["source"]) / "SKILL.md",
            item["source_hash"],
        )
        for item in document["skills"]
    )
    if PACKAGE not in {item.name for item in records}:
        raise click.ClickException(
            "shared projection does not contain SAC's skill namespace"
        )
    report = audit_projection(RegistryReport(records, ()), base)
    if not report.ok:
        raise click.ClickException("shared skill projection failed post-install audit")
    return base


def install_skills(
    *,
    dest,
    no_link,
    claude_symlink,
    no_hermes_symlink,
    dry_run,
    as_json,
) -> None:
    """Delegate materialization; expose only an audited store to native harnesses."""
    hermes = Path.home() / ".hermes" / "skills" / PACKAGE
    if not no_hermes_symlink:
        _require_link_slot(hermes)
    _require_shared_installer()
    command = [
        sys.executable,
        "-m",
        "scitex_dev",
        "dev",
        "skills",
        "install",
        "--package",
        PACKAGE,
        "--json",
        "--no-link" if no_link else "--link",
    ]
    if dest is not None:
        command.extend(["--dest", str(Path(dest).expanduser())])
    if claude_symlink:
        command.append("--claude-symlink")
    if dry_run:
        command.append("--dry-run")
    result = subprocess.run(command, capture_output=True, text=True, timeout=120)
    try:
        payload = json.loads(result.stdout)
    except (ValueError, TypeError) as exc:
        raise click.ClickException(
            "shared skill installer returned no valid JSON result"
        ) from exc
    if result.returncode or payload.get("ok") is not True:
        if as_json:
            click.echo(json.dumps(payload, indent=2))
        elif result.stderr:
            click.echo(result.stderr.rstrip(), err=True)
        raise click.exceptions.Exit(result.returncode or 1)
    base = Path(payload["destination"]).resolve(strict=False)
    if not dry_run:
        base = _audit_installed_projection(payload)
        if not no_hermes_symlink:
            _link_projection(hermes, base / PACKAGE)
    payload["hermes_projection"] = None if no_hermes_symlink else str(hermes)
    payload["client_discovery"] = "not-verified"
    if as_json:
        click.echo(json.dumps(payload, indent=2, sort_keys=True))
    else:
        action = "would copy" if no_link else "would symlink"
        if dry_run:
            click.echo(f"{action} installed SAC skills → {base / PACKAGE}")
            if not no_hermes_symlink:
                click.echo(f"would symlink {hermes} → {base / PACKAGE}")
        else:
            click.echo(f"installed validated SAC skills → {base / PACKAGE}")
            if not no_hermes_symlink:
                click.echo(f"linked {hermes} → {base / PACKAGE}")
