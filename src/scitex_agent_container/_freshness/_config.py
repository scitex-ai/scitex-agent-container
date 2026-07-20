#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# File: src/scitex_agent_container/_freshness/_config.py

"""sac's half of the version-currency contract: the constants, and nothing else.

The JUDGMENT — what counts as stale, which check may run against which kind
of install, what remedy is safe to print — lives upstream in
``scitex_dev.versioning`` and is deliberately not duplicated here. sac used
to be where all of it lived; the operator's ruling moved it
(「デブが primitive を持ち、リーフが使って自己アップデート」 — dev holds the
primitive, leaves consume it). What is left on this side is the handful of
facts that are genuinely sac's and could not be generic: our distribution
name, our PyPI endpoint, our release workflow file, our daemon's unit name,
and where our cache goes.

WHY THE VALUES BELOW ARE CONSTANTS AND NOT LITERALS AT THE CALL SITE: each
one has exactly one correct value and several plausible wrong ones. The
release workflow in particular is a real filename that must match the repo,
and a typo there does not fail loudly — it silently produces UNKNOWN for the
release-run check forever, which is the failure mode this whole subsystem
exists to make impossible.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ._expectations import build_expectations

if TYPE_CHECKING:  # pragma: no cover - kept off the CLI import path
    from scitex_dev.versioning import VersioningConfig

__all__ = [
    "CACHE_SUBPATH",
    "DIST_NAME",
    "LISTEN_UNIT",
    "MODULE_NAME",
    "PYPI_JSON_URL",
    "RELEASE_WORKFLOW",
    "sac_versioning_config",
]

#: The distribution name on PyPI. Also the key ``importlib.metadata`` is
#: asked for — the lookup whose frozen answer started all of this.
DIST_NAME = "scitex-agent-container"

#: The import name. Not derivable from DIST_NAME by a rule we control, so
#: it is stated rather than computed.
MODULE_NAME = "scitex_agent_container"

#: PyPI's JSON endpoint for this distribution — the only source that knows
#: what actually SHIPPED, as opposed to what was tagged or released.
PYPI_JSON_URL = f"https://pypi.org/pypi/{DIST_NAME}/json"

#: The workflow file that publishes a release. MUST match the real filename
#: in .github/workflows/ — see the module docstring on why a typo here is
#: silent. Verified against the repo when this landed.
RELEASE_WORKFLOW = "pypi-publish-and-github-release-on-tag.yml"

#: sac's long-lived host control-plane daemon. Its start time vs the
#: install mtime is what catches the case a version string structurally
#: cannot see: the package on disk was upgraded, and this process is still
#: executing the modules it imported at boot, because Python does not
#: reload modules in a live process.
LISTEN_UNIT = "sac-listen.service"

#: Under ``$SCITEX_DIR`` (default ``~/.scitex``). Kept RELATIVE and joined at
#: call time by the primitive: ``$HOME`` is ``/home/agent`` in a container and
#: ``/home/ywatanabe`` on the host, and an import-time ``Path.home()``
#: constant cannot be redirected afterwards by a container or a test fixture.
#: Matches sac's existing ``~/.scitex/agent-container/`` layout rather than
#: the primitive's ``<module>/`` default, so the file lands where an operator
#: looking for sac state would actually look.
CACHE_SUBPATH = ("agent-container", "runtime", "version-currency.json")


def sac_versioning_config() -> "VersioningConfig":
    """Build the ``VersioningConfig`` describing sac.

    Requires scitex-dev. Callers on any path that must survive its absence
    go through :func:`scitex_agent_container._freshness.check_currency`,
    which degrades to UNKNOWN instead of raising.

    ``env_prefix`` is deliberately left to the primitive's own derivation:
    it produces ``SCITEX_AGENT_CONTAINER_FRESHNESS`` from the dist name,
    which is already sac's env-var namespace. Overriding it with a shorter
    ``SAC_*`` alias would fork that namespace for no gain.
    """
    from scitex_dev.versioning import VersioningConfig

    return VersioningConfig(
        dist=DIST_NAME,
        module=MODULE_NAME,
        pypi_json_url=PYPI_JSON_URL,
        release_workflow=RELEASE_WORKFLOW,
        systemd_unit=LISTEN_UNIT,
        cache_subpath=CACHE_SUBPATH,
        expectations=build_expectations(),
    )


# EOF
