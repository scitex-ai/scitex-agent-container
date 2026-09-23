"""``sac image list / status / snapshot`` — installed-image reporting verbs.

Extracted from :mod:`image_group` (512-line budget) when the
incident-local-heavy-build low-priority default pushed the group module
over the cap; registered back onto the ``sac image`` group via
``image_group.add_command`` so the CLI surface is unchanged. One cohesive
responsibility: read-only reporting over already-built artefacts (no
build/mutate verbs here).

Shared constants and backend seams (``_CONTAINERS_DIR``,
``_SCITEX_USER_STATE_ROOT``, ``_ensure_containers_dir``,
``_load_apptainer``, ``_load_env_snapshot``) stay in :mod:`image_group` —
the mutating verbs share them, and the test suite swaps them there via the
save/restore pattern. Each command body therefore imports
:mod:`image_group` lazily at call time: the swap stays effective and there
is no import-time cycle (image_group imports THIS module to register the
commands).
"""

from __future__ import annotations

from .._logging import render_rich
import datetime as _dt
import json
from pathlib import Path

import click



@click.command("list")
@click.option("--json", "as_json", is_flag=True, help="Output as JSON.")
def image_list(as_json: bool) -> None:
    """List installed SIFs across every scitex-* package.

    Discovers via the ``~/.scitex/<pkg>/containers/*.sif`` convention
    (operator design 8566) — sac does NOT know any other package by
    name; new packages light up automatically.

    \b
    Example:
      $ sac image list
      $ sac image list --json
    """
    from . import image_group as ig

    ig._ensure_containers_dir()
    root = ig._SCITEX_USER_STATE_ROOT
    entries: list[Path] = []
    entries.extend(sorted(root.glob("*/containers/*.sif")))
    entries.extend(sorted(p for p in root.glob("*/containers/*.sandbox") if p.is_dir()))

    def _dir_size_bytes(d: Path) -> int:
        total = 0
        for p in d.rglob("*"):
            try:
                if p.is_file() and not p.is_symlink():
                    total += p.stat().st_size
            except OSError:
                pass
        return total

    versions = []
    for p in entries:
        is_sandbox = p.is_dir()
        target_state = "available"
        try:
            artifact_stat = p.stat()
        except OSError:  # stx-allow: fallback (reason: stale entries stay visible in the returned JSON or human stdout row instead of crashing the read-only listing)
            try:
                artifact_stat = p.lstat()
            except OSError:  # stx-allow: fallback (reason: an entry deleted during the scan cannot supply stable metadata for the returned JSON or human stdout row)
                continue
            target_state = "dangling" if p.is_symlink() else "unreadable"
        if is_sandbox:
            size_bytes = _dir_size_bytes(p)
        elif target_state == "dangling":
            size_bytes = 0
        else:
            size_bytes = artifact_stat.st_size
        # RESOLVE THE SYMLINK. `sac-base.sif` is a symlink onto a DATED file
        # (`sac-base/sac-base-2026-0816-110731.sif`), and the listing printed
        # only the link name — so two hosts four days apart rendered
        # identically. Naming the target is what makes them distinguishable.
        # stx-allow: fallback (reason: a broken/absent link must still list, so
        # resolution failure degrades to "" rather than dropping the row.)
        try:
            resolved = p.resolve()
            target = resolved.name if resolved.name != p.name else ""
        except OSError:  # stx-allow: fallback (reason: see inline comment)
            target = ""
        versions.append(
            {
                "package": p.parent.parent.name,
                "name": p.name,
                "path": str(p),
                "kind": "sandbox" if is_sandbox else "sif",
                "size_bytes": size_bytes,
                "mtime": artifact_stat.st_mtime,
                # The link target, "" when the entry is not a symlink.
                "resolves_to": target,
                "target_state": target_state,
            }
        )
    if as_json:
        # STDOUT IS THE PAYLOAD. The scan-root banner below is a human
        # courtesy printed to stdout, so emitting it here made
        # ``sac image list --json | jq`` fail on the very first byte:
        #
        #   scan root: /home/…/.scitex/*/containers/
        #   [ … ]
        #
        # A ``--json`` surface promises stdout is EXACTLY one JSON
        # document; the banner is for the human render only. (Found by
        # tightening test_image_group's parse off `result.output`'s
        # prefix-skip, which had been hiding this since the banner
        # landed.)
        click.echo(json.dumps(versions, indent=2, default=str))
        return
    render_rich(f"[dim]scan root: {root}/*/containers/[/dim]", __name__)
    if not versions:
        render_rich(f"[dim](no SIFs under {root}/*/containers/ — "
            f"run `sac image build base -y && sac image build scitex -y` to "
            f"populate; downstream packages populate their own siblings)[/dim]", __name__)
        return
    for v in versions:
        size_mb = v["size_bytes"] / (1024 * 1024)
        tag = "sandbox" if v["kind"] == "sandbox" else "sif"
        label = f"{v['package']}/{v['name']}"
        # BUILT date, because a listing of sizes alone cannot answer "is this
        # host running the same image as that one?". On 2026-08-16 nas-03 and
        # compute-03 ran a 08-12 SIF while compute-04 ran 08-16; the agents
        # execute sac from INSIDE the image, so a four-day gap silently broke
        # token resolution on two hosts while every host-side check looked
        # clean. The date is what makes that comparable at a glance.
        built = _dt.datetime.fromtimestamp(v["mtime"]).strftime("%Y-%m-%d %H:%M")
        suffix = f"  -> {v['resolves_to']}" if v.get("resolves_to") else ""
        if v["target_state"] != "available":
            suffix += f"  [red]{v['target_state'].upper()}[/red]"
        render_rich(f"  {tag:<7s}  {label:50s} {size_mb:>8.1f} MB  built {built}{suffix}", __name__)
    # NECESSARY, NOT SUFFICIENT — do not let a fresh date retire the content
    # question. scitex-hpc measured a bake on 2026-07-18 whose build-context
    # source was develop HEAD (1e4870fd) while the INSTALLED wheel was pre-fix
    # (1edf17d0), because `uv pip install --force-reinstall` reinstalls without
    # rebuilding and uv's cache is keyed on VERSION, not content. `built_at`
    # would have read "just now" and been perfectly true. Only a content assert
    # (sha the changed files against the source ref) separates those two, and
    # that is tracked as sac-sif-build-content-assert-force-reinstall.


