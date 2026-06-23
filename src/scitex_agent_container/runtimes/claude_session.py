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
roll SSH dispatch for the legacy ``spec.remote`` block (deleted in
WI-6, handoff §6, 2026-05-20), is gone — sac is a container wrapper
now (per the F-CS16-DESIGN.md agreement), and cross-host work goes
through ``sac --on <peer>`` (F-CS12) and ``spec.host`` pinning.
"""

from __future__ import annotations

import sys
from pathlib import Path

from ..config import AgentConfig
from ._to_home import deploy_to_home
from .base import RuntimeBase
from .claude_md import cleanup_claude_md, setup_claude_md

__all__ = ["ClaudeSessionRuntime"]


def _spec_dir_for(config: AgentConfig) -> Path | None:
    cp = getattr(config, "config_path", "")
    if not cp:
        return None
    return Path(cp).parent


def _materialize_home_layouts(config: AgentConfig, home_dir: str) -> None:
    """Materialize the agent's ``to_home/`` layout into ``home_dir``.

    ``to_home/`` is the single canonical layout (see ADR-0006).
    ``deploy_to_home`` overlays the per-agent ``to_home/`` on top of the
    shared baseline ``to_home/``, so per-agent files win on conflict.

    The legacy ``dot_claude/`` layout was removed: a spec that still
    ships a ``dot_claude/`` dir next to ``spec.yaml`` is a hard error
    pointing at ``to_home/`` — refusing to silently ignore stale config.
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


