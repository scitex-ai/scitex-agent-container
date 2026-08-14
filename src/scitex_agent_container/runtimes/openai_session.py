"""``openai-session`` runtime adapter — thin container wrapper.

Mirrors :mod:`runtimes.claude_session` but targets the ``openai-agents``
SDK path (scitex-todo card ``openai-compat-2``; ``spec.provider: openai``).

Key differences from ClaudeSessionRuntime:
  * No CLAUDE.md materialisation — there is no Claude SDK to discover
    skills/hooks/settings from ``$HOME/.claude/``.
  * No F-CS8 heavy-workdir warning — the workdir ``.claude/`` bloat
    heuristic is Claude-SDK-specific.
  * No ``_skills_boot_log`` — skills boot logging is Claude-specific.
  * Still does: ``to_home`` deployment (``.env``, ``.mcp.json``,
    ``settings.json``, etc.), same container isolation/binds/overlay,
    same state-dir semantics.

Everything else — the apptainer dispatch, the runtime contract
(``RuntimeBase``), the container-runtime injection seam, the pid
recording — is identical to the Claude path.
"""

from __future__ import annotations

from pathlib import Path

from .._logging import get_logger
from ..config import AgentConfig
from ._to_home import deploy_to_home
from .base import RuntimeBase

__all__ = ["OpenAISessionRuntime"]


def _spec_dir_for(config: AgentConfig) -> Path | None:
    """Return the directory containing the agent's spec.yaml, or ``None``."""
    cp = getattr(config, "config_path", "")
    if not cp:
        return None
    return Path(cp).parent


def _materialize_home_layouts(config: AgentConfig, home_dir: str) -> None:
    """Materialize the agent's ``to_home/`` layout into ``home_dir``.

    Mirrors :func:`_to_home_deployers._materialize_home_layouts` from
    the Claude path verbatim — the ``to_home/`` tree (``.env``,
    ``.mcp.json``, ``settings.json``, hooks, etc.) is provider-agnostic
    and needed by every agent regardless of SDK family.

    The legacy ``dot_claude/`` layout was removed (ADR-0006); a spec
    that still ships a ``dot_claude/`` dir is a hard error pointing
    at ``to_home/``.
    """
    spec_dir = _spec_dir_for(config)
    if spec_dir is not None and (spec_dir / "dot_claude").is_dir():
        raise RuntimeError(
            f"Spec {getattr(config, 'config_path', '<unknown>')!r} ships a "
            "legacy 'dot_claude/' dir next to spec.yaml. The dot_claude/ "
            "layout was removed (see ADR-0006) — rename it to 'to_home/' "
            "and move its contents to the $HOME-relative layout "
            "(to_home/{CLAUDE.md,.mcp.json,.env,.claude/{hooks,skills}}). "
            "Refusing to start with stale config."
        )
    deploy_to_home(config, home_dir)


# 2026-05-13 docker/podman ripout: apptainer is the only accepted
# runtime. Empty / unset ``spec.runtime`` is treated as ``apptainer``.
_CONTAINER_ENGINES: tuple[str, ...] = ("apptainer",)


def _container_runtime_for(config: AgentConfig):
    """Return the apptainer container runtime, or None for an
    unrecognised ``spec.runtime``.

    The OpenAI path uses the same container dispatch as the Claude path:
    ``apptainer exec`` into the SIF, with the inner command swapped to
    ``python -m scitex_agent_container._runners.openai_session`` by
    the apptainer argv builder (which branches on ``spec.provider``).
    """
    runtime = getattr(config, "runtime", "") or "apptainer"
    # Every spelling the harness registry's SDK entry claims (the legacy
    # ``apptainer`` value and the current ``claude-agent-sdk`` selector —
    # v4 step-4 derivation) dispatches the headless SDK runner via
    # ``apptainer exec``. The OpenAI path uses the same container
    # runtime — only the inner module differs.
    from ..config._harness_registry import CLAUDE_AGENT_SDK, runtime_spellings_for

    if runtime in runtime_spellings_for(CLAUDE_AGENT_SDK):
        from ._apptainer_runtime import ApptainerContainerRuntime

        return ApptainerContainerRuntime()
    return None


