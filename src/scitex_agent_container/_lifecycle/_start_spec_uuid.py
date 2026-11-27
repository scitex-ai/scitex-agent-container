"""The ``spec_uuid`` a START records on its ``instances`` row.

Immutable spec identity (card sac-identity-spec-uuid-20261026) is
first-seen-stable. A spec without ``uuid`` gets one minted into its RUNTIME
state dir, never into the committed spec.yaml: the spec-authority gate
refuses a dirty spec repo, so stamping the file made every second start fail
(issue #1777). A spec that DOES carry ``uuid`` has it mirrored into the same
runtime state file, so a host-only stamp line (written by SAC < 0.40) can be
removed later (``sac agents migrate-spec-uuids``) without the next start
minting a new identity.
"""

from __future__ import annotations

from typing import Any

__all__ = ["start_spec_uuid"]


def start_spec_uuid(config: Any) -> str:
    """Resolve (and persist) the uuid for this start; ``""`` when unknown.

    Best-effort ON PURPOSE: a stamp/mirror failure (read-only state dir,
    malformed state file) must never break a start. An authored uuid is
    kept; a spec without one stays unstamped and the resolver treats the
    row as unknown, exactly as before.
    """
    authored = str(getattr(config, "uuid", "") or "")
    if not str(getattr(config, "config_path", "") or ""):
        return authored
    try:
        from ..config._spec_uuid import resolve_start_spec_uuid
        from ..runtimes.tui_session import state_dir_for_config

        return resolve_start_spec_uuid(authored, state_dir_for_config(config))
    except Exception:  # stx-allow: fallback (reason: the uuid stamp/mirror is bookkeeping about a start that must proceed; an authored uuid is kept, an unstamped row resolves by name exactly as before)
        return authored
