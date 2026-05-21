"""SIF-build + overlay-image helpers for the apptainer runtime.

Extracted from :mod:`_apptainer_runtime` (which orchestrates argv
assembly + lifecycle). These are pure-ish build helpers with no
dependency on :class:`ApptainerContainerRuntime`:

* :func:`_safe_image_tag` — hash an image reference to a cache filename.
* :func:`_create_overlay_image` — ``apptainer overlay create``.
* :func:`_build_sif_from_uri` — ``apptainer build <sif> <uri>``.
* :func:`_build_sif_from_def` — ``apptainer build <sif> <def>``.
"""

from __future__ import annotations

import hashlib
import re
import subprocess
from pathlib import Path

from ..config import AgentConfig


def resolve_sif(config: AgentConfig, cache_dir: Path) -> Path | None:
    """Resolve the local SIF path for ``config`` against ``cache_dir``.

    Pure resolution logic extracted from
    :meth:`ApptainerContainerRuntime.resolve_sif` (the method owns the
    apptainer-on-PATH guard + cache-dir creation, then delegates here).

    Resolution order:
      1. ``spec.apptainer.def_file`` — build from this .def.
      2. ``spec.image`` is a local ``.sif`` path — use directly.
      3. ``spec.image`` is a ``--sandbox`` dir — use directly.
      4. ``spec.image`` starts with ``docker://`` / ``oras://`` — cache + build.
      5. Bare image name — assume ``docker://``.

    Returns ``None`` on any unrecoverable error (build failed,
    unparseable image reference).
    """
    ap = getattr(config, "apptainer", None)
    def_file_str = getattr(ap, "def_file", "") if ap is not None else ""
    if def_file_str:
        def_file = Path(def_file_str).expanduser().resolve()
        if not def_file.is_file():
            return None
        sif_path = cache_dir / f"{_safe_image_tag(str(def_file))}.sif"
        if sif_path.is_file():
            return sif_path
        return sif_path if _build_sif_from_def(sif_path, def_file) else None

    # v3-realign: prefer spec.apptainer.image; fall back to legacy
    # AgentConfig.image (kept populated for back-compat) and finally
    # to the default sac-scitex SIF path.
    ap_image = getattr(ap, "image", "") if ap is not None else ""
    image = (ap_image or config.image or "").strip()
    if not image:
        return None

    if image.endswith(".sif"):
        sif_path = Path(image).expanduser().resolve()
        return sif_path if sif_path.is_file() else None

    # Sandbox image: a directory tree built via `apptainer build
    # --sandbox`. Used on hosts where /dev/fuse isn't exposed to
    # user namespaces (Spartan compute nodes etc.) — the rootfs
    # is a regular directory tree, no squashfuse needed at exec.
    # Detection: presence of the `.singularity.d/` marker dir.
    candidate = Path(image).expanduser()
    if candidate.is_dir() and (candidate / ".singularity.d").is_dir():
        return candidate.resolve()

    if image.startswith("docker://") or image.startswith("oras://"):
        sif_path = cache_dir / f"{_safe_image_tag(image)}.sif"
        if sif_path.is_file():
            return sif_path
        return sif_path if _build_sif_from_uri(sif_path, image) else None

    # Bare image name without a scheme — assume docker://.
    uri = f"docker://{image}"
    sif_path = cache_dir / f"{_safe_image_tag(uri)}.sif"
    if sif_path.is_file():
        return sif_path
    return sif_path if _build_sif_from_uri(sif_path, uri) else None


def _safe_image_tag(reference: str) -> str:
    """Hash an image reference / def-file path to a filename-safe tag.

    apptainer build emits a single .sif per (image, build-time) tuple;
    this hash gives us a deterministic filename so subsequent starts
    skip the rebuild. The full reference is preserved in the .sif
    metadata; the hash is just the cache key.
    """
    digest = hashlib.sha1(reference.encode("utf-8")).hexdigest()[:16]
    return digest


def _create_overlay_image(path: Path, size: str) -> None:
    """Create an apptainer overlay image at ``path`` with the given size.

    Size string accepts apptainer-style units with **M/MB/G/GB only**:
    ``"5G"``, ``"500M"``, ``"1024MB"`` etc. K/KB are explicitly
    rejected — ``apptainer overlay create --size`` takes integer MB so
    sub-MB granularity is unrepresentable. Parent dir is created if
    missing. Raises ``ValueError`` for unparseable / unsupported sizes
    and ``RuntimeError`` if the apptainer call itself fails.
    """
    m = re.match(r"^\s*(\d+)\s*([MG]B?)\s*$", size, re.IGNORECASE)
    if not m:
        raise ValueError(
            f"overlay_size {size!r} unparseable; use '5G', '500M', '1024MB' "
            "etc. (units M/MB/G/GB only — K/KB rejected because apptainer "
            "overlay create takes integer MB)."
        )
    n = int(m.group(1))
    unit = m.group(2).upper()
    # apptainer overlay create --size expects integer MB.
    multipliers = {"M": 1, "MB": 1, "G": 1024, "GB": 1024}
    if unit not in multipliers:
        # Defensive: regex already constrains to M/MB/G/GB, but if
        # someone ever broadens it without updating multipliers we
        # want a clear error, not a KeyError.
        raise ValueError(f"overlay_size unit {unit!r} unsupported")
    mb = int(n * multipliers[unit])
    if mb < 1:
        raise ValueError(f"overlay_size {size!r} resolves to <1MB")
    path.parent.mkdir(parents=True, exist_ok=True)
    result = subprocess.run(
        ["apptainer", "overlay", "create", "--size", str(mb), str(path)],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"apptainer overlay create failed (rc={result.returncode}): "
            f"{result.stderr.strip()}"
        )


def _build_sif_from_uri(sif_path: Path, uri: str) -> bool:
    """``apptainer build <sif> <uri>`` — pulls + converts an OCI image."""
    sif_path.parent.mkdir(parents=True, exist_ok=True)
    result = subprocess.run(["apptainer", "build", str(sif_path), uri])
    return result.returncode == 0


def _build_sif_from_def(sif_path: Path, def_file: Path) -> bool:
    """``apptainer build <sif> <def_file>`` — builds from a .def script.

    No docker daemon required even if the .def starts with
    ``Bootstrap: docker`` — apptainer's docker compatibility runs
    entirely over OCI registry pulls.
    """
    sif_path.parent.mkdir(parents=True, exist_ok=True)
    result = subprocess.run(["apptainer", "build", str(sif_path), str(def_file)])
    return result.returncode == 0


# ---------------------------------------------------------------------------
# Bus-auth bearer resolution (FIX 1)
# ---------------------------------------------------------------------------


def _listen_token_path() -> Path:
    """Canonical ``sac listen`` bearer token file for this host.

    Mirrors the resolver the listen server itself uses
    (:func:`_listen.tokens.default_token_path`,
    ``~/.scitex/agent-container/tokens/listen-<host>.token``) so the
    bearer we inject matches the one the bus validates against.
    """
    from .._listen.tokens import default_token_path

    return default_token_path()


def _read_listen_bearer() -> str | None:
    """Read the host bus bearer from the token file, or ``None``.

    Never raises — a missing/unreadable token file yields ``None`` so
    the caller can warn loudly (no silent fallback) and inject only the
    base URL.
    """
    from .._listen.tokens import read_token

    return read_token(_listen_token_path())


__all__ = [
    "resolve_sif",
    "_listen_token_path",
    "_read_listen_bearer",
    "_safe_image_tag",
    "_create_overlay_image",
    "_build_sif_from_uri",
    "_build_sif_from_def",
]
