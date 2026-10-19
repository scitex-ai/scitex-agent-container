"""``sac image disk`` — filesystem pressure for the image store + alert.

Card ``infra-disk-hygiene-mechanize-20260929`` gap 3: no 85%-full alert
existed anywhere, so a full runner disk broke CI silently. This verb
reports the store filesystem's pressure and fires a loud ``ALERT`` line
when it reaches ``--alert-threshold`` (default 85). The threshold is
explicitly settable so the firing path is testable on any host: 0.0
always fires, 101.0 can never fire (a real filesystem tops out at
100%). Exit code stays 0 either way — the ALERT line is the signal,
automation keys on the text, and a full disk must never break the
reporting verb itself.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import click

from .._logging import render_rich

DEFAULT_ALERT_THRESHOLD = 85.0
NEVER_FIRES_THRESHOLD = 101.0


def disk_pressure_pct(path: Path) -> float:
    """Used percent of the filesystem holding ``path`` (real statfs)."""
    usage = shutil.disk_usage(path)
    if usage.total <= 0:
        return 0.0
    return usage.used / usage.total * 100.0


def pressure_fires(used_pct: float, *, threshold: float) -> bool:
    """Pure threshold rule, so both directions are unit-testable."""
    return used_pct >= threshold


@click.command("disk")
@click.option(
    "--path",
    "path",
    type=click.Path(path_type=Path),
    default=None,
    help="Filesystem to measure (default: the image containers dir).",
)
@click.option(
    "--alert-threshold",
    type=float,
    default=DEFAULT_ALERT_THRESHOLD,
    show_default=True,
    help="Fire the ALERT line at or above this percent "
    "(0.0 always fires; 101.0 never fires).",
)
def image_disk(path: Path | None, alert_threshold: float) -> None:
    """Report image-store filesystem pressure; ALERT at the threshold.

    Example:
      $ sac image disk
      $ sac image disk --alert-threshold 0
    """
    from . import image_group as ig

    if not 0.0 <= alert_threshold <= NEVER_FIRES_THRESHOLD:
        raise click.UsageError(
            "--alert-threshold must be between 0.0 and 101.0"
        )
    target = path if path is not None else ig._ensure_containers_dir()
    try:
        usage = shutil.disk_usage(target)
        pct = disk_pressure_pct(target)
    except OSError as exc:
        raise click.ClickException(
            f"Cannot measure filesystem for {target}: {exc}"
        ) from exc
    used_gb = usage.used / (1024**3)
    total_gb = usage.total / (1024**3)
    render_rich(
        f"  {target}  {used_gb:.1f} / {total_gb:.1f} GB  ({pct:.1f}%)",
        __name__,
    )
    if pressure_fires(pct, threshold=alert_threshold):
        render_rich(
            f"[red]ALERT: disk pressure {pct:.1f}% >= "
            f"threshold {alert_threshold:.1f}% — reclaim space "
            f"(`sac image prune --dry-run`)[/red]",
            __name__,
        )
