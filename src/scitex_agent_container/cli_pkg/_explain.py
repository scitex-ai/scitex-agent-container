"""``sac agents explain <name>`` — render an agent's FULL effective launch plan.

Shows exactly what ``sac agents start`` will mount, where it opens, and what it
injects — so the caller understands what happens BEFORE it happens (the
constitution's No-Surprise rule). The mounts + ``--pwd`` are parsed from the
SAME ``build_run_argv`` the runtime launches, so the plan cannot drift from
reality. Resolution honours the project-over-user cascade (a repo's
``.scitex/agent-container/agents/<name>`` wins over the user-scope copy).
"""

from __future__ import annotations

from pathlib import Path

import click

from .._state._meta.secrets import _SECRET_ENV  # noqa: F401 (re-exported, back-compat)
from .._state._meta.secrets import _redact_env_entry as _redact
from ..config import AgentConfig, load_config


def _spec_path_for(name: str) -> Path | None:
    """Resolve ``name`` → its spec.yaml via the project-over-user cascade."""
    from ._helpers._agent_list import _discover_defined_agents

    for agent_name, spec in _discover_defined_agents():
        if agent_name == name:
            return spec
    return None


def _argv_for(config: AgentConfig) -> list[str]:
    """The real launch argv (binds + --pwd come straight from build_run_argv).

    SIF resolution is best-effort — when no SIF resolves (apptainer absent) we
    still render the plan with a visible ``<unresolved>`` placeholder rather
    than failing, so ``explain`` works anywhere.
    """
    from ..runtimes._apptainer_build_argv import build_run_argv
    from ..runtimes._apptainer_runtime import ApptainerContainerRuntime
    from ..runtimes.tui_session import state_dir_for_config

    sif = ApptainerContainerRuntime().resolve_sif(config)
    sif_path = sif if sif is not None else Path(config.image or "<unresolved>.sif")
    return build_run_argv(
        config, state_dir=state_dir_for_config(config), sif_path=sif_path, tui=True
    )


def _binds(argv: list[str]) -> list[tuple[str, str, str]]:
    """``[(src, dst, mode)]`` for every ``--bind`` in the argv."""
    out: list[tuple[str, str, str]] = []
    for i, a in enumerate(argv):
        if a == "--bind" and i + 1 < len(argv):
            parts = argv[i + 1].split(":")
            src = parts[0]
            dst = parts[1] if len(parts) > 1 else parts[0]
            mode = parts[2] if len(parts) > 2 else "rw"
            out.append((src, dst, mode))
    return out


def _envs(argv: list[str]) -> list[str]:
    return [
        _redact(argv[i + 1])
        for i, a in enumerate(argv)
        if a == "--env" and i + 1 < len(argv)
    ]


def _annotate(src: str, dst: str) -> str:
    home = str(Path.home())
    if src == home and dst == home:
        return "whole-home — FULL host reach"
    if dst.startswith("/state/"):
        return "sac state"
    if dst.endswith("/.ssh"):
        return "git/ssh identity"
    if dst.endswith("/.config/gh"):
        return "gh auth"
    if "credentials.json" in dst:
        return "claude credentials"
    if dst.endswith("/.scitex/todo"):
        return "shared todo store"
    return ""


def _pwd_is_backed(pwd: str, binds: list[tuple[str, str, str]]) -> bool:
    """True iff ``--pwd`` is at/under some bind target (so the cwd exists)."""
    for _src, dst, _mode in binds:
        if pwd == dst or pwd.startswith(dst.rstrip("/") + "/"):
            return True
    return False


def _hook_label(command: str) -> str:
    """Readable name for a hook command: the script basename, else the command."""
    if not command:
        return command
    first = command.split()[0]
    if "/" in first:
        return first.rsplit("/", 1)[-1]
    return command[:48]


