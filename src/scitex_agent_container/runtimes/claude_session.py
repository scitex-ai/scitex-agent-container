"""``claude-session`` runtime adapter — F-CS17 minimal version.

After F-CS17 stages 1+2 made the validator container-only and
F-CS17 stage 3a deleted the bare-metal subprocess and SSH-dispatch
paths, this module is a thin shim:

  * Materialise <workdir>/.claude/CLAUDE.md before the container starts
    (F-CS1 hard/soft skill chain — the file lives on the host and the
    container sees it via the /work bind-mount).
  * Surface the F-CS8 heavy-workdir warning.
  * Delegate every lifecycle method to ContainerRuntime.

Anything that used to spawn a host-side ``subprocess.Popen`` for
``runtime: claude-session`` (the bare-metal Python runner), or hand-
roll SSH dispatch for ``spec.remote.host`` agents, is gone — sac
is a container wrapper now (per the F-CS16-DESIGN.md agreement),
and cross-host work goes through ``sac --on <peer>`` (F-CS12).
"""

from __future__ import annotations

import sys
from pathlib import Path

from ..config import AgentConfig
from ._dot_claude import cleanup_dot_claude, deploy_dot_claude
from .base import RuntimeBase
from .claude_md import cleanup_claude_md, setup_claude_md

__all__ = ["ClaudeSessionRuntime"]


# F-CS8 — silent SDK failure on heavy workdir/.claude/ trees.
# claude-agent-sdk auto-discovers ``<workdir>/.claude/`` at session
# start (hooks, skills, settings.local.json, agents). When that tree
# is large (or contains a hook that errors), the SDK swallows the
# error and returns 0 tokens with no log line — heartbeat fresh,
# every turn empty. Hard to debug. Emit a clear warning at start
# whenever the size exceeds this threshold.
_WORKDIR_CLAUDE_SIZE_WARN_BYTES = 10 * 1024 * 1024  # 10 MB


def _workdir_claude_size_bytes(workdir: str | None) -> int:
    """Return total size of ``<workdir>/.claude/`` in bytes, or 0.

    Symlinks are NOT followed (avoids loops; matches what the SDK's
    own discovery walks). Inaccessible files contribute 0 — this is
    a best-effort precheck, not a security audit.
    """
    if not workdir:
        return 0
    root = Path(workdir) / ".claude"
    if not root.is_dir():
        return 0
    total = 0
    for path in root.rglob("*"):
        # stx-allow: fallback (reason: stat may fail on broken symlinks
        # or permission-denied entries; treat as 0 bytes rather than abort)
        try:
            if path.is_file() and not path.is_symlink():
                total += path.stat().st_size
        except OSError:  # stx-allow: fallback (reason: see inline comment)
            continue
    return total


def _warn_if_heavy_workdir_claude(config: AgentConfig) -> None:
    """Print stderr warning if ``<workdir>/.claude/`` is large enough
    to risk silent SDK discovery failure (F-CS8).

    Best-effort: silent for stub configs and workdirs that don't
    carry a ``.claude/`` subtree.
    """
    workdir = getattr(config, "expanded_workdir", None) or getattr(
        config, "workdir", None
    )
    size = _workdir_claude_size_bytes(workdir)
    if size <= _WORKDIR_CLAUDE_SIZE_WARN_BYTES:
        return
    mb = size / (1024 * 1024)
    print(
        f"warning: '{workdir}/.claude/' is {mb:.1f} MB — "
        "claude-agent-sdk auto-discovery may swallow errors and the "
        "agent will return 0 tokens per turn with no log line. "
        "Recommend a project-specific workdir (e.g. "
        "/home/<you>/proj/<this-project>/) or /tmp/<scratch>/, then "
        "reference other repos via absolute paths. (F-CS8)",
        file=sys.stderr,
        flush=True,
    )


# 2026-05-13 docker/podman ripout: apptainer is the only accepted
# runtime. Empty / unset ``spec.runtime`` is treated as ``apptainer``.
_CONTAINER_ENGINES: tuple[str, ...] = ("apptainer",)