# F-CS8 — silent SDK failure on heavy workdir/.claude/ trees.
# claude-agent-sdk auto-discovers ``<workdir>/.claude/`` at session
# start (hooks, skills, settings.local.json, agents). When that tree
# is large (or contains a hook that errors), the SDK swallows the
# error and returns 0 tokens with no log line — heartbeat fresh,
# every turn empty. Hard to debug. Emit a clear warning at start
# whenever the size exceeds this threshold.
def _workdir_claude_warn_threshold() -> int:
    """Resolve the workdir/.claude size-warn threshold at call time.

    Honours ``$SAC_WORKDIR_CLAUDE_WARN_BYTES`` as an override for tests
    and ops; defaults to 10 MiB.
    """
    import os

    override = os.environ.get("SAC_WORKDIR_CLAUDE_WARN_BYTES")
    if override:
        try:
            return int(override)
        except ValueError:
            pass
    return 10 * 1024 * 1024  # 10 MB


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
    # The richer LOUD audit lives in ``_workdir_audit`` so the same code
    # surface is reused by ``sac agents status --workdir-audit`` and the
    # ``sac agents prune-claude`` CLI. ``audit_workdir_claude`` walks once,
    # collects both byte size + file count, and reports per-subdir bloat
    # sources so the operator sees exactly which directory to prune.
    from .._workdir_audit import audit_workdir_claude

    audit = audit_workdir_claude(workdir)
    # Fire on EITHER:
    #   * exceeded totals (the original F-CS8 gate), OR
    #   * a non-empty bloat_sources list (one of the per-subdir probes
    #     crossed the bloat threshold).
    # The latter clause exists because the 2026-06-04 audit refactor
    # PRUNES ``worktrees/`` from the top-level total (it doesn't slow
    # boot anymore, see _walk_exclusions + _measure_top_level), so a
    # worktrees-only-bloated tree no longer trips ``exceeded_*``. The
    # bloat_sources probe still catches it and the operator still
    # wants the actionable banner (which already includes the
    # subdir-specific `mv` remediation lines).
    if not (audit.exceeded_files or audit.exceeded_bytes or audit.bloat_sources):
        return

    # Multi-line LOUD warn — single-line stderr is too easy to lose in
    # apptainer's `WARNING: While bind mounting ...` flood. Banner +
    # bullet block + bloat-source listing + concrete next-step commands
    # so even a half-distracted reviewer of the start log spots it.
    mb = audit.bytes / (1024 * 1024)
    lines = [
        "",
        "=" * 72,
        "F-CS8 WARNING — heavy workdir/.claude/ detected",
        "=" * 72,
        f"  workdir       : {audit.workdir}/.claude/",
        f"  file count    : {audit.files:,}"
        f" (threshold {audit.threshold_files:,})"
        f"{'  <-- OVER' if audit.exceeded_files else ''}",
        f"  total bytes   : {mb:.1f} MB"
        f" (threshold {audit.threshold_bytes / (1024 * 1024):.1f} MB)"
        f"{'  <-- OVER' if audit.exceeded_bytes else ''}",
        "",
        "claude-agent-sdk walks <workdir>/.claude/ at session start to",
        "discover hooks/skills/settings. A heavy walk either silently times",
        "out spawning MCP servers (the bun telegrammer server is the canary",
        "— it just won't show up in the SDK's tool surface) or makes the",
        "agent return 0 tokens per turn with no log line. Verified field",
        "failure: proj-scitex-orochi at 41,873 files / 884 MB (2026-06-03).",
        "",
    ]
    if audit.bloat_sources:
        lines.append("  bloat sources (sub-dirs above the per-bucket threshold):")
        for s in audit.bloat_sources:
            sub_mb = s.bytes / (1024 * 1024)
            lines.append(
                f"    - .claude/{s.rel_path}/  {s.files:,} files / {sub_mb:.1f} MB"
            )
        lines.append("")
        lines.append("  Immediate remediation (parks data, does NOT delete):")
        for s in audit.bloat_sources:
            safe = s.rel_path.replace("/", "_")
            lines.append(
                f"    mv {audit.workdir}/.claude/{s.rel_path}"
                f" {audit.workdir}/.claude-{safe}-parked-$(date +%Y-%m-%d)"
            )
        lines.append("")
        lines.append("  Then `sac agents stop <NAME> && sac agents start <NAME>` to")
        lines.append("  verify the SDK picks up the lean tree on next boot.")
    else:
        lines.append("  no single sub-dir is over the per-bucket threshold —")
        lines.append("  the bloat is distributed across the whole tree.")
        lines.append("")
        lines.append("  Recommend a project-specific workdir (e.g.")
        lines.append("  /home/<you>/proj/<this-project>/) or /tmp/<scratch>/,")
        lines.append("  then reference other repos via absolute paths.")
    lines.append("=" * 72)
    lines.append("")
    print("\n".join(lines), file=sys.stderr, flush=True)


# 2026-05-13 docker/podman ripout: apptainer is the only accepted
# runtime. Empty / unset ``spec.runtime`` is treated as ``apptainer``.
_CONTAINER_ENGINES: tuple[str, ...] = ("apptainer",)


def _container_runtime_for(config: AgentConfig):
    """Return the apptainer container runtime, or None for an
    unrecognised ``spec.runtime``.
    """
    runtime = getattr(config, "runtime", "") or "apptainer"
    # Both the legacy ``apptainer`` value and the current
    # ``claude-agent-sdk`` selector dispatch the headless SDK runner via
    # ``apptainer exec`` — the registry (_runtime_select) maps BOTH to
    # ClaudeSessionRuntime, so both must resolve to a container runtime
    # here. Previously only ``apptainer`` did, so ``runtime:
    # claude-agent-sdk`` fell through to None and start() failed loud with
    # "requires docker|podman" — despite docker having been ripped out
    # (apptainer is the only engine). This made the recommended value
    # unusable while the deprecated alias worked; both now resolve.
    if runtime in ("apptainer", "claude-agent-sdk"):
        from ._apptainer_runtime import ApptainerContainerRuntime

        return ApptainerContainerRuntime()
    return None


