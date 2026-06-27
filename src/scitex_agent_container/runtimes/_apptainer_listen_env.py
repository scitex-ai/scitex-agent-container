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
    """Return the ``--env`` flags ``apptainer exec`` needs for the bus.

    Forwards the bus-listen URL + bearer (``SAC_LISTEN_*``) and, when set
    on the host, the agent-spec search path
    (``SCITEX_AGENT_CONTAINER_YAML_DIRS``) so an in-container ``sac agents
    start <peer>`` resolves specs at the SAME path the operator uses on
    the host. Without this forward the in-container sac only searches
    ``~/.scitex/agent-container/agents`` + the repo-local dir (the env var
    is unset inside the container), so the spec-bearing spawn path fails
    with ``Agent '<name>' not found ... (env
    $SCITEX_AGENT_CONTAINER_YAML_DIRS: <unset>)`` even though the specs
    are visible in-container via the host-home bind.

    Pure except for reading the host token file, config, and host env;
    raises ``RuntimeError`` when a ``server:sac`` spec has no resolvable
    bearer.
    """
    # Local imports keep these resolvable even if a formatter strips
    # module-level unused imports during a refactor, and avoid a circular
    # import with the runtime module that calls this helper.
    import os

    from .._listen._config import listen_base_url
    from ._apptainer_build import _listen_token_path, _read_listen_bearer

    flags: list[str] = ["--env", f"SAC_LISTEN_BASE_URL={listen_base_url()}"]

    # PERSISTENT TESTMON CACHE — point testmon's data file at the
    # container-side bind destination (see
    # ``_p3a_default_binds._FLEET_DEFAULT_BINDS``: the host's
    # ``~/.cache/scitex-testmon`` is bound ``rw`` to
    # ``/home/agent/.cache/scitex-testmon``). scitex-dev's pre-commit-hook
    # wrapper reads ``$SCITEX_TESTMON_CACHE_ROOT`` so the testmon cache
    # PERSISTS across the fresh-git-worktree churn the develop-pin hook
    # forces — otherwise every commit re-runs the full ~2500-test suite
    # against a cold worktree-local ``.testmondata``. UNCONDITIONAL (same
    # as the base URL above): the cache helps every agent regardless of
    # bus membership, and a missing host bind dir is already a silent
    # no-op via ``default_binds_for_host``'s skip-if-missing filter.
    flags += [
        "--env",
        "SCITEX_TESTMON_CACHE_ROOT=/home/agent/.cache/scitex-testmon",
    ]

    # HOST-TUNNELED QWEN FALLBACK — point the scitex-genai client at the
    # host-side ssh tunnel to Spartan-hosted qwen. ``127.0.0.1:4000`` is
    # reachable from EVERY agent container because apptainer shares the
    # host network namespace, so no per-container port forward is needed.
    # ``SCITEX_GENAI_BASE_URL`` is a namespaced FALLBACK that scitex-genai
    # consults ONLY on its self-hosted / unknown-model path — it is
    # deliberately NOT ``OPENAI_BASE_URL`` (which the openai SDK auto-reads
    # and would misroute real ``gpt-*`` traffic to this local qwen tunnel).
    # Only the base URL is injected here; the qwen API key is gated on a
    # separate operator security decision and is intentionally NOT set.
    # UNCONDITIONAL (same as the base URL + testmon cache above): a base
    # URL with no key is harmless to agents that never hit the fallback.
    flags += [
        "--env",
        "SCITEX_GENAI_BASE_URL=http://127.0.0.1:4000/v1",
    ]

    # Forward the host's agent-spec search path so the in-container sac
    # resolves peer specs at the operator's path. Only inject when the
    # host has it set+non-empty — otherwise leave the in-container default
    # search order untouched (no empty ``--env`` that would shadow it).
    spec_dirs = os.environ.get("SCITEX_AGENT_CONTAINER_YAML_DIRS", "").strip()
    if spec_dirs:
        flags += ["--env", f"SCITEX_AGENT_CONTAINER_YAML_DIRS={spec_dirs}"]

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
