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

import datetime as _dt
import json
from pathlib import Path

import click

from ._helpers import console


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
    entries.extend(
        sorted(p for p in root.glob("*/containers/*.sandbox") if p.is_dir())
    )

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
        size_bytes = _dir_size_bytes(p) if is_sandbox else p.stat().st_size
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
                "mtime": p.stat().st_mtime,
                # The link target, "" when the entry is not a symlink.
                "resolves_to": target,
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
    console.print(f"[dim]scan root: {root}/*/containers/[/dim]")
    if not versions:
        console.print(
            f"[dim](no SIFs under {root}/*/containers/ — "
            f"run `sac image build base -y && sac image build scitex -y` to "
            f"populate; downstream packages populate their own siblings)[/dim]"
        )
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
        console.print(
            f"  {tag:<7s}  {label:50s} {size_mb:>8.1f} MB  built {built}{suffix}"
        )
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
    """Unified container dashboard (active version, sandboxes, sizes).

    \b
    Example:
      $ sac image status
      $ sac image status --json
    """
    from . import image_group as ig

    sc_status = ig._load_apptainer().status

    info = sc_status(containers_dir=ig._CONTAINERS_DIR)
    if as_json:
        click.echo(json.dumps(info, indent=2, default=str))
        return
    if not info:
        console.print(f"[dim](no containers in {ig._CONTAINERS_DIR})[/dim]")
        return
    for entry in info:
        name = entry.get("name", "?")
        size = entry.get("sif_size", "-")
        rebuild = "REBUILD" if entry.get("needs_rebuild") else "ok"
        console.print(f"  {name:30s}  {size!s:>10}  {rebuild}")


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
        console.print(f"[green]wrote[/green] {output}")
    else:
        click.echo(payload)


__all__ = ["image_list", "image_snapshot", "image_status"]