def _materialized_hooks_and_sections(
    config: AgentConfig,
) -> tuple[dict[str, list[str]], list[str]]:
    """Effective hooks + CLAUDE.md section titles the agent will actually get.

    Runs the EXACT production materializers (setup_claude_md → deploy_to_home →
    setup_settings_json) into a THROWAWAY directory and reads the result back —
    so the shown set is ground truth (the same merge ``start`` does), with no
    drift and no writes to the agent's real home. The temp dir is always
    removed.
    """
    import json as _json
    import shutil
    import tempfile

    from ..runtimes._to_home import deploy_to_home
    from ..runtimes.claude_md import setup_claude_md
    from ..runtimes.settings_json import setup_settings_json

    tmp = tempfile.mkdtemp(prefix="sac-explain-")
    try:
        setup_claude_md(config, tmp)
        deploy_to_home(config, tmp)
        setup_settings_json(config, tmp, filename="settings.json")
        claude_dir = Path(tmp) / ".claude"

        hooks: dict[str, list[str]] = {}
        settings_path = claude_dir / "settings.json"
        if settings_path.is_file():
            data = _json.loads(settings_path.read_text())
            for event, blocks in (data.get("hooks") or {}).items():
                names = [
                    _hook_label(h.get("command", ""))
                    for blk in blocks
                    for h in blk.get("hooks", [])
                ]
                if names:
                    hooks[event] = names

        sections: list[str] = []
        claude_md = claude_dir / "CLAUDE.md"
        if claude_md.is_file():
            for raw in claude_md.read_text().splitlines():
                line = raw.strip()
                if line.startswith("## "):
                    sections.append(line[3:].strip())
                elif line.startswith("# ") and not line.startswith("## "):
                    sections.append(f"{line[2:].strip()} (title)")
        return hooks, sections
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _identity_lines(
    config: AgentConfig, *, spec_path: Path | None, sif: str, claude: object
) -> list[str]:
    """Agent identity header: name, project/role, spec path, runtime/image,
    account/creds — the part every plan variant (full + summary) starts with.
    """
    labels = getattr(config, "labels", {}) or {}
    lines: list[str] = [f"Agent: {config.name}"]
    meta = ", ".join(f"{k}={v}" for k, v in labels.items() if k in ("project", "role"))
    if meta:
        lines.append(f"  {meta}")
    if spec_path is not None:
        lines.append(f"  spec: {spec_path}")
    lines.append(
        "  profile: "
        f"{getattr(config, 'profile', 'default')}   "
        f"harness: {getattr(config, 'harness', 'claude-code')}   "
        f"backend: {getattr(config, 'backend', 'anthropic')}"
    )
    lines.append(f"  runtime: {getattr(config, 'runtime', '?')}   image: {sif}")
    account = getattr(claude, "account", "") or ""
    creds = getattr(claude, "credentials_file", "") or ""
    if account or creds:
        lines.append(f"  account: {account or '(host live)'}   creds: {creds or '-'}")
    return lines


def _workdir_line(pwd: str, binds: list[tuple[str, str, str]]) -> str:
    """The ``Workdir (--pwd): ...`` line, with the backed-by-a-bind check."""
    backed = _pwd_is_backed(pwd, binds)
    flag = "✓ backed by a bind" if backed else "⚠ NOT backed by any bind — no cwd!"
    return f"Workdir (--pwd): {pwd}   [{flag}]"