class OpenAISessionRuntime(RuntimeBase):
    """Daemon-mode runtime backed by ``openai-agents``, dispatched
    via apptainer. The host side never spawns a Python subprocess —
    every ``start`` goes through ``apptainer exec`` (or equivalent).

    Mirrors :class:`ClaudeSessionRuntime` — same isolation, binds,
    overlay, to_home, state-dir behaviour — but the inner module
    is ``scitex_agent_container._runners.openai_session`` instead
    of ``claude_session``.

    Key differences from the Claude path:
      * No CLAUDE.md materialisation (no Claude SDK).
      * No F-CS8 workdir-bloat warning (Claude-SDK-specific).
      * No skills-boot logging (Claude-SDK-specific).
      * Still does: ``to_home`` deployment, container dispatch,
        state-dir management, pid recording.

    ``container_runtime_for`` is an injection seam — defaults to the
    module-level ``_container_runtime_for`` lookup. Tests pass a
    callable that returns a fake container runtime so the dispatch
    glue can be exercised without booting a real container.
    """

    def __init__(self, container_runtime_for=None):
        self._container_runtime_for = container_runtime_for or _container_runtime_for

    def _setup_workspace(self, config: AgentConfig) -> None:
        """Materialise the agent's ``to_home/`` layout into the container
        ``$HOME``.

        ADR-0003 (D6/D7): the agent's container ``$HOME`` is bind-mounted
        from ``runtime/<name>/home/``. We materialise ``to_home/`` there
        so the container sees ``.env``, ``.mcp.json``, ``settings.json``,
        hooks, etc.

        Best-effort: skipped for stub configs that don't carry the full
        AgentConfig surface (unit-test SimpleNamespace fixtures).
        """
        required_attrs = ("expanded_workdir", "skills", "claude", "env", "labels")
        if not all(hasattr(config, a) for a in required_attrs):
            return
        home_dir = str(self._state_dir(config) / "home")
        Path(home_dir).mkdir(parents=True, exist_ok=True)
        _materialize_home_layouts(config, home_dir)
        # Mirror the same tree into the overlay's upper home so it lands
        # as part of the container filesystem (no-op for non-overlay specs).
        from ._to_home_overlay import deploy_to_home_overlay

        deploy_to_home_overlay(config)

    def _cleanup_workspace(self, config: AgentConfig) -> None:
        """No-op — the OpenAI path has no CLAUDE.md to scrub on stop."""
        # No managed CLAUDE.md section to remove; the to_home tree
        # is persistent and belongs to the agent's workspace, not
        # sac's lifecycle.
        pass

    def start(
        self,
        config: AgentConfig,
        no_preflight: bool = False,
        force: bool = False,
        dry_run: bool = False,
        foreground: bool = False,
        one_shot: bool = False,
    ) -> bool:
        """Spawn the container backing the agent.

        Mirrors :meth:`ClaudeSessionRuntime.start` — same ``apptainer exec``
        dispatch, same parameter contract. Only differs in:
          * No CLAUDE.md setup.
          * No F-CS8 warning.
        """
        # Operator directive 12870 (lead a2a b58dd5d3): emit the legacy
        # ``runtime: apptainer`` deprecation HERE (real start path), not
        # in ``_get_runtime`` (status / list / discovery walks).
        from .._lifecycle._runtime_select import warn_if_legacy_apptainer_runtime

        warn_if_legacy_apptainer_runtime(config)

        container_rt = self._container_runtime_for(config)
        if container_rt is None:
            # Same start-failure-reported-as-False shape as
            # ClaudeSessionRuntime.start; see the note there.
            get_logger(__name__).error(
                f"OpenAISessionRuntime requires a container engine "
                f"(spec.runtime: docker | podman). Got: "
                f"{getattr(config, 'runtime', '<unset>')!r}."
            )
            return False

        # to_home materialisation — .env, .mcp.json, settings.json, etc.
        self._setup_workspace(config)

        # No F-CS8 workdir-bloat check: the OpenAI SDK does not walk
        # ``<workdir>/.claude/`` at session start, so heavy trees
        # cannot cause silent discovery failures.

        return container_rt.start(
            config,
            no_preflight=no_preflight,
            force=force,
            dry_run=dry_run,
            foreground=foreground,
            one_shot=one_shot,
        )

    def stop(self, config: AgentConfig) -> bool:
        """Stop the container."""
        container_rt = self._container_runtime_for(config)
        if container_rt is None:
            return False
        return container_rt.stop(config)

    def is_running(self, config: AgentConfig) -> bool:
        container_rt = self._container_runtime_for(config)
        if container_rt is None:
            return False
        return container_rt.is_running(config)

    def agent_pid(self, config: AgentConfig) -> int | None:
        """The container process pid (``RuntimeBase`` seam).

        Delegates to the container runtime exactly as :meth:`is_running`
        above does, so the pid recorded in ``instances.pid`` is the same
        one this runtime's liveness verdict is keyed on — for apptainer,
        the long-lived ``apptainer`` process.

        ``None`` when the spec's runtime resolves to no container runtime
        (unrecognised ``spec.runtime``) or the container runtime cannot
        name a pid — honestly "unknown", never a fabricated value.
        """
        container_rt = self._container_runtime_for(config)
        if container_rt is None:
            return None
        getter = getattr(container_rt, "agent_pid", None)
        if not callable(getter):
            return None
        return getter(config)

    def logs(self, config: AgentConfig, lines: int = 50) -> str:
        """Prefer the rendered ``session.jsonl`` tail (host-side via
        /state bind-mount). Fall through to ``docker logs --tail N``
        when the transcript hasn't been written yet."""
        container_rt = self._container_runtime_for(config)
        if container_rt is None:
            return ""
        state_dir = self._state_dir(config)
        rendered = _format_session_tail(state_dir, lines)
        if rendered:
            return rendered
        return container_rt.logs(config, lines=lines)

    def _state_dir(self, config: AgentConfig) -> Path:
        """Per-agent state dir on the host: project-local if available,
        else ``~/.scitex/agent-container/runtime/<name>/``.

        Reuses the claude runner's state-dir resolver so both SDK
        families land state in the same place (shared ``state.db``,
        ``heartbeat.json``, etc.).
        """
        from .._runners import claude_session as _runner

        return _runner.state_dir_for(config.name, root=_project_runtime_root(config))