@click.command("status")
@click.option("--json", "as_json", is_flag=True, help="Output as JSON.")
def image_status(as_json: bool) -> None:
    """Report the active immutable image for each SAC layer.

    \b
    Example:
      $ sac image status
      $ sac image status --json
    """
    from . import image_group as ig

    apptainer = ig._load_apptainer()
    info = []
    for layer in ig._LAYERS:
        image_name = f"sac-{layer}"
        builds = apptainer.list_builds(ig._CONTAINERS_DIR, image_name)
        active = next((build for build in builds if build["active"]), None)
        if active is None:
            continue
        sif = Path(active["sif"])
        stat = sif.stat()
        verified = active["verified"]
        verification = (
            "verified"
            if verified is True
            else "unverified"
            if verified is False
            else "unknown"
        )
        info.append(
            {
                "name": image_name,
                "version": active["ts"],
                "sif_path": str(sif),
                "sif_size_bytes": stat.st_size,
                "sif_date": _dt.datetime.fromtimestamp(stat.st_mtime).isoformat(),
                "verification": verification,
            }
        )
    if as_json:
        click.echo(json.dumps(info, indent=2, default=str))
        return
    if not info:
        render_rich(f"[dim](no active SAC images in {ig._CONTAINERS_DIR})[/dim]", __name__)
        return
    for entry in info:
        size_mb = entry["sif_size_bytes"] / (1024 * 1024)
        render_rich(f"  {entry['name']:16s}  {size_mb:>8.1f} MB  "
            f"{entry['verification']:10s}  {entry['version']}", __name__)


@click.command("snapshot")
@click.option(
    "--output",
    "-o",
    type=click.Path(dir_okay=False, path_type=Path),
    default=None,
    help="Write JSON to this path instead of stdout.",
)
def image_snapshot(output: Path | None) -> None:
    """Capture a reproducibility snapshot (pip + apt + conda + git + ...).

    \b
    Example:
      $ sac image snapshot
      $ sac image snapshot -o env.json
    """
    from . import image_group as ig

    env_snapshot = ig._load_env_snapshot()

    snap = env_snapshot(containers_dir=ig._CONTAINERS_DIR)
    payload = json.dumps(snap, indent=2, default=str)
    if output:
        output.write_text(payload)
        render_rich(f"[green]wrote[/green] {output}", __name__)
    else:
        click.echo(payload)


__all__ = ["image_list", "image_snapshot", "image_status"]