def render_plan_summary(config: AgentConfig, *, spec_path: Path | None = None) -> str:
    """Short variant of :func:`render_plan` for ``sac agents start``'s
    refuse-without-``--yes`` preview.

    Reuses the same already-computed identity/workdir/model pieces as the
    full plan, but stops there — no Mounts, Env, Flags/Channels, Skills,
    Startup prompts, Hooks, Instruction sections, Settings sources, or Host
    deep-merge. Use ``sac agents explain <name>`` (``render_plan``) for the
    full detail.
    """
    argv = _argv_for(config)
    binds = _binds(argv)
    pwd = argv[argv.index("--pwd") + 1] if "--pwd" in argv else "(none)"
    sif = next((a for a in argv if isinstance(a, str) and a.endswith(".sif")), "(none)")
    claude = getattr(config, "claude", None)

    lines = _identity_lines(config, spec_path=spec_path, sif=sif, claude=claude)
    lines.append("")
    lines.append(_workdir_line(pwd, binds))

    model = getattr(claude, "model", "") or getattr(config, "model", "")
    lines.append("")
    lines.append(f"Model: {model}")
    return "\n".join(lines)


def render_plan(config: AgentConfig, *, spec_path: Path | None = None) -> str:
    """Return the human-readable effective launch plan for ``config``."""
    argv = _argv_for(config)
    binds = _binds(argv)
    pwd = argv[argv.index("--pwd") + 1] if "--pwd" in argv else "(none)"
    sif = next((a for a in argv if isinstance(a, str) and a.endswith(".sif")), "(none)")
    claude = getattr(config, "claude", None)

    lines = _identity_lines(config, spec_path=spec_path, sif=sif, claude=claude)

    lines.append("")
    lines.append(_workdir_line(pwd, binds))

    lines.append("")
    lines.append("Mounts (apptainer.binds — the single source of truth):")
    width = max((len(s) for s, _d, _m in binds), default=0)
    for src, dst, mode in binds:
        note = _annotate(src, dst)
        note = f"   [{note}]" if note else ""
        lines.append(f"  {src:<{width}}  →  {dst}  ({mode}){note}")

    envs = _envs(argv)
    if envs:
        lines.append("")
        lines.append("Env (--env):")
        for e in envs:
            lines.append(f"  {e}")

    model = getattr(claude, "model", "") or getattr(config, "model", "")
    flags = getattr(claude, "flags", []) or []
    channels = getattr(claude, "channels", []) or []
    lines.append("")
    lines.append(f"Model: {model}")
    if flags:
        lines.append(f"Flags: {' '.join(flags)}")
    if channels:
        lines.append(f"Channels: {', '.join(channels)}")

    try:
        from ..runtimes.claude_md import build_skills_lines

        skills = [line for line in build_skills_lines(config) if line.startswith("@")]
        if skills:
            lines.append("")
            lines.append(f"Skills (@-imports, {len(skills)}):")
            for s in skills:
                lines.append(f"  {s}")
    except Exception:  # stx-allow: fallback (explain is best-effort; never crash)
        pass

    prompts = list(getattr(config, "startup_prompts", []) or [])
    if prompts:
        lines.append("")
        lines.append(f"Startup prompts: {len(prompts)}")
        for idx, p in enumerate(prompts):
            n = str(p)
            tag = " ⚠ long" if (len(n) > 600 or n.count("\n") + 1 > 8) else ""
            lines.append(
                f"  [{idx}] {len(n)} chars / {n.count(chr(10)) + 1} lines{tag}"
            )

    # Hooks + instruction sections that materialize into the agent's $HOME —
    # the part that used to be invisible. Read back from a throwaway
    # materialization using the EXACT production materializers (ground truth,
    # no drift, no writes to the real home). Best-effort: never crash explain.
    try:
        hooks, sections = _materialized_hooks_and_sections(config)
        if hooks:
            total = sum(len(v) for v in hooks.values())
            lines.append("")
            lines.append(f"Hooks (materialized, {total} total):")
            for event, names in hooks.items():
                lines.append(f"  {event} ({len(names)}): {', '.join(names)}")
        if sections:
            lines.append("")
            lines.append("Instruction sections ($HOME/.claude/CLAUDE.md):")
            for title in sections:
                lines.append(f"  • {title}")
    except Exception:  # stx-allow: fallback (explain is best-effort; never crash)
        pass

    # Settings provenance (ADR-0018): which to_home layer owns each top-level
    # settings.json key, so cross-layer drift / overrides are visible before
    # launch. Best-effort: never crash explain.
    try:
        from ..runtimes._to_home import settings_layer_dirs
        from ..runtimes._to_home_settings import settings_cascade_provenance

        prov = settings_cascade_provenance(settings_layer_dirs(config))
        if prov:
            owners: dict[str, set[str]] = {}
            for path, layer in prov.items():
                owners.setdefault(path.split(".", 1)[0], set()).add(layer)
            lines.append("")
            lines.append("Settings sources (settings.json key → to_home layer):")
            for key in sorted(owners):
                lines.append(f"  {key}: {', '.join(sorted(owners[key]))}")
    except Exception:  # stx-allow: fallback (explain is best-effort; never crash)
        pass

    # Host deep-merge (developer agents): how many host
    # ~/.claude/{commands,skills,hooks} files this agent links in, plus any
    # drift vs. the live host. Capsule/solitary agents show "off". Best-effort:
    # never crash explain. Materializes into a throwaway home (ground truth).
    try:
        lines.extend(_host_merge_lines(config))
    except Exception:  # stx-allow: fallback (explain is best-effort; never crash)
        pass

    return "\n".join(lines)


