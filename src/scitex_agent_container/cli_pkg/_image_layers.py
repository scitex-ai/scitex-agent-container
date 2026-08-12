"""The image chain — ONE definition, read by every site that needs it.

sac used to hardcode a two-layer ``base → scitex`` chain in FOUR places that
did not read each other:

  * ``image_group._LAYERS``            (layer → .def filename)
  * ``_image_source_build.resolve_bootstrap_sif``  (``if layer != "scitex"``)
  * ``_remote_bake_core.LAYERS`` + ``SIF_RE``      (a ``base|scitex`` regex)
  * ``containers/spartan-sif-bake.sh``            (a ``base|scitex`` case gate)

Four independent spellings of one fact is four chances to update three of
them, and three-of-four is worse than none: the two that agree look like
confirmation. This module is that fact, stated once. Every site above now
reads it (the shell script gets it via the CLI, which prints the table).

WHY A SEPARATE MODULE rather than living in ``image_group``: ``image_group``
imports ``_image_source_build``, so the table cannot live in ``image_group``
without ``_image_source_build`` importing it back — a cycle. Bottom of the
dependency order is the only honest home for shared data. This module imports
nothing from its siblings, on purpose; keep it that way.

THE CHAIN (operator instruction 2026-08-12 — build progressively, "like a
rocket pencil"):

    01-system-deps → 02-python-pkgs → 03-base → 04-scitex

plus ``proxy``, which is OFF the chain by design: it bootstraps straight from
the ubuntu digest so it inherits none of the chain's weight. It is still ours
and still built by the same CLI — anything chain-shaped that silently skips it
is a bug, which is why it is IN this table rather than special-cased outside.

ARTIFACT NAMES AND THE COMPAT ALIASES. Stage 03 publishes ``sac-03-base.sif``
and stage 04 ``sac-04-scitex.sif``. Live agent specs hardcode the ABSOLUTE
paths of ``sac-base.sif`` / ``sac-scitex.sif`` — 65+ of them on this host
alone, and the spec census counts 157 references to the base image across 114
specs. Renaming without an alias is a fleet-wide outage, not a rename, so each
renamed stage carries a ``legacy_image`` and the build publishes a symlink
under the old name beside the new artifact. The aliases are also accepted as
CLI layer names, so every doc, example and muscle-memorised ``sac image build
base -y`` keeps working.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field


@dataclass(frozen=True)
class Layer:
    """One buildable stage: its recipe, its parent, and what it publishes."""

    name: str
    """Canonical stage name — also the CLI argument and the artifact suffix."""

    parent: str | None
    """Name of the stage this bootstraps from (``Bootstrap: localimage``).

    ``None`` means the recipe starts from a registry image
    (``Bootstrap: docker``) and has no prerequisite SIF.
    """

    legacy_image: str | None = None
    """Pre-split artifact stem kept alive as a symlink beside the new one.

    Set only where a rename would break something already deployed.
    """

    aliases: tuple[str, ...] = field(default_factory=tuple)
    """Names the CLI still accepts for this stage (docs, habit, old scripts)."""

    remote_bakeable: bool = True
    """Whether ``sac image bake-remote`` may bake this stage on Spartan."""

    @property
    def def_name(self) -> str:
        """Recipe filename shipped in the wheel under ``containers/``."""
        return f"apptainer-{self.name}.def"

    @property
    def image(self) -> str:
        """Published artifact stem: ``<image>.sif`` / ``<image>.sandbox``."""
        return f"sac-{self.name}"


# Declaration order IS build order. Anything that iterates the chain relies on
# it (``chain_for``, the ``--chain`` build, the CLI's choice list).
_LAYER_LIST: tuple[Layer, ...] = (
    Layer(name="01-system-deps", parent=None),
    Layer(name="02-python-pkgs", parent="01-system-deps"),
    Layer(
        name="03-base",
        parent="02-python-pkgs",
        legacy_image="sac-base",
        aliases=("base",),
    ),
    Layer(
        name="04-scitex",
        parent="03-base",
        legacy_image="sac-scitex",
        aliases=("scitex",),
    ),
    # OFF the chain: bootstraps from the ubuntu digest, feeds nothing.
    # Never remote-baked (it never was), but it IS a first-class layer of the
    # CLI — a list that forgets it leaves sac shipping a recipe nothing builds.
    Layer(name="proxy", parent=None, remote_bakeable=False),
)

LAYERS: dict[str, Layer] = {layer.name: layer for layer in _LAYER_LIST}

DEFAULT_LAYER = "03-base"

# alias → canonical name. Kept separate from LAYERS so ``list(LAYERS)`` is the
# canonical set and never accidentally advertises an alias as a stage.
ALIASES: dict[str, str] = {
    alias: layer.name for layer in _LAYER_LIST for alias in layer.aliases
}

#: Every name the CLI accepts, canonical first.
ACCEPTED_NAMES: tuple[str, ...] = tuple(LAYERS) + tuple(ALIASES)

#: Stages ``sac image bake-remote`` may bake, in build order.
BAKEABLE: tuple[str, ...] = tuple(
    layer.name for layer in _LAYER_LIST if layer.remote_bakeable
)


class UnknownLayer(KeyError):
    """Raised for a layer name that is neither canonical nor an alias.

    Carries a human-readable message naming the valid set — callers surface it
    verbatim rather than inventing their own wording.
    """


def resolve(name: str) -> Layer:
    """Canonical name or alias → the :class:`Layer`.

    Raises :class:`UnknownLayer` (never returns a default) — guessing which
    layer the caller meant is exactly how a build lands in the wrong place.
    """
    canonical = ALIASES.get(name, name)
    try:
        return LAYERS[canonical]
    except KeyError:
        raise UnknownLayer(
            f"Unknown layer {name!r}. Choose from: {', '.join(LAYERS)} "
            f"(aliases: {', '.join(ALIASES) or 'none'})"
        ) from None


def canonical_name(name: str) -> str:
    """Canonical name or alias → the canonical name."""
    return resolve(name).name


def parent_of(name: str) -> Layer | None:
    """The stage ``name`` bootstraps from, or ``None`` for a registry root."""
    parent = resolve(name).parent
    return LAYERS[parent] if parent else None


def chain_for(name: str) -> tuple[Layer, ...]:
    """Every stage that must exist for ``name``, in BUILD order, ending at it.

    ``chain_for("04-scitex")`` is (01, 02, 03, 04); ``chain_for("proxy")`` is
    just (proxy,), because proxy has no parent. Cycle-safe: a malformed table
    raises rather than looping forever.
    """
    layer = resolve(name)
    ordered: list[Layer] = []
    seen: set[str] = set()
    while layer is not None:
        if layer.name in seen:
            raise ValueError(
                f"layer chain cycles at {layer.name!r} — the table in "
                f"{__name__} is malformed"
            )
        seen.add(layer.name)
        ordered.append(layer)
        layer = LAYERS[layer.parent] if layer.parent else None
    return tuple(reversed(ordered))


def artifact_re() -> re.Pattern[str]:
    """Match a timestamped artifact name, capturing its layer.

    e.g. ``sac-04-scitex-2026-0812-113927.sif`` → layer ``04-scitex``.
    Built from the table so a new stage is matched the day it is added; the
    alternation is longest-first so a name that prefixes another cannot win.
    """
    names = sorted(LAYERS, key=len, reverse=True)
    alternation = "|".join(re.escape(n) for n in names)
    return re.compile(
        rf"^sac-(?P<layer>{alternation})-(?P<ts>\d{{4}}-\d{{4}}-\d{{6}})\.sif$"
    )


__all__ = [
    "ACCEPTED_NAMES",
    "ALIASES",
    "BAKEABLE",
    "DEFAULT_LAYER",
    "LAYERS",
    "Layer",
    "UnknownLayer",
    "artifact_re",
    "canonical_name",
    "chain_for",
    "parent_of",
    "resolve",
]
