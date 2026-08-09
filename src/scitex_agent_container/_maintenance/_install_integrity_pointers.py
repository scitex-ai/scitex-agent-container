"""Editable-pointer discovery + parsing — every shape pip/uv/setuptools emit.

An "editable pointer" is any file in site-packages that redirects imports
somewhere else on disk. There is no single format: the tooling has emitted
at least four shapes, and a guard that knows only one of them reports the
other three as clean.

  __editable__.<name>-<ver>.pth        setuptools "compat" mode — one bare
                                       absolute path on a line
  __editable__.<name>-<ver>.pth        setuptools "strict" mode — instead an
                                       ``import __editable___..._finder;
                                       ...install()`` exec line
  __editable___<name>_<ver>_finder.py  the module that exec line names; the
                                       real paths live in its MAPPING dict
  _editable_impl_<name>.pth            uv / hatchling — one bare absolute
                                       path (the /opt/venv-sac shape)
  <anything>.pth                       a plain path-adding .pth

Parsing is deliberately STATIC: the finder module is read with
:mod:`ast`, never imported or ``exec``'d. These files are the ones we
already suspect of pointing at abandoned trees; running them to find out
where they point is exactly the wrong instinct.

Only the two ``exists``-flavoured helpers touch the filesystem; everything
else is string/AST work, so the shapes can be tested from literal text.
"""

from __future__ import annotations

import ast
import logging
import re
from pathlib import Path

from ._install_integrity_model import (
    POINTER_FINDER,
    POINTER_PTH_IMPORT,
    POINTER_PTH_PATH,
    EditablePointer,
    canonical_dist_name,
)

logger = logging.getLogger(__name__)

__all__ = [
    "EDITABLE_PTH_MARKERS",
    "candidate_dist_names",
    "collect_pointers",
    "finder_targets",
    "is_editable_pth_name",
    "pth_targets",
    "target_exists",
]

#: A ``.pth`` whose NAME carries one of these is an editable pointer even
#: when we cannot parse a target out of it — that unparsable state is a
#: reportable UNKNOWN, not something to drop on the floor. A plain ``.pth``
#: without one of these markers is only a pointer if it really does carry
#: an absolute path (which is what keeps coverage/site hooks out).
EDITABLE_PTH_MARKERS = ("__editable__", "_editable_impl_", "editable")

_FINDER_RE = re.compile(r"__editable___\w+_finder")
#: Trailing ``_0_31_0``-style version segments a finder filename embeds.
_TRAILING_VERSION_RE = re.compile(r"(?:_\d+)+$")


def _content_lines(text: str) -> list[str]:
    """Non-blank, non-comment lines — how ``site.py`` reads a ``.pth``."""
    out = []
    for raw in text.splitlines():
        line = raw.strip()
        if line and not line.startswith("#"):
            out.append(line)
    return out


def is_editable_pth_name(name: str) -> bool:
    """Does this ``.pth`` filename advertise itself as an editable pointer?"""
    lowered = name.lower()
    return any(marker in lowered for marker in EDITABLE_PTH_MARKERS)


def pth_targets(text: str) -> list[tuple[str, str]]:
    """``.pth`` content -> ``[(shape, target_or_finder_module), ...]``.

    Two shapes come out of here. A bare ABSOLUTE path line is a
    :data:`POINTER_PTH_PATH` whose target is that path. An ``import
    __editable___x_finder; ...`` exec line is a :data:`POINTER_PTH_IMPORT`
    whose "target" is the finder MODULE NAME — the caller resolves it to
    real paths via :func:`finder_targets`.

    Exec lines that name no finder (coverage bootstraps, ``site`` hooks)
    yield nothing: they redirect no imports, and reporting them would bury
    the real findings under noise every venv has.
    """
    found: list[tuple[str, str]] = []
    for line in _content_lines(text):
        if line.startswith(("import ", "import\t")):
            match = _FINDER_RE.search(line)
            if match:
                found.append((POINTER_PTH_IMPORT, match.group(0)))
            continue
        # site.py treats any other line as a path to add to sys.path. Only
        # absolute ones are unambiguous evidence of a redirect.
        if line.startswith("/"):
            found.append((POINTER_PTH_PATH, line))
    return found


def finder_targets(text: str) -> list[str]:
    """``__editable___*_finder.py`` source -> the real directories it maps to.

    Reads the module's ``MAPPING``/``NAMESPACES`` dict literals with
    :func:`ast.literal_eval`. Never imports or execs the file.
    """
    # stx-allow: fallback (reason: a finder module we cannot parse must degrade to "no targets" -> an UNKNOWN leg, never crash the check that exists to read it)
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError) as exc:
        logger.debug("install-integrity: unparsable finder module: %s", exc)
        return []
    targets: list[str] = []
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        names = {t.id for t in node.targets if isinstance(t, ast.Name)}
        if not names & {"MAPPING", "NAMESPACES"}:
            continue
        try:
            value = ast.literal_eval(node.value)
        except (
            ValueError,
            SyntaxError,
        ):  # stx-allow: fallback (reason: a non-literal MAPPING is unreadable evidence, not a crash)
            continue
        if isinstance(value, dict):
            targets.extend(str(v) for v in value.values() if v)
    # Preserve first-seen order, drop repeats.
    return list(dict.fromkeys(targets))


