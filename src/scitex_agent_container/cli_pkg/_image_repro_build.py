"""Reproducible round-trip build for ``sac image build --reproducible``.

``sac image build`` has always produced a SIF and nothing that PROVES the
SIF can be produced again. scitex-container has shipped the proof machinery
for months — rough build → freeze the actually-installed versions → emit a
version-pinned ``.def`` → rebuild from it → compare the two version sets →
mark ``.verified`` / ``.unverified`` — and sac never called it. This module
is the call.

Why it could not simply be called before: the round trip took no build
CONTEXT. sac's whole contribution to a build is a STAGED context (a copy of
its own source tree beside the ``.def``, plus a symlink to the prerequisite
layer's SIF), because the shipped recipes pull sac in by a relative path::

    %files
        scitex-agent-container-src /opt/scitex-agent-container-src

    Bootstrap: localimage
    From: ./sac-base.sif

Those paths exist only inside the staging directory ``stage_build_context``
creates. Without a ``cwd`` argument reaching apptainer, the round trip
resolved them against the containers dir, where they do not exist, and the
build FATAL'd before running a line of ``%post``. That is also the
mechanism behind the operator's long-standing observation that rebuilding
by hand goes wrong while building through ``sac`` works: a raw ``apptainer
build`` on a shipped ``.def`` fails immediately for exactly this reason.
scitex-container grew the ``cwd`` parameter for this; here it is used.

WHAT "REPRODUCIBLE" MEANS HERE — the operator chose, explicitly, between
two readings:

  A. ENVIRONMENT IDENTITY — the same version set comes back. CHOSEN.
  B. Byte-for-byte identical digests. OUT OF SCOPE.

So ``SOURCE_DATE_EPOCH`` and squashfs timestamp determinism are deliberately
not attempted; scitex-container's own design calls byte-identity "an
OPTIONAL stretch, deliberately NOT the default gate". A mismatch is NOT a
build failure: the rough SIF stays usable and is marked ``.unverified``
carrying the drift, because an image you can use with a known-unproven
provenance is strictly better than no image and a red X.

Testability follows the same seam pattern as ``_image_source_build``:
``_container_build_reproducible`` is a module-level callable that tests
reassign to a real (no MagicMock) recording fake.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

from ._image_source_build import stage_build_context


def _default_container_build_reproducible(
    *,
    def_path: Path,
    output_dir: Path,
    cwd: Path,
    image_name: str,
    force: bool,
    verify: bool,
) -> Any:
    """Default round-trip builder — delegates to scitex-container.

    ``cwd`` is the staged build context, forwarded to BOTH the rough build
    and the verify rebuild so the replay resolves the same staged inputs
    the rough build did. ``layer`` in scitex-container's vocabulary is the
    artifact stem, which is sac's ``image_name`` (``sac-base`` etc.).

    Returns scitex-container's ``RoundTripResult``: the kept artifact paths
    plus ``verified`` (True / False / None when verify was skipped) and the
    ``diff`` naming what drifted.
    """
    # Local import — scitex-container pulls its own deps and we don't want
    # to pay that on the cold ``sac image`` startup path until a build runs.
    from scitex_container import build_reproducible as _sc_build_reproducible

    return _sc_build_reproducible(
        layer=image_name,
        root=output_dir,
        def_path=def_path,
        cwd=cwd,
        verify=verify,
        force=force,
    )


# Module-level overridable reference — same swap-and-restore pattern as
# ``_image_source_build._container_build``. Tests reassign this to a real
# recording callable so the unit suite never shells a real apptainer.
_container_build_reproducible: Callable[..., Any] = (
    _default_container_build_reproducible
)


def build_layer_reproducible(
    *,
    layer: str,
    def_path: Path,
    pkg_root: Path,
    output_dir: Path,
    force: bool = True,
    bootstrap_sif: Path | None = None,
    verify: bool = True,
) -> Any:
    """Build a sac SIF through scitex-container's reproducible round trip.

    Stages the build context exactly as the plain source build does (same
    ``stage_build_context``, same staging directory), then hands that
    directory to the round trip as the build ``cwd``.

    The staging directory must OUTLIVE the rough build, because the verify
    rebuild replays the generated locked ``.def`` — which is the rough
    ``.def`` plus a pin stanza and therefore carries the identical relative
    ``%files`` / ``From:`` references — against the same context. It does:
    nothing removes the staging dir between the two builds, and the next
    build resets it.

    Parameters
    ----------
    layer : str
        Layer name (``base`` / ``scitex`` / ``proxy``), mapped to the
        ``sac-<layer>`` artifact stem.
    def_path : Path
        Source .def file. Copied (not modified) into the staging dir.
    pkg_root : Path
        Package source root, staged as ``scitex-agent-container-src/``.
    output_dir : Path
        Containers dir. Artifacts land under ``<output_dir>/sac-<layer>/``.
    force : bool
        Force a rebuild even when the recipe hash is unchanged.
    bootstrap_sif : Path | None
        Prerequisite SIF for a layered .def, symlinked into the staging dir
        so ``From: ./<name>.sif`` resolves. ``None`` for top-of-stack defs.
    verify : bool
        Run the verify rebuild + version-set comparison inline. False
        captures the lock and the locked def but leaves the build UNMARKED
        — useful when the second build is being scheduled separately, and
        honest about it: an unmarked build is treated as unverified by the
        use-time gate.

    Returns
    -------
    RoundTripResult
        scitex-container's result object: ``sif`` / ``lock`` / ``locked_def``
        / ``verified`` / ``diff`` / ``marker``.

    Raises
    ------
    RuntimeError
        Propagated when the underlying apptainer build fails. A round-trip
        MISMATCH does NOT raise — it marks ``.unverified`` and returns.
    FileNotFoundError
        Propagated from :func:`stage_build_context` if inputs are missing.
    """
    artifact_dir = output_dir / f"sac-{layer}"
    artifact_dir.mkdir(parents=True, exist_ok=True)

    staging_dir = artifact_dir / "build-context"
    staged_def = stage_build_context(
        pkg_root, def_path, staging_dir, bootstrap_sif=bootstrap_sif
    )

    return _container_build_reproducible(
        def_path=staged_def,
        output_dir=output_dir,
        cwd=staging_dir,
        image_name=f"sac-{layer}",
        force=force,
        verify=verify,
    )


def describe_result(result: Any) -> list[str]:
    """Render a round-trip result as operator-facing lines.

    Kept beside the build so the CLI verb stays a thin dispatcher, and so
    the *unverified* case reads as a finding rather than a stack trace: the
    drift summary names what moved between the two builds.
    """
    lines = [f"artifact  {result.sif}", f"lock      {result.lock}"]
    lines.append(f"recipe    {result.locked_def}")
    if result.verified is None:
        lines.append(
            "verify    SKIPPED — build is UNMARKED, so the use-time gate "
            "treats it as unverified"
        )
        return lines
    if result.verified:
        lines.append("verify    VERIFIED — rebuild produced the same version set")
        lines.append(f"marker    {result.marker}")
        return lines
    detail = result.diff.summary() if result.diff is not None else "unknown drift"
    lines.append(f"verify    MISMATCH — {detail}")
    lines.append(f"marker    {result.marker}")
    lines.append(
        "          the image is USABLE but its reproducibility is unproven; "
        "the drift above is what a rebuild changed"
    )
    return lines


def validate_flags(
    *, reproducible: bool, sandbox: bool, skip_verify: bool
) -> str | None:
    """Return an error message for an impossible flag combination, else None.

    Kept out of the verb so ``sac image build`` stays a dispatcher, and out
    of ``build_layer_reproducible`` so the refusal happens BEFORE any
    staging work (a build context is an rm -rf + a full source copytree).
    """
    if reproducible and sandbox:
        return (
            "--reproducible and --sandbox are mutually exclusive. The round "
            "trip compares two immutable SIFs; a sandbox is a mutable "
            "directory with nothing to compare."
        )
    if skip_verify and not reproducible:
        return "--skip-verify only applies with --reproducible."
    return None


def run_build(
    *,
    layer: str,
    def_path: Path,
    pkg_root: Path,
    output_dir: Path,
    bootstrap_sif: Path | None,
    verify: bool,
) -> Any:
    """Run the round trip for the CLI and report it. Exits non-zero on failure.

    This is the verb's whole reproducible branch, lifted here so
    ``image_group`` stays a dispatcher. It owns the operator-facing
    reporting because the interesting outcome is not "a file appeared" but
    WHETHER THE VERSION SETS MATCHED — and, when they did not, what moved.
    """
    import sys

    import click

    from ._helpers import console

    try:
        result = build_layer_reproducible(
            layer=layer,
            def_path=def_path,
            pkg_root=pkg_root,
            output_dir=output_dir,
            force=True,
            bootstrap_sif=bootstrap_sif,
            verify=verify,
        )
    except (FileNotFoundError, RuntimeError) as exc:
        click.echo(f"error: apptainer build failed: {exc}", err=True)
        sys.exit(1)
    console.print(f"[green]built[/green] {result.sif}")
    for line in describe_result(result):
        click.echo(line)
    return result


__all__ = [
    "build_layer_reproducible",
    "describe_result",
    "run_build",
    "validate_flags",
    "_default_container_build_reproducible",
    "_container_build_reproducible",
]