class ClaudeSessionRuntime(RuntimeBase):
    """Daemon-mode runtime backed by ``claude-agent-sdk``, dispatched
    via apptainer. The host side never spawns a Python subprocess —
    every ``start`` goes through ``apptainer exec`` (or equivalent).

    ``container_runtime_for`` is an injection seam — defaults to the
    module-level ``_container_runtime_for`` lookup. Tests pass a
    callable that returns a fake container runtime so the dispatch
    glue can be exercised without booting a real container.
    """

    def __init__(self, container_runtime_for=None):
        self._container_runtime_for = container_runtime_for or _container_runtime_for

    def _setup_workspace(self, config: AgentConfig) -> None:
        """Materialise CLAUDE.md before launching the SDK runner.

        ADR-0003 (D6/D7): the agent's container ``$HOME`` is bind-mounted
        from ``runtime/<name>/home/``. We materialise ``to_home/`` and
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
        _materialize_home_layouts(config, home_dir)
        # Relaxed apptainer specs declare their own ``--home``/``--overlay``
        # in raw_args; under that combo the workspace-home bind above is
        # shadowed and the to_home tree never reaches the container $HOME.
        # Mirror the same tree into the overlay's upper home so it lands as
        # part of the container filesystem (no-op for non-overlay specs).
        from ._to_home_overlay import deploy_to_home_overlay

        deploy_to_home_overlay(config)

    def _cleanup_workspace(self, config: AgentConfig) -> None:
        """Remove the agent-container CLAUDE.md section on stop."""
        required_attrs = ("expanded_workdir", "skills", "claude", "env", "labels")
        if not all(hasattr(config, a) for a in required_attrs):
            return
        home_dir = str(self._state_dir(config) / "home")
        cleanup_claude_md(config, home_dir)

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

        ``no_preflight`` is currently a no-op (kept for API parity
        with the legacy bare-metal runtime). ``foreground=True``
        attaches the operator's stdio to the container; daemon mode
        (the default) returns once the engine reports the container
        is live.

        ``one_shot`` is accepted for API parity with the CLI:
        ``sac agents start --one-shot`` threads
        ``start_kw["one_shot"] = True`` through
        ``runtime.start(config, **start_kw)`` in
        ``_lifecycle/_start.py``. At this runtime layer it is a
        no-op (mirroring the ``no_preflight`` precedent above) —
        the one-shot termination semantics are governed by
        ``spec.autonomous.drive_until="DONE"`` in the agent's
        spec.yaml, not by a runtime flag. Accepting the kwarg here
        keeps the CLI ↔ runtime contract aligned so the
        ``--broker-self --foreground --one-shot`` launcher path
        (cohort-A nested-sac dispatch) does not crash with
        ``TypeError: ClaudeSessionRuntime.start() got an
        unexpected keyword argument 'one_shot'``. The kwarg is
        forwarded to the underlying container runtime so any
        future support there works without another signature
        change.
        """
        # Operator directive 12870 (lead a2a b58dd5d3): emit the legacy
        # ``runtime: apptainer`` deprecation HERE (real start path), not
        # in ``_get_runtime`` (status / list / discovery walks).
        from .._lifecycle._runtime_select import warn_if_legacy_apptainer_runtime

        warn_if_legacy_apptainer_runtime(config)

        container_rt = self._container_runtime_for(config)
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
            one_shot=one_shot,
        )

    def stop(self, config: AgentConfig) -> bool:
        """Stop the container; scrub the managed CLAUDE.md section."""
        container_rt = self._container_runtime_for(config)
        if container_rt is None:
            return False
        ok = container_rt.stop(config)
        self._cleanup_workspace(config)
        return ok

    def is_running(self, config: AgentConfig) -> bool:
        container_rt = self._container_runtime_for(config)
        if container_rt is None:
            return False
        return container_rt.is_running(config)

    def logs(self, config: AgentConfig, lines: int = 50) -> str:
        """Prefer the rendered ``session.jsonl`` tail (host-side via
        /state bind-mount). Fall through to ``docker logs --tail N``
        when the transcript hasn't been written yet — typical for a
        brand-new container that hasn't completed its first turn.
        """
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