def _host_merge_lines(config: AgentConfig) -> "list[str]":
    """Host deep-merge summary for ``sac agents explain`` (ground-truth read).

    Runs the production host-merge into a THROWAWAY home and reports the count
    of linked host files per ``.claude`` subdir plus any drift the verifier
    finds — so the operator sees, before launch, whether a developer agent's
    host overlay is healthy. Empty list for a non-developer agent's "off" line
    is still shown so the gate decision is visible.
    """
    import shutil
    import tempfile

    from ..runtimes._host_merge import (
        apply_host_merge,
        is_full_developer,
        verify_host_merge,
    )
    from ..runtimes._to_home import deploy_to_home

    out: list[str] = ["", "Host deep-merge (~/.claude → $HOME/.claude):"]
    if not is_full_developer(config):
        out.append("  off (not a full-developer agent — agent layers only)")
        return out
    tmp = tempfile.mkdtemp(prefix="sac-hostmerge-")
    try:
        deploy_to_home(config, tmp)
        created = apply_host_merge(config, tmp)
        by_dir: dict[str, int] = {}
        for link in created:
            # climb to the .claude/<subdir> name
            parts = link.relative_to(Path(tmp) / ".claude").parts
            key = parts[0] if parts else "?"
            by_dir[key] = by_dir.get(key, 0) + 1
        summary = ", ".join(f"{k}={by_dir[k]}" for k in sorted(by_dir)) or "0 files"
        out.append(f"  on — linked host files: {summary}")
        drift = verify_host_merge(config, tmp)
        if drift:
            out.append(f"  ⚠ DRIFT ({len(drift)}):")
            for d in drift:
                out.append(f"    - {d}")
        else:
            out.append("  ✓ no drift (matches live host + agent layers)")
        return out
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


@click.command("explain")
@click.argument("name")
@click.option(
    "--profile",
    metavar="NAME",
    help="Select a named launch profile (defaults to spec.default_profile).",
)
def explain(name: str, profile: str | None) -> None:
    """Render the FULL effective launch plan for agent NAME (no launch).

    Mounts + --pwd are parsed from the same build_run_argv the runtime uses,
    so what you see is exactly what `sac agents start` will do.
    """
    spec = _spec_path_for(name)
    if spec is None:
        raise click.ClickException(
            f"no agent named '{name}' found under any agents/ tree "
            "(project-scope .scitex/agent-container/agents/ or "
            "~/.scitex/agent-container/agents/). Run `sac agents list`."
        )
    try:
        config = load_config(str(spec), profile=profile)
    except ValueError as exc:
        raise click.ClickException(str(exc)) from exc
    click.echo(render_plan(config, spec_path=spec))