def candidate_dist_names(filename: str) -> list[str]:
    """Pointer filename -> canonical dist-name candidates, longest first.

    The filename is the only attribution signal these files carry, and
    every emitter spells it differently::

        __editable__.scitex_dev-0.31.0.pth   -> scitex_dev-0.31.0
        __editable___scitex_dev_0_31_0_finder.py -> scitex_dev_0_31_0
        _editable_impl_scitex_agent_container.pth -> scitex_agent_container

    Version fragments are not separable from name fragments by rule (an
    underscore means both), so this returns PROGRESSIVELY SHORTER
    prefixes and lets the caller pick the first that matches a dist it
    actually saw. Guessing a single answer here would mis-attribute
    pointers to distributions that do not exist.
    """
    stem = Path(filename).stem
    for prefix in ("__editable___", "__editable__.", "__editable__", "_editable_impl_"):
        if stem.startswith(prefix):
            stem = stem[len(prefix) :]
            break
    if stem.endswith("_finder"):
        stem = stem[: -len("_finder")]
    stem = stem.split("-")[0]  # `name-1.2.3` -> `name`
    stem = _TRAILING_VERSION_RE.sub("", stem)  # `name_1_2_3` -> `name`
    if not stem:
        return []
    parts = stem.split("_")
    return [canonical_dist_name("_".join(parts[:n])) for n in range(len(parts), 0, -1)]


def target_exists(path: str) -> bool | None:
    """Does ``path`` exist? ``None`` when the stat itself failed.

    Tri-state on purpose: "I could not stat it" must never be recorded as
    "it is not there", which would fabricate a DEAD_POINTER out of a
    permissions error.
    """
    # stx-allow: fallback (reason: an unreadable/dangling mount must report UNKNOWN, never a fabricated "missing")
    try:
        return Path(path).exists()
    except OSError as exc:
        logger.debug("install-integrity: cannot stat %s: %s", path, exc)
        return None


def _finder_pointer(
    site: Path, module_name: str, source: Path
) -> list[EditablePointer]:
    """Resolve a ``pth-import`` line into the finder module's real targets."""
    finder = site / f"{module_name}.py"
    # stx-allow: fallback (reason: an unreadable finder module is an UNKNOWN leg, reported as an unparsable pointer)
    try:
        text = finder.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        logger.debug("install-integrity: cannot read finder %s: %s", finder, exc)
        return [EditablePointer(path=str(source), shape=POINTER_PTH_IMPORT, target="")]
    targets = finder_targets(text)
    if not targets:
        return [EditablePointer(path=str(finder), shape=POINTER_FINDER, target="")]
    return [
        EditablePointer(
            path=str(finder),
            shape=POINTER_FINDER,
            target=target,
            target_exists=target_exists(target),
        )
        for target in targets
    ]


def collect_pointers(site: Path, entry_names: list[str]) -> list[EditablePointer]:
    """Every editable pointer in ``site``, from its already-listed entries.

    ``entry_names`` is passed in rather than re-listed so the probe reads
    the directory exactly once (and so an unreadable directory is handled
    in one place, by the caller).
    """
    pointers: list[EditablePointer] = []
    listed = set(entry_names)
    for name in sorted(entry_names):
        if not name.endswith(".pth"):
            continue
        source = site / name
        # stx-allow: fallback (reason: an unreadable .pth is evidence we could not read, reported as an unparsable pointer — never a crash)
        try:
            text = source.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            logger.debug("install-integrity: cannot read %s: %s", source, exc)
            if is_editable_pth_name(name):
                pointers.append(
                    EditablePointer(path=str(source), shape=POINTER_PTH_PATH, target="")
                )
            continue
        parsed = pth_targets(text)
        if not parsed and is_editable_pth_name(name):
            # Named like a pointer, target unreadable -> an honest UNKNOWN.
            pointers.append(
                EditablePointer(path=str(source), shape=POINTER_PTH_PATH, target="")
            )
            continue
        for shape, value in parsed:
            if shape == POINTER_PTH_IMPORT:
                pointers.extend(_finder_pointer(site, value, source))
            else:
                pointers.append(
                    EditablePointer(
                        path=str(source),
                        shape=shape,
                        target=value,
                        target_exists=target_exists(value),
                    )
                )
    # A finder module with no .pth naming it still redirects nothing on its
    # own, but its presence is evidence; pick up orphaned ones too.
    claimed = {p.path for p in pointers}
    for name in sorted(listed):
        if not (name.startswith("__editable___") and name.endswith("_finder.py")):
            continue
        if str(site / name) in claimed:
            continue
        pointers.extend(_finder_pointer(site, name[: -len(".py")], site / name))
    return pointers
