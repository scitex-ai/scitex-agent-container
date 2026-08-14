"""Container image LAYER TOPOLOGY — which layers exist and what each one
bootstraps off.

One module owns the answer to "what is the stack?" so the CLI shape
(``image_group``), the staging/build path (``_image_source_build``) and the
reproducible round trip (``_image_repro_build``) all read the same map
instead of each carrying a partial copy.

The agent-runtime stack is a four-link chain::

    system-deps  ->  python-pkgs  ->  base  ->  scitex
    (ubuntu:24.04)   (venv+SAC)      (manifest) (scitex[all])

It was ONE recipe (``apptainer-base.def``, 898 lines) until 2026-08-14. The
split exists because the two halves of the old ``:base`` have very different
rebuild frequencies: the OS floor (apt + rustup + a source-built tree + a
cargo-built rtk) costs the bulk of the bake wall-clock and changes roughly
monthly, while the Python pin set above it (scitex-cards floors,
claude-agent-sdk floated to latest, sac's own bundled source) changes weekly.
Fused, every pin bump re-paid the whole apt/cargo cost. Split, a pin bump
rebuilds ``python-pkgs`` and reuses ``sac-system-deps.sif`` untouched.

``proxy`` is a layer you can build but is NOT part of the chain: it
bootstraps straight from the registry as a standalone sidecar image.
"""

from __future__ import annotations

from pathlib import Path

# Layer name → the .def filename that builds it. Recipes ship inside the
# wheel under ``containers/``.
LAYER_DEFS = {
    "system-deps": "apptainer-system-deps.def",
    "python-pkgs": "apptainer-python-pkgs.def",
    "base": "apptainer-base.def",
    "scitex": "apptainer-scitex.def",
    "proxy": "apptainer-proxy.def",
}

# The agent-runtime chain, bottom-up. ``proxy`` is deliberately absent.
STACK_ORDER = ("system-deps", "python-pkgs", "base", "scitex")

# Layer → the layer it bootstraps off. Only ``system-deps`` bootstraps from a
# registry image; every other link in the chain starts ``From:
# ./sac-<parent>.sif``. An ABSENT key means "no prerequisite" — the correct
# answer for both ``system-deps`` (bottom of the stack) and ``proxy`` (not in
# the stack at all), which is why this is a lookup with a default rather than
# an exhaustive map.
BOOTSTRAP_PARENT = {
    "python-pkgs": "system-deps",
    "base": "python-pkgs",
    "scitex": "base",
}


class BootstrapSifMissing(FileNotFoundError):
    """Raised when a layered build's prerequisite SIF is absent.

    Carries the fail-loud remediation text the CLI surfaces verbatim, so
    the layer→prerequisite policy lives with the topology rather than
    inline in the ``sac image build`` command.
    """


def resolve_bootstrap_sif(layer: str, output_dir: Path) -> Path | None:
    """Return the prerequisite SIF a layered ``.def`` bootstraps off.

    Layered .defs start ``From: ./sac-<parent>.sif`` — a path RELATIVE to
    the build-context dir. The prerequisite is the parent layer's STABLE
    inner boot symlink, ``<output_dir>/sac-<parent>/sac-<parent>.sif`` (a
    symlink to the live timestamped SIF under scitex-container 0.3.0's
    atomic layout). ``build_layer_from_source`` symlinks it into the
    staging dir so apptainer's relative ``From:`` resolves at build time.

    Returns ``None`` for layers that bootstrap off a registry image rather
    than a prior SIF: ``system-deps`` (bottom of the chain) and ``proxy``
    (a standalone sidecar, not part of the chain).

    Raises
    ------
    BootstrapSifMissing
        When a layered build is requested but the prerequisite SIF has not
        been built. Fails loud BEFORE staging so apptainer never FATAL's
        on a half-staged context (the 2026-06-07 cohort-A rebuild stall).
        The exception message names the missing path AND the remediation
        command — and, because the chain is now four links deep, it names
        the IMMEDIATE parent rather than always pointing at ``base``.
    """
    parent = BOOTSTRAP_PARENT.get(layer)
    if parent is None:
        return None
    bootstrap_sif = output_dir / f"sac-{parent}" / f"sac-{parent}.sif"
    if not bootstrap_sif.is_file():
        raise BootstrapSifMissing(
            f"{layer} layer requires a built sac-{parent}.sif at "
            f"{bootstrap_sif}; build the {parent} layer first:\n"
            f"  $ sac image build {parent} -y\n"
            f"then retry `sac image build {layer} -y`."
        )
    return bootstrap_sif


__all__ = [
    "LAYER_DEFS",
    "STACK_ORDER",
    "BOOTSTRAP_PARENT",
    "BootstrapSifMissing",
    "resolve_bootstrap_sif",
]
