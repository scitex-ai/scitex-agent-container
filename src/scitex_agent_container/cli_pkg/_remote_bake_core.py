"""Core logic of the periodic Spartan SIF bake → master pull pipeline.

Pure (click-free) legs of ``sac image bake-remote``: verdict parsing, the
pull/verify/swap/prune chain, and the shared subprocess seam. The CLI
wrapper and the remote-bake ssh invocation live in
``_image_remote_bake.py``; the remote-side script itself ships in the
wheel at ``containers/spartan-sif-bake.sh``.

OPERATOR DIRECTIVE (2026-07-17, verbatim): 「sif は最新版を定期焼きにしましょう。
spartan 側で。それでこちらには定期的に rsync する形で。どうでしょうか。cpu は
使わずに新しいものが得られると思います。」 — bake fresh SIFs periodically ON
SPARTAN, rsync the result to the master; the master gets fresh images
without spending its own CPU.

Every leg is THREE-STATE and loud (no silent fallbacks):

* a remote run with no ``SAC_BAKE_RESULT`` line is ``NO_RESULT`` (the
  script died), never a soft failure and never an ok;
* a checksum that could not be compared is a FAILURE, not a pass;
* a symbol probe that could not RUN is a FAILURE ("gate-not-run is not
  gate-passed") — the artifact stays unpublished and the live symlinks
  untouched.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

# ---------------------------------------------------------------------------
# Wheel-shipped assets (same package-relative convention as image_group's
# _RECIPES_DIR — the recipes, the remote script and the probe travel
# together in the wheel).
# ---------------------------------------------------------------------------
_CONTAINERS_ASSETS = Path(__file__).resolve().parent.parent / "containers"
BAKE_SCRIPT = _CONTAINERS_ASSETS / "spartan-sif-bake.sh"
SYMBOL_PROBE = _CONTAINERS_ASSETS / "sif-symbol-probe.py"

LAYERS = ("base", "scitex")

# Timestamped artifact name, e.g. sac-scitex-2026-0717-092952.sif —
# matches scitex-container's ``_store`` timestamp shape.
SIF_RE = re.compile(r"^sac-(?P<layer>base|scitex)-(?P<ts>\d{4}-\d{4}-\d{6})\.sif$")

# Module-level seams (save/restore in tests, same pattern as image_group's
# _load_apptainer): every subprocess this pipeline spawns goes through
# ``_run``, and every binary lookup through ``_which`` — the test host (a
# SIF without rsync, say) must not decide what the production host can do.
_run = subprocess.run
_which = shutil.which


class BakeVerdict(str, Enum):
    """Remote bake outcome. NO_RESULT is a refusal to guess, never an ok."""

    BAKED = "BAKED"
    SKIPPED = "SKIPPED"
    FAILED = "FAILED"
    NO_RESULT = "NO_RESULT"


@dataclass(frozen=True)
class RemoteBakeOutcome:
    """The parsed ``SAC_BAKE_RESULT`` verdict of one remote bake leg."""

    verdict: BakeVerdict
    layer: str
    sif: str = ""
    sha256: str = ""
    head: str = ""
    detail: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.verdict, BakeVerdict):
            raise ValueError(f"verdict must be BakeVerdict, got {self.verdict!r}")
        if self.layer not in LAYERS and self.layer != "unset":
            raise ValueError(f"unknown layer {self.layer!r}")
        if self.verdict in (BakeVerdict.BAKED, BakeVerdict.SKIPPED):
            # A green with no artifact identity is not a green.
            if not self.sif or not self.sha256:
                raise ValueError(
                    f"{self.verdict.value} verdict must carry sif+sha256 "
                    f"(got sif={self.sif!r} sha256={self.sha256!r})"
                )


def parse_bake_result(output: str, *, layer: str) -> RemoteBakeOutcome:
    """Parse the LAST ``SAC_BAKE_RESULT={...}`` line of the remote output.

    No line at all means the remote script died before reaching a verdict
    — that is ``NO_RESULT``, a distinct loud state, never a soft failure.
    """
    lines = [
        ln[len("SAC_BAKE_RESULT=") :]
        for ln in output.splitlines()
        if ln.startswith("SAC_BAKE_RESULT=")
    ]
    if not lines:
        return RemoteBakeOutcome(
            verdict=BakeVerdict.NO_RESULT,
            layer=layer,
            detail="remote script emitted no SAC_BAKE_RESULT line (died mid-flight)",
        )
    try:
        payload = json.loads(lines[-1])
    except json.JSONDecodeError as exc:
        return RemoteBakeOutcome(
            verdict=BakeVerdict.NO_RESULT,
            layer=layer,
            detail=f"unparseable SAC_BAKE_RESULT line: {exc}",
        )
    verdict = BakeVerdict(payload.get("verdict", "NO_RESULT"))
    return RemoteBakeOutcome(
        verdict=verdict,
        layer=payload.get("layer", layer),
        sif=payload.get("sif", ""),
        sha256=payload.get("sha256", ""),
        head=payload.get("head", ""),
        detail=payload.get("reason", "") or payload.get("step", ""),
    )


# ---------------------------------------------------------------------------
# Pull + verify + swap + prune (the master-side legs)
# ---------------------------------------------------------------------------
class PullVerdict(str, Enum):
    SWAPPED = "SWAPPED"  # new artifact verified and made live
    UP_TO_DATE = "UP_TO_DATE"  # artifact already live locally
    FAILED = "FAILED"


@dataclass(frozen=True)
class PullOutcome:
    verdict: PullVerdict
    layer: str
    detail: str

    def __post_init__(self) -> None:
        if not isinstance(self.verdict, PullVerdict):
            raise ValueError(f"verdict must be PullVerdict, got {self.verdict!r}")
        if not self.detail:
            raise ValueError("PullOutcome.detail must state the evidence")


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _atomic_symlink(link: Path, target: str) -> None:
    """Point ``link`` at ``target`` atomically (temp symlink + rename)."""
    tmp = link.parent / f".{link.name}.tmp.{os.getpid()}"
    if tmp.is_symlink() or tmp.exists():
        tmp.unlink()
    tmp.symlink_to(target)
    os.replace(tmp, link)


def swap_live_symlinks(containers_dir: Path, layer: str, sif_name: str) -> None:
    """Flip BOTH stable symlinks to ``sif_name``, mirroring the live store.

    * inner boot symlink  ``sac-<layer>/sac-<layer>.sif -> <sif_name>``
    * top-level symlink   ``sac-<layer>.sif -> sac-<layer>/<sif_name>``
      (what a layered .def's ``From: ./sac-<layer>.sif`` and the agent
      launch path resolve).
    """
    if not SIF_RE.match(sif_name):
        raise ValueError(f"refusing to publish non-canonical sif name {sif_name!r}")
    layer_dir = containers_dir / f"sac-{layer}"
    if not (layer_dir / sif_name).is_file():
        raise FileNotFoundError(
            f"cannot publish missing artifact {layer_dir / sif_name}"
        )
    _atomic_symlink(layer_dir / f"sac-{layer}.sif", sif_name)
    _atomic_symlink(containers_dir / f"sac-{layer}.sif", f"sac-{layer}/{sif_name}")


def prune_local(containers_dir: Path, layer: str, retain: int) -> list[str]:
    """Keep the ``retain`` newest timestamped SIFs; NEVER a live symlink
    target; return the pruned names (caller echoes them — no silent prune)."""
    layer_dir = containers_dir / f"sac-{layer}"
    live: set[Path] = set()
    for link in (layer_dir / f"sac-{layer}.sif", containers_dir / f"sac-{layer}.sif"):
        if link.is_symlink():
            live.add(link.resolve())
    candidates = sorted(
        (p for p in layer_dir.glob(f"sac-{layer}-*.sif") if SIF_RE.match(p.name)),
        key=lambda p: p.name,
        reverse=True,  # timestamp shape is lexicographically chronological
    )
    pruned: list[str] = []
    kept = 0
    for sif in candidates:
        if sif.resolve() in live or kept < retain:
            kept += 1
            continue
        sif.unlink()
        sidecar = Path(str(sif) + ".sha256")
        if sidecar.is_file():
            sidecar.unlink()
        pruned.append(sif.name)
    return pruned


def pull_and_publish(
    *,
    host: str,
    outcome: RemoteBakeOutcome,
    containers_dir: Path,
    retain: int,
    apptainer: str | None = None,
) -> PullOutcome:
    """rsync the gated SIF from the remote store, re-verify it HERE
    (checksum + symbol probe), then atomically swap the live symlinks.

    Transport is a second place to rot: the master never trusts the
    remote's green — it re-checks the checksum against the remote sidecar
    value and re-runs the symbol probe on the received file before any
    symlink moves. A failed verify leaves the live image untouched.
    """
    layer = outcome.layer
    sif_name = Path(outcome.sif).name
    if not SIF_RE.match(sif_name):
        return PullOutcome(
            PullVerdict.FAILED,
            layer,
            f"remote reported non-canonical name {sif_name!r}",
        )
    layer_dir = containers_dir / f"sac-{layer}"
    layer_dir.mkdir(parents=True, exist_ok=True)
    final = layer_dir / sif_name

    top = containers_dir / f"sac-{layer}.sif"
    if final.is_file() and top.is_symlink() and top.resolve() == final.resolve():
        if _sha256_file(final) == outcome.sha256:
            return PullOutcome(
                PullVerdict.UP_TO_DATE,
                layer,
                f"{sif_name} already live and checksum-matched",
            )
        return PullOutcome(
            PullVerdict.FAILED,
            layer,
            f"{sif_name} is live locally but its checksum DIFFERS from the "
            "remote sidecar — refusing to guess which is right",
        )

    rsync = _which("rsync")
    if rsync is None:
        return PullOutcome(PullVerdict.FAILED, layer, "rsync not found on this host")
    incoming = layer_dir / f".incoming-{sif_name}"
    proc = _run(
        [
            rsync,
            "-e",
            "ssh -o BatchMode=yes",
            "--partial",
            f"{host}:{outcome.sif}",
            str(incoming),
        ],
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0 or not incoming.is_file():
        return PullOutcome(
            PullVerdict.FAILED,
            layer,
            f"rsync rc={proc.returncode}: "
            f"{proc.stderr.strip() or 'no artifact landed'}",
        )

    got = _sha256_file(incoming)
    if got != outcome.sha256:
        # Keep the partial for --partial resume; it can never be mistaken
        # for an artifact under its dot-name.
        return PullOutcome(
            PullVerdict.FAILED,
            layer,
            f"checksum mismatch after transfer: remote={outcome.sha256} local={got}",
        )

    apptainer = apptainer or _which("apptainer")
    if apptainer is None:
        return PullOutcome(
            PullVerdict.FAILED,
            layer,
            "apptainer not found on this host — symbol probe CANNOT run, "
            "artifact stays unpublished (gate-not-run is not gate-passed)",
        )
    if not SYMBOL_PROBE.is_file():
        return PullOutcome(
            PullVerdict.FAILED,
            layer,
            f"symbol probe missing from wheel: {SYMBOL_PROBE}",
        )
    with tempfile.TemporaryDirectory(prefix="sac-sif-probe-") as td:
        probe = Path(td) / "sif-symbol-probe.py"
        shutil.copy2(SYMBOL_PROBE, probe)
        proc = _run(
            [
                apptainer,
                "exec",
                "--bind",
                td,
                str(incoming),
                "/opt/venv-sac/bin/python",
                str(probe),
            ],
            capture_output=True,
            text=True,
        )
    if proc.returncode != 0:
        return PullOutcome(
            PullVerdict.FAILED,
            layer,
            "symbol probe FAILED on the pulled artifact "
            f"(rc={proc.returncode}): {(proc.stderr or proc.stdout).strip()}",
        )

    os.replace(incoming, final)  # same-dir rename: atomic
    Path(str(final) + ".sha256").write_text(f"{outcome.sha256}  {sif_name}\n")
    swap_live_symlinks(containers_dir, layer, sif_name)
    pruned = prune_local(containers_dir, layer, retain)
    detail = f"published {sif_name} and flipped both live symlinks"
    if pruned:
        detail += f"; pruned {', '.join(pruned)}"
    return PullOutcome(PullVerdict.SWAPPED, layer, detail)


__all__ = [
    "BAKE_SCRIPT",
    "BakeVerdict",
    "LAYERS",
    "PullOutcome",
    "PullVerdict",
    "RemoteBakeOutcome",
    "SIF_RE",
    "SYMBOL_PROBE",
    "parse_bake_result",
    "prune_local",
    "pull_and_publish",
    "swap_live_symlinks",
]