def _format_session_tail(state_dir, max_lines: int) -> str:
    """Render the tail of ``session.jsonl`` as a compact human view.

    Returns the empty string if the file is absent (caller falls back
    to ``container_rt.logs``). Each record renders as a single line
    keyed by type:

      [user]      mission text
      [assistant] streamed chunk
      [result]    session=<sid>  in/out/cache totals
      [error]     kind  detail
      [user_echo] (verbatim repr trimmed in the runner)
    """
    import json as _json

    transcript = state_dir / "session.jsonl"
    if not transcript.is_file():
        return ""
    try:
        with transcript.open(encoding="utf-8") as fh:
            raw = fh.readlines()[-max_lines:]
    except OSError:
        return ""
    out: list[str] = []
    for line in raw:
        line = line.strip()
        if not line:
            continue
        try:
            rec = _json.loads(line)
        except _json.JSONDecodeError:
            continue
        kind = rec.get("type", "?")
        if kind == "user":
            out.append(f"[user]      {rec.get('text', '')}")
        elif kind == "assistant":
            out.append(f"[assistant] {rec.get('text', '')}")
        elif kind == "result":
            usage = rec.get("usage") or {}
            out.append(
                f"[result]    session={rec.get('session_id', '?')}  "
                f"in={usage.get('input_tokens', 0)} "
                f"out={usage.get('output_tokens', 0)} "
                f"cache_w={usage.get('cache_creation_input_tokens', 0)} "
                f"cache_r={usage.get('cache_read_input_tokens', 0)}"
            )
        elif kind == "error":
            out.append(f"[error]     {rec.get('kind', '?')}: {rec.get('detail', '')}")
        elif kind == "user_echo":
            out.append(f"[user_echo] {rec.get('raw', '')}")
        else:
            out.append(f"[{kind}]      {_json.dumps(rec, ensure_ascii=False)[:200]}")
    return "\n".join(out)


def _project_runtime_root(config: AgentConfig) -> "Path | None":
    """If the agent's YAML lives under a project-scope
    ``.scitex/agent-container/`` tree (a git repo with that subdir),
    return the sibling ``runtime/`` so per-agent state lands inside
    the same repo. Otherwise None.

    In-repo test agents get in-repo state, keeping ``~/.scitex``
    clean and letting CI snapshot transcripts as build artifacts.
    """
    src = getattr(config, "config_path", "") or ""
    if not src:
        return None
    try:
        from scitex_config._ecosystem import local_state
    except Exception:  # stx-allow: fallback (reason: scitex-config optional; degrade to home-scope state)
        return None
    scope = local_state.find_project_scope("agent-container", start=Path(src).parent)
    return (scope / "runtime") if scope is not None else None
