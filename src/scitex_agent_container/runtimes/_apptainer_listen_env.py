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

    Forwards the bus-listen URL + bearer (``SAC_LISTEN_*``) and ALWAYS
    injects the agent-spec search path
    (``SCITEX_AGENT_CONTAINER_YAML_DIRS``) so an in-container ``sac agents
    start <peer>`` resolves specs at the SAME path the operator uses on
    the host. The injected value is the union of any host-set
    ``SCITEX_AGENT_CONTAINER_YAML_DIRS`` (pass-through, order preserved)
    and the host's canonical user-scope agents dir
    (``~/.scitex/agent-container/agents`` expanded against the HOST home
    at launch time). That host dir is bind-visible in-container because
    apptainer binds the invoking host's ``$HOME`` at the same path, yet
    the in-container ``$HOME`` is a DIFFERENT, empty home — so the
    in-container sac's default user-scope search finds zero specs unless
    we point it back at the host home explicitly. Without this the
    spec-bearing spawn path fails with ``Agent '<name>' not found ... (env
    $SCITEX_AGENT_CONTAINER_YAML_DIRS: <unset>)`` even though the specs
    are visible in-container via the host-home bind.

    Pure except for reading the host token file, config, host env, and
    host home; raises ``RuntimeError`` when a ``server:sac`` spec has no
    resolvable bearer.
    """
    # Local imports keep these resolvable even if a formatter strips
    # module-level unused imports during a refactor, and avoid a circular
    # import with the runtime module that calls this helper.
    import os
    from pathlib import Path

    from .._listen._config import listen_base_url
    from ._apptainer_build import _listen_token_path, _read_listen_bearer
    from ._mcp_reliability import mcp_timeout_env_flags

    flags: list[str] = ["--env", f"SAC_LISTEN_BASE_URL={listen_base_url()}"]

    # AGENT SELF-NAME — without ``SAC_NAME`` an in-container agent cannot
    # introspect its own registry row: ``agent_list``/``agent_logs`` return
    # "not found" and ``agent_spawn`` can't resolve the caller, so parent->child
    # lineage links go unrecorded (the spawn caller defaults to ``SAC_NAME``,
    # read via ``_env.getenv("NAME")`` which honours SAC_NAME/SCITEX_AGENT_
    # CONTAINER_NAME). ``SAC_LISTEN_*`` were injected but this was missed. Skip
    # an EMPTY name so it can never shadow a value supplied elsewhere.
    agent_name = getattr(config, "name", "") or ""
    if agent_name:
        flags += ["--env", f"SAC_NAME={agent_name}"]

    # DIRENV CONFIG LOCATION — direnv reads its config from
    # ``$DIRENV_CONFIG/direnv.toml`` (default ``$HOME/.config/direnv``). The sac
    # base image writes a permissive whitelist to ``/etc/direnv/direnv.toml``
    # (``prefix = [ "/" ]``) so per-project ``.envrc`` files load without a
    # manual ``direnv allow`` — but without pointing ``$DIRENV_CONFIG`` at that
    # path, direnv reads an unwritten ``~/.config/direnv/direnv.toml``, its
    # loaded ``whitelist.prefix`` stays empty, and EVERY ``.envrc`` is blocked
    # ("direnv: error .envrc is blocked. Run `direnv allow`"). Empirically
    # verified 2026-07-01. UNCONDITIONAL — every agent needs the whitelist to
    # apply. Note: this is a generic, language-agnostic direnv-tooling knob (NOT
    # coupled to any scitex-* package), so it does not violate the
    # sac/other-package standalone boundary.
    flags += ["--env", "DIRENV_CONFIG=/etc/direnv"]

    # UV DEFAULT VENV — force ``uv``'s project venv to the container-only
    # boot-built path ``/uvwork/venv-agent`` so any ad-hoc ``uv run`` /
    # ``uv pip install`` / ``uv sync`` an agent runs WITHOUT an explicit
    # ``--python`` resolves there instead of defaulting to ``./.venv`` inside
    # the shared ``~/proj/<agent>`` host<->container bind. A container-created
    # ``./.venv`` uses the container python (``/opt/python3.12``) whose
    # ``pyvenv.cfg home=`` then DANGLES on the host (no ``/opt/python3.12``
    # host-side), breaking host ``uv``/``sac`` — INCIDENT 2026-07-02:
    # ``~/proj/neurovista/.venv`` broke host tooling exactly this way. The
    # shared ``~/proj/<agent>/.venv`` stays reserved for the host's own
    # python. UNCONDITIONAL — every agent's uv must default to the
    # container-only venv. Generic uv-tooling knob, not coupled to any
    # scitex-* package.
    flags += ["--env", "UV_PROJECT_ENVIRONMENT=/uvwork/venv-agent"]

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

    # MCP STARTUP CONNECT TIMEOUT — raise Claude Code's per-server MCP startup
    # timeout (fleet incident 2026-07-06). The ``sac`` + ``scitex-todo`` stdio
    # MCP servers pull in ``fastmcp``, whose import alone is multiple seconds on
    # a cold / slow container FS; Claude Code's default startup cap intermittently
    # loses that race, and it does NOT auto-reconnect a failed *stdio* MCP — so
    # the agent then runs its ENTIRE session missing host_exec / agent_spawn /
    # db_* / the todo tools. ``MCP_TIMEOUT`` is the CLIENT startup-timeout env var
    # (ms), DISTINCT from the per-server ``timeout`` field (per-tool-call). Paired
    # with ``alwaysLoad:true`` on those servers (see ``_mcp_reliability``), the
    # session deterministically WAITS for the (slow) connect. UNCONDITIONAL —
    # every agent needs it; a generic Claude-Code tooling knob, not coupled to any
    # scitex-* package.
    flags += mcp_timeout_env_flags()

    # ALWAYS inject the agent-spec search path so the in-container sac
    # resolves peer specs even when the launching env has nothing set.
    # The in-container ``$HOME`` is a different, empty home than the
    # host's, so its default user-scope search
    # (``~/.scitex/agent-container/agents`` under the CONTAINER home)
    # finds zero specs. apptainer binds the host ``$HOME`` at the same
    # path, so the HOST-side expansion below is bind-visible in-container.
    # Union: any host-set value (pass-through, order preserved) followed
    # by the host user-scope agents dir if not already present. The suffix
    # ``.scitex/agent-container/agents`` matches ``config/_resolve.py``'s
    # ``_search_dirs`` ``primary`` so the two stay in sync. Do NOT hardcode
    # a username — ``expanduser`` resolves the invoking host's home.
    host_default = str(
        Path("~/.scitex/agent-container/agents").expanduser()
    )
    spec_dirs_raw = os.environ.get("SCITEX_AGENT_CONTAINER_YAML_DIRS", "")
    spec_dirs: list[str] = [p for p in spec_dirs_raw.split(":") if p.strip()]
    if host_default not in spec_dirs:
        spec_dirs.append(host_default)
    flags += [
        "--env",
        f"SCITEX_AGENT_CONTAINER_YAML_DIRS={':'.join(spec_dirs)}",
    ]

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
