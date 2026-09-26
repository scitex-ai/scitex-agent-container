"""Agent-spec source drift detection (sac-drift).

Three surfaces:

* :mod:`._authority` — fail-closed lifecycle authority validation.
* :mod:`._local` — resilient, read-only local diagnostics for ``sac doctor``.
* :mod:`._fleet` — on-demand ``sac doctor --fleet`` that ssh-checks the
  same drift on every configured peer host and renders a per-host table.

The shared drift model lives in :mod:`._status`.
"""

from __future__ import annotations

from ._authority import SpecAuthority, SpecAuthorityError, validate_spec_authority
from ._fleet import HostDrift, check_fleet_drift, check_peer_drift
from ._local import (
    SpecSourceDriftError,
    check_spec_source_drift,
    drift_warning_lines,
    spec_source_repo,
    warn_if_spec_source_drifted,
)
from ._status import DriftState, DriftStatus
from .versions import (
    collect_versions,
    discover_base_sifs,
    record_overlay_manifest,
)

__all__ = [
    "DriftState",
    "DriftStatus",
    "HostDrift",
    "SpecAuthority",
    "SpecAuthorityError",
    "SpecSourceDriftError",
    "check_fleet_drift",
    "check_peer_drift",
    "check_spec_source_drift",
    "collect_versions",
    "discover_base_sifs",
    "drift_warning_lines",
    "record_overlay_manifest",
    "spec_source_repo",
    "validate_spec_authority",
    "warn_if_spec_source_drifted",
]
