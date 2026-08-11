"""BOOT ASSERTION: refuse to run in a venv that carries a duplicated dist-info.

The host-side rail (:mod:`._overlay_venv_invalidate`) tries to REPAIR the
overlay before the container starts. This is the second layer: once inside, it
measures the union the agent actually imports from and refuses to proceed when
that union is incoherent. Repair host-side, refuse to run broken in-container.

THE METHOD NOTE IS LOAD-BEARING — ``distributions()``, NEVER ``entry_points()``
------------------------------------------------------------------------------
``importlib.metadata.entry_points()`` **DEDUPES BY NORMALISED NAME** before it
reads any entry point. CPython 3.12 spells it::

    eps = itertools.chain.from_iterable(
        dist.entry_points for dist in _unique(distributions())
    )                              # _unique keys on _normalized_name

Two ``.dist-info`` directories for the same distribution therefore collapse to
ONE before ``entry_points()`` looks at anything, so a gate built on it CANNOT
SEE THIS BUG. It passes while the venv is broken. That is a gate that cannot
fail, which is strictly worse than no gate: the config still lists it, so the
absence of an alarm reads as evidence of health.

``distributions()`` does not dedupe, and it is what PLUGGY ITSELF uses —
pytest's plugin loader iterates ``importlib.metadata.distributions()`` and reads
``dist.entry_points`` per distribution, so it observes exactly what this check
observes, including the dead entry.

That is the whole mechanism of the outage. Measured by scitex-hub: the SIF's
``scitex_dev-0.43.1.dist-info`` declares
``pytest11 = scitex_dev._core._test_execution_plugin`` while the stale overlay
tree supplies 0.38.0 code where that module does not exist, so every ``pytest``
run dies before collecting a single test::

    ModuleNotFoundError: No module named 'scitex_dev._core._test_execution_plugin'

An ``entry_points()``-based check is green throughout. See also
:mod:`..runtimes._overlay_distinfo`, which predicts this collision host-side and
explains why a VERSION comparison is likewise blind to it.

SCOPE IS AN EXPLICIT GATE, NOT A FOLDED UNKNOWN
-----------------------------------------------
:func:`duplicate_distributions` is three-valued and returns UNKNOWN whenever it
could not measure — including when pointed at a path that is not a populated
venv. :func:`assert_venv_distributions_unique` refuses on FAIL **and** on
UNKNOWN alike. What it does NOT do is run at all when the venv is simply not
present on this filesystem: that is a SCOPE decision, taken visibly and logged,
rather than an unknown quietly recorded as a pass.
"""

from __future__ import annotations

import logging
import os
from collections import defaultdict
from pathlib import Path

from .._drift.versions import DEFAULT_VENV
from ._overlay_masking_model import canonical_dist_name
from ._overlay_venv_model import CHECK_VENV_DISTS_UNIQUE, VenvCheck

logger = logging.getLogger(__name__)

__all__ = [
    "SKIP_ENV_VAR",
    "VenvDistributionError",
    "assert_venv_distributions_unique",
    "duplicate_distributions",
    "site_packages_dirs",
]

#: Set to ``"1"`` to skip the assertion. Deliberately an OVERRIDE rather than a
#: default-off knob — the hazard belongs in the escape hatch, not in the
#: declaration, so nobody disables this by merely not knowing about it. Mirrors
#: ``runtimes._entry_point_gate.SKIP_ENV_VAR``.
SKIP_ENV_VAR = "SAC_SKIP_VENV_DIST_ASSERTION"

_HINT_DUPLICATE = (
    "the overlay's upper layer is shadowing the image's site-packages. Stop "
    "the agent and let the next start reconcile it "
    "(_maintenance._overlay_venv_invalidate.reconcile_overlay_venv moves the "
    "stale <overlay>/upper/opt/venv-sac aside into <overlay>/.old/<ts>/ — "
    "nothing is deleted), or move that slice aside by hand FROM THE HOST. "
    "Never delete it from inside the container: overlayfs turns that into a "
    "whiteout that masks the image's clean files too"
)


class VenvDistributionError(RuntimeError):
    """The venv this process imports from carries a duplicated distribution.

    Carries the repair, not just the complaint. Raised at BOOT, so the operator
    sees this instead of a ``ModuleNotFoundError`` from deep inside pytest that
    reads as a broken repository.
    """


def site_packages_dirs(venv: Path | str = DEFAULT_VENV) -> list[Path]:
    """Every ``site-packages`` under ``venv``. Empty when there is none."""
    root = Path(venv)
    try:
        return sorted(p for p in root.glob("lib/python*/site-packages") if p.is_dir())
    except OSError as exc:  # stx-allow: fallback (reason: an unreadable venv must yield UNKNOWN upstream, not a crash)
        logger.warning("venv-dists: could not enumerate %s: %s", root, exc)
        return []


