"""Bus-listen env injection for the apptainer runtime.

Pulled out of ``_apptainer_runtime.py`` so the listen-env logic lives in
one place and the runtime file stays under sac's 512-line cap. Mirrors
the ``_apptainer_iso_flags.compute_iso_prepend`` extraction pattern.

The in-container ``sac mcp channel`` adapter (registered when
``spec.claude.channels`` contains ``server:sac``) resolves the bus from
two env vars at start:

* ``SAC_LISTEN_BASE_URL`` — the host-stable ``sac listen`` URL the
  adapter subscribes its inbox SSE against, and the per-agent sidecar
  advertises in its agent card so peers survive per-restart port churn.
* ``SAC_LISTEN_BEARER`` — the bearer the adapter must present or
  ``sac listen`` returns 401, the subscription never lands, and every
  lead ``a2a_send`` push reports ``delivered_subscriber_count=0``.

This module returns the ``--env`` flags ``build_run_argv`` should append.
The injection is UNCONDITIONAL w.r.t. the relaxed escape-hatch: relaxed
specs (``--containall`` + explicit ``raw_args``) bypass the preflight
wrapper but still need bus auth, otherwise their adapter can never
subscribe.

Fail-loud contract: when ``server:sac`` is registered but the bearer
cannot be resolved, this raises ``RuntimeError`` rather than launching an
agent whose adapter can never authenticate (the silent wake-on-push
failure this guard exists to prevent). When ``server:sac`` is absent a
missing token is harmless (nothing subscribes) — we inject only the base
URL and log a loud warning.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


def listen_env_flags(config) -> list[str]:
    """Return the ``--env SAC_LISTEN_*`` flags for ``apptainer exec``.

    Pure except for reading the host token file and config; raises
    ``RuntimeError`` when a ``server:sac`` spec has no resolvable bearer.
    """
    # Local imports keep these resolvable even if a formatter strips
    # module-level unused imports during a refactor, and avoid a circular
    # import with the runtime module that calls this helper.
    from .._listen._config import listen_base_url
    from ._apptainer_build import _listen_token_path, _read_listen_bearer

    flags: list[str] = ["--env", f"SAC_LISTEN_BASE_URL={listen_base_url()}"]

    claude_spec = getattr(config, "claude", None)
    channels = list(getattr(claude_spec, "channels", None) or [])
    wants_bus = any(str(c).strip() == "server:sac" for c in channels)

    bearer = _read_listen_bearer()
    if bearer:
        flags += ["--env", f"SAC_LISTEN_BEARER={bearer}"]
    elif wants_bus:
        raise RuntimeError(
            "spec.claude.channels includes 'server:sac' but the bus bearer "
            f"token file {_listen_token_path()} is absent or empty, so the "
            "in-container channel adapter could never authenticate to "
            "`sac listen` (401). Subscriptions would never land and every "
            "pushed turn would report delivered_subscriber_count=0 — "
            "refusing to launch an agent whose adapter can never subscribe. "
            "Start `sac listen` to generate the token, then restart this "
            "agent."
        )
    else:
        logger.warning(
            "SAC_LISTEN_BEARER not injected: bus token file %s is absent. "
            "The in-container channel adapter cannot authenticate to "
            "`sac listen` (401), so inbox subscription and pushed turns "
            "will fail. Start `sac listen` to generate the token, then "
            "restart this agent.",
            _listen_token_path(),
        )
    return flags


__all__ = ["listen_env_flags"]