def _container_runtime_for(config: AgentConfig):
    """Return the apptainer container runtime, or None for an
    unrecognised ``spec.runtime``.
    """
    runtime = getattr(config, "runtime", "") or "apptainer"
    if runtime == "apptainer":
        from ._apptainer_runtime import ApptainerContainerRuntime

        return ApptainerContainerRuntime()
    return None


class ClaudeSessionRuntime(RuntimeBase):
    """Daemon-mode runtime backed by ``claude-agent-sdk``, dispatched
    via apptainer. The host side never spawns a Python subprocess —
    every ``start`` goes through ``apptainer exec`` (or equivalent).
    """

    def _setup_workspace(self, config: AgentConfig) -> None:
        """Materialise CLAUDE.md before launching the SDK runner.

        ADR-0003 (D6/D7): the agent's container ``$HOME`` is bind-mounted
        from ``runtime/<name>/home/``. We materialise ``dot_claude/`` and
        the sac-managed CLAUDE.md there (instead of the workdir, which
        is the project-source mount at ``/work``). Claude SDK's
        ``$HOME/.claude/`` discovery then sees skills, hooks, .mcp.json.

        Best-effort: skipped for stub configs that don't carry the full
        AgentConfig surface (unit-test SimpleNamespace fixtures).
        """
        required_attrs = ("expanded_workdir", "skills", "claude", "env", "labels")
        if not all(hasattr(config, a) for a in required_attrs):
            return
        home_dir = str(self._state_dir(config) / "home")
        Path(home_dir).mkdir(parents=True, exist_ok=True)
        setup_claude_md(config, home_dir)
        deploy_dot_claude(config, home_dir)

    def _cleanup_workspace(self, config: AgentConfig) -> None:
        """Remove the agent-container CLAUDE.md section on stop."""
        required_attrs = ("expanded_workdir", "skills", "claude", "env", "labels")
        if not all(hasattr(config, a) for a in required_attrs):
            return
        home_dir = str(self._state_dir(config) / "home")
        cleanup_claude_md(config, home_dir)
        cleanup_dot_claude(config, home_dir)

    def start(
        self,
        config: AgentConfig,
        no_preflight: bool = False,
        force: bool = False,
        dry_run: bool = False,
        foreground: bool = False,
    ) -> bool:
        """Spawn the container backing the agent.

        ``no_preflight`` is currently a no-op (kept for API parity
        with the legacy bare-metal runtime). ``foreground=True``
        attaches the operator's stdio to the container; daemon mode
        (the default) returns once the engine reports the container
        is live.
        """
        container_rt = _container_runtime_for(config)
        if container_rt is None:
            print(
                f"error: ClaudeSessionRuntime requires a container engine "
                f"(spec.runtime: docker | podman). Got: "
                f"{getattr(config, 'runtime', '<unset>')!r}.",
                file=sys.stderr,
                flush=True,
            )
            return False

        # F-CS1 — materialise CLAUDE.md so the SDK's auto-load picks
        # up the hard/soft skill list at session start. The file
        # lands on the host under <workdir>/.claude/; the container
        # sees it through the /work bind-mount.
        self._setup_workspace(config)

        # F-CS8 — pre-flight check on the workdir's .claude/ size,
        # so a heavy hook tree gets a clear stderr warning instead
        # of a silent 0-token-per-turn failure.
        _warn_if_heavy_workdir_claude(config)

        return container_rt.start(
            config,
            no_preflight=no_preflight,
            force=force,
            dry_run=dry_run,
            foreground=foreground,
        )

    def stop(self, config: AgentConfig) -> bool:
        """Stop the container; scrub the managed CLAUDE.md section."""
        container_rt = _container_runtime_for(config)
        if container_rt is None:
            return False
        ok = container_rt.stop(config)
        self._cleanup_workspace(config)
        return ok

    def is_running(self, config: AgentConfig) -> bool:
        container_rt = _container_runtime_for(config)
        if container_rt is None:
            return False
        return container_rt.is_running(config)

    def logs(self, config: AgentConfig, lines: int = 50) -> str:
        """Prefer the rendered ``session.jsonl`` tail (host-side via
        /state bind-mount). Fall through to ``docker logs --tail N``
        when the transcript hasn't been written yet — typical for a
        brand-new container that hasn't completed its first turn.
        """
        container_rt = _container_runtime_for(config)
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