def _evidence_path(dist) -> str:
    """Where this distribution's metadata lives, for the error message.

    ``PathDistribution._path`` is private and is the only handle on the actual
    ``.dist-info`` directory; ``locate_file("")`` (public) narrows only to the
    containing ``site-packages``. A missing ``_path`` therefore degrades the
    EVIDENCE, never the verdict.
    """
    path = getattr(dist, "_path", None)
    if path is not None:
        return str(path)
    try:
        return str(dist.locate_file(""))
    except Exception:  # stx-allow: fallback (reason: evidence only — a distribution that cannot name its own location must not suppress the alarm about it)
        return "(location unavailable)"


def duplicate_distributions(venv: Path | str = DEFAULT_VENV) -> VenvCheck:
    """Three-valued: is every distribution in ``venv`` present exactly once?

    Enumerates with :func:`importlib.metadata.distributions` scoped to the
    venv's ``site-packages`` — see the module docstring for why this is
    ``distributions()`` and not ``entry_points()``.
    """
    from importlib.metadata import distributions

    sites = site_packages_dirs(venv)
    if not sites:
        return VenvCheck(
            name=CHECK_VENV_DISTS_UNIQUE,
            ok=None,
            detail=f"no site-packages found under {venv}",
            hint=(
                f"point the check at a real venv; {venv} carries no "
                "lib/python*/site-packages, so nothing was measured and "
                "nothing may be concluded"
            ),
        )

    try:
        found = list(distributions(path=[str(p) for p in sites]))
    except OSError as exc:  # stx-allow: fallback (reason: an unreadable site-packages is UNKNOWN; reporting it as clean is the bug this rail closes)
        return VenvCheck(
            name=CHECK_VENV_DISTS_UNIQUE,
            ok=None,
            detail=f"could not enumerate distributions under {venv}: {exc}",
            hint="check permissions on the site-packages directories, then re-run",
        )

    if not found:
        return VenvCheck(
            name=CHECK_VENV_DISTS_UNIQUE,
            ok=None,
            detail=f"{venv} has site-packages but no installed distributions",
            hint=(
                "an empty venv is not a healthy one; confirm the image was "
                "built and mounted before treating this as a pass"
            ),
        )

    seen: dict[str, list[tuple[str, str]]] = defaultdict(list)
    for dist in found:
        name = canonical_dist_name(dist.metadata["Name"] or "")
        if not name:
            continue
        seen[name].append((dist.version or "(no version)", _evidence_path(dist)))

    duplicates = {n: rows for n, rows in seen.items() if len(rows) > 1}
    if not duplicates:
        return VenvCheck(
            name=CHECK_VENV_DISTS_UNIQUE,
            ok=True,
            detail=f"{len(seen)} distribution(s) under {venv}, each exactly once",
        )

    lines = []
    for name in sorted(duplicates):
        rendered = "; ".join(f"{ver} at {path}" for ver, path in duplicates[name])
        lines.append(f"{name}: {len(duplicates[name])} dist-infos -> {rendered}")
    return VenvCheck(
        name=CHECK_VENV_DISTS_UNIQUE,
        ok=False,
        detail=(
            f"{len(duplicates)} distribution(s) appear more than once under "
            f"{venv} -- " + " | ".join(lines)
        ),
        hint=_HINT_DUPLICATE,
    )


def assert_venv_distributions_unique(
    agent_name: str,
    *,
    venv: Path | str = DEFAULT_VENV,
) -> VenvCheck | None:
    """Refuse to boot into an incoherent venv. Returns the check, or ``None``.

    ``None`` means the assertion did not apply — the override is set, or the
    venv is not present on this filesystem (host-side unit runs, a source
    checkout, any non-container invocation). Both are logged; neither is
    recorded as a pass.

    Refuses on FAIL and on UNKNOWN alike. That is safe here because the only
    way to reach either is to have LOOKED at a real, populated venv: an absent
    venv is filtered out above as out-of-scope, before the check runs.

    Raises:
        VenvDistributionError: naming every duplicated package, every version
            found, and every path — or naming what could not be measured.
    """
    if os.environ.get(SKIP_ENV_VAR) == "1":
        logger.warning(
            "venv-dist assertion SKIPPED for %r via %s — the union is unverified",
            agent_name,
            SKIP_ENV_VAR,
        )
        return None

    if not Path(venv).is_dir():
        logger.info(
            "venv-dist assertion not applicable for %r: %s is not present here",
            agent_name,
            venv,
        )
        return None

    check = duplicate_distributions(venv)
    if check.ok is True:
        logger.info("venv-dist assertion passed for %r: %s", agent_name, check.detail)
        return check

    raise VenvDistributionError(f"{agent_name}: {check.detail}\nREPAIR: {check.hint}")
