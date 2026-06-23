"""``sac agents explain <name>`` — render an agent's FULL effective launch plan.

Shows exactly what ``sac agents start`` will mount, where it opens, and what it
injects — so the caller understands what happens BEFORE it happens (the
constitution's No-Surprise rule). The mounts + ``--pwd`` are parsed from the
SAME ``build_run_argv`` the runtime launches, so the plan cannot drift from
reality. Resolution honours the project-over-user cascade (a repo's
``.scitex/agent-container/agents/<name>`` wins over the user-scope copy).
"""

from __future__ import annotations

import re
from pathlib import Path

import click

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


_SECRET_ENV = re.compile(
    r"(SECRET|TOKEN|BEARER|PASSWORD|API_KEY|_KEY|CREDENTIAL)", re.IGNORECASE
)


def _redact(entry: str) -> str:
    """Mask the VALUE of a secret-named env var — never echo a key/token."""
    key, sep, val = entry.partition("=")
    if sep and val and _SECRET_ENV.search(key):
        return f"{key}=<redacted: {len(val)} chars>"
    return entry


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


def render_plan(config: AgentConfig, *, spec_path: Path | None = None) -> str:
    """Return the human-readable effective launch plan for ``config``."""
    argv = _argv_for(config)
    binds = _binds(argv)
    pwd = argv[argv.index("--pwd") + 1] if "--pwd" in argv else "(none)"
    sif = next((a for a in argv if isinstance(a, str) and a.endswith(".sif")), "(none)")
    claude = getattr(config, "claude", None)
    labels = getattr(config, "labels", {}) or {}

    lines: list[str] = []
    lines.append(f"Agent: {config.name}")
    meta = ", ".join(f"{k}={v}" for k, v in labels.items() if k in ("project", "role"))
    if meta:
        lines.append(f"  {meta}")
    if spec_path is not None:
        lines.append(f"  spec: {spec_path}")
    lines.append(f"  runtime: {getattr(config, 'runtime', '?')}   image: {sif}")
    account = getattr(claude, "account", "") or ""
    creds = getattr(claude, "credentials_file", "") or ""
    if account or creds:
        lines.append(f"  account: {account or '(host live)'}   creds: {creds or '-'}")

    lines.append("")
    backed = _pwd_is_backed(pwd, binds)
    flag = "✓ backed by a bind" if backed else "⚠ NOT backed by any bind — no cwd!"
    lines.append(f"Workdir (--pwd): {pwd}   [{flag}]")

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

    return "\n".join(lines)


@click.command("explain")
@click.argument("name")
def explain(name: str) -> None:
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
    config = load_config(str(spec))
    click.echo(render_plan(config, spec_path=spec))
