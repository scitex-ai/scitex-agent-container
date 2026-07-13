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
import os
import pwd
import re
import subprocess
from pathlib import Path

from ..config import AgentConfig

# ---------------------------------------------------------------------------
# Fakeroot auto-detection for SIF builds (operator gotcha 2026-06-03)
#
# ``apptainer build`` on a non-setuid install falls back to ``sudo
# apptainer build`` when the caller isn't root + ``--fakeroot`` isn't
# requested. ``sudo`` then prompts for a password, which fails in any
# headless / cron / agent / detached context. The result is a silent
# "build failed" with a non-actionable error in the build log.
#
# When the host already has fakeroot mappings configured for the
# current user (``/etc/subuid`` + ``/etc/subgid`` carry an entry), the
# user-namespace path works without sudo — we just have to ask for it
# via ``--fakeroot``. This helper detects that situation so the build
# argv carries the flag automatically.
#
# Test seams (``euid``, ``subuid_path``, ``subgid_path``) keep the
# probe injectable from tests without monkeypatching ``os.geteuid`` or
# ``/etc/subuid``.
# ---------------------------------------------------------------------------


def _has_subid_entry(path: Path, username: str) -> bool:
    """Return ``True`` iff ``path`` (a ``/etc/sub{u,g}id``-shaped file)
    has a mapping entry for ``username``.

    Format per ``shadow-utils``: ``<username>:<start_id>:<count>`` per
    line; the only check sac needs is "does this user have ANY
    mapping" — the kernel handles the rest.
    """
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.startswith(username + ":"):
                return True
    except (FileNotFoundError, OSError):
        pass
    return False


def _should_use_fakeroot_for_build(
    *,
    euid: int | None = None,
    subuid_path: Path | None = None,
    subgid_path: Path | None = None,
) -> bool:
    """Auto-detect whether ``apptainer build`` needs ``--fakeroot``.

    Returns ``True`` iff:

    * The current effective UID is NOT root (real root doesn't need
      fakeroot — the OS already lets it create namespaces directly),
    * AND the user's ``/etc/subuid`` entry exists,
    * AND the user's ``/etc/subgid`` entry exists.

    A returning ``True`` means the build argv should add ``--fakeroot``
    so apptainer uses the user-namespace path instead of falling back
    to ``sudo`` (which prompts for a password in any non-interactive
    context — agents, cron, detached lead, etc.).

    Test seams (``euid``, ``subuid_path``, ``subgid_path``) let the
    probe run against fake values without monkeypatching the real
    syscalls / files.
    """
    real_euid = euid if euid is not None else os.geteuid()
    if real_euid == 0:
        return False

    try:
        username = pwd.getpwuid(real_euid).pw_name
    except KeyError:
        return False

    sub_uid = subuid_path if subuid_path is not None else Path("/etc/subuid")
    sub_gid = subgid_path if subgid_path is not None else Path("/etc/subgid")
    return _has_subid_entry(sub_uid, username) and _has_subid_entry(sub_gid, username)


def _build_argv_prefix() -> list[str]:
    """Return the demoted ``apptainer build`` argv head.

    ``nice -n 19 ionice -c 2 -n 7 apptainer build [--fakeroot]`` — the
    low-priority prefix (incident-local-heavy-build: a full SIF bake at
    normal priority starved the operator's loaded interactive host)
    demotes ONLY the spawned build subprocess; the calling process — and
    the agent container it goes on to launch — keeps normal priority.
    IO runs at best-effort lowest, NOT the idle class: idle-class IO
    starved/killed a real mksquashfs stage under sustained load (see
    :mod:`scitex_agent_container._build_priority`). Degrades to
    nice-only when ``ionice`` is off PATH, and to no prefix at all
    under ``SAC_BUILD_NO_NICE=1``.

    Used by both :func:`_build_sif_from_def` and
    :func:`_build_sif_from_uri` so the fakeroot + priority logic stays
    in one place.
    """
    from .._build_priority import low_priority_build_prefix

    argv = low_priority_build_prefix() + ["apptainer", "build"]
    if _should_use_fakeroot_for_build():
        argv.append("--fakeroot")
    return argv


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
    """``apptainer build <sif> <uri>`` — pulls + converts an OCI image.

    Returns ``True`` on success. Raises :class:`RuntimeError` carrying
    the apptainer stderr verbatim on non-zero rc — backlog #4 fail-loud
    contract. The pre-fix shape was ``return result.returncode == 0``
    which silently dropped the stderr; callers saw only ``False`` and
    the operator had no diagnostic about why the build failed
    (network, missing registry credentials, malformed reference, ...).
    """
    sif_path.parent.mkdir(parents=True, exist_ok=True)
    argv = _build_argv_prefix() + [str(sif_path), uri]
    result = subprocess.run(argv, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(
            f"apptainer build {uri!r} → {sif_path} failed "
            f"(rc={result.returncode}). stderr:\n{result.stderr.strip()}\n"
            f"stdout:\n{result.stdout.strip()}"
        )
    return True


def _build_sif_from_def(sif_path: Path, def_file: Path) -> bool:
    """``apptainer build <sif> <def_file>`` — builds from a .def script.

    No docker daemon required even if the .def starts with
    ``Bootstrap: docker`` — apptainer's docker compatibility runs
    entirely over OCI registry pulls.

    Auto-adds ``--fakeroot`` via :func:`_build_argv_prefix` when the
    current user has ``/etc/subuid`` + ``/etc/subgid`` mappings — see
    that function's docstring. Avoids the silent "sudo: a password is
    required" failure in headless / agent / cron contexts when the
    host's apptainer install isn't setuid.

    Returns ``True`` on success. Raises :class:`RuntimeError` carrying
    the apptainer stderr verbatim on non-zero rc — backlog #4 fail-loud
    contract (see :func:`_build_sif_from_uri` for the same rationale).
    """
    sif_path.parent.mkdir(parents=True, exist_ok=True)
    argv = _build_argv_prefix() + [str(sif_path), str(def_file)]
    result = subprocess.run(argv, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(
            f"apptainer build {def_file} → {sif_path} failed "
            f"(rc={result.returncode}). stderr:\n{result.stderr.strip()}\n"
            f"stdout:\n{result.stdout.strip()}"
        )
    return True


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
    "_build_argv_prefix",
    "_build_sif_from_def",
    "_build_sif_from_uri",
    "_create_overlay_image",
    "_has_subid_entry",
    "_listen_token_path",
    "_read_listen_bearer",
    "_safe_image_tag",
    "_should_use_fakeroot_for_build",
]
