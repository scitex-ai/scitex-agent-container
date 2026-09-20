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
from ._explain_engine import engine_lines


def _channel_lines(config: AgentConfig, channels: list[str]) -> list[str]:
    """Show declarations, resolved ingress and tool exposure as distinct facts."""
    from scitex_dev.status import Check, StatusCode

    declared = set(channels)
    mcp_names = set(getattr(config, "mcp_servers", {}) or {})
    checks = []
    checks.append(
        Check.ok(
            "sac_inbound_delivery",
            "server:sac resolves to the explicit-ack SAC inbox consumer and the "
            "canonical turn-exchange ledger",
        )
        if "server:sac" in declared
        else Check.not_ok(
            "sac_inbound_delivery",
            "server:sac is not declared",
            "add server:sac to spec.comms.channels",
            cause=StatusCode(
                kind="scitex",
                code="NOT_RESOLVABLE",
                message="server:sac is not declared; inspect `sac agents explain`",
            ),
        )
    )
    checks.append(
        Check.ok(
            "cards_inbound_delivery",
            "server:scitex-cards resolves to durable poll, terminal-visible turn, "
            "then positive confirmation",
        )
        if "server:scitex-cards" in declared
        else Check.not_ok(
            "cards_inbound_delivery",
            "server:scitex-cards is not declared",
            "add server:scitex-cards to spec.comms.channels",
        )
    )
    if "server:scitex-cards" in declared:
        from ..runtimes._channel_inbox_dispatcher_lifecycle import (
            cards_store_check,
            effective_cards_store,
        )

        cards_env, cards_store = effective_cards_store(config)
        checks.append(cards_store_check(config.name, cards_store, cards_env))
    card_tools = bool({"cards", "scitex-cards"} & mcp_names)
    checks.append(
        Check.ok(
            "cards_tools",
            "the scitex-cards MCP server is declared as a tools-only surface; "
            "tools-only mode; this is tool exposure, not proof of inbound delivery",
        )
        if card_tools
        else Check.not_ok(
            "cards_tools",
            "Cards inbound delivery is declared but no scitex-cards MCP tool server "
            "is present",
            "declare spec.mcp_servers.scitex-cards if this agent must operate Cards",
            cause=StatusCode(
                kind="scitex",
                code="NOT_RESOLVABLE",
                message="the Cards MCP server does not resolve; inspect `sac agents explain`",
            ),
        )
    )
    if (
        "server:claude-code-telegrammer" in declared
        and getattr(config, "harness", "") == "hermes"
    ):
        from ..runtimes._cct_rail_verdict import RAIL_UP, assess_cct_rail

        rail = assess_cct_rail(config)
        checks.append(
            Check.ok("cct_inbound_delivery", rail.detail)
            if rail.state == RAIL_UP
            else Check.unknown(
                "cct_inbound_delivery",
                rail.detail,
                rail.remedy(),
            )
        )
    else:
        checks.append(
            Check.ok(
                "cct_inbound_delivery",
                "server:claude-code-telegrammer is omitted by declaration; CCT is optional",
            )
        )
    lines = ["Channel resolution:"]
    for check in checks:
        wire = check.to_dict()
        state = {True: "resolved", False: "unavailable", None: "unknown"}[wire["ok"]]
        lines.append(f"  {check.name}: {state} — {check.detail}")
        if check.hint:
            lines.append(f"    Hint: {check.hint}")
    return lines


def _spec_path_for(name: str) -> Path | None:
    """Resolve ``name`` → its spec.yaml via the project-over-user cascade."""
    from ._helpers._agent_list import _discover_defined_agents

    for agent_name, spec in _discover_defined_agents():
        if agent_name == name:
            return spec
    return None


def _argv_for(config: AgentConfig) -> list[str]:
    """The real launch argv, resolved through the selected runtime adapter.

    SIF resolution is best-effort — when no SIF resolves (apptainer absent) we
    still render the plan with a visible ``<unresolved>`` placeholder rather
    than failing, so ``explain`` works anywhere.
    """
    from .._lifecycle._runtime_select import _get_runtime

    runtime = _get_runtime(config)

    # TUI adapters own their full argv builder because the harness command is
    # part of that adapter.  Using it here keeps explain on the same selection
    # path as start without teaching this module Claude/Codex argv details.
    default_argv = getattr(runtime, "_default_argv", None)
    if callable(default_argv):
        argv = default_argv(config)
        if argv is not None:
            return argv

    # Headless wrapper adapters delegate the actual container launch to their
    # selected container runtime.  Hermes is itself that runtime, so both paths
    # converge here without a harness-specific fallback.
    container = runtime
    container_factory = getattr(runtime, "_container_runtime_for", None)
    if callable(container_factory):
        container = container_factory(config)
    if container is None:
        raise RuntimeError(
            f"{type(runtime).__name__} cannot resolve its container runtime"
        )

    resolve_sif = getattr(container, "resolve_sif", None)
    build_argv = getattr(container, "build_run_argv", None)
    if not callable(resolve_sif) or not callable(build_argv):
        raise RuntimeError(
            f"{type(runtime).__name__} does not expose a launch-plan argv adapter"
        )

    state_dir_fn = getattr(runtime, "_state_dir", None)
    if not callable(state_dir_fn):
        state_dir_fn = getattr(container, "_state_dir", None)
    if not callable(state_dir_fn):
        raise RuntimeError(
            f"{type(runtime).__name__} does not expose a state directory"
        )

    sif = resolve_sif(config)
    sif_path = sif if sif is not None else Path(config.image or "<unresolved>.sif")
    return build_argv(config, state_dir=state_dir_fn(config), sif_path=sif_path)


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


def _uvwork_line(config: AgentConfig) -> str:
    """One line saying where ``/uvwork`` comes from — ADR-0024.

    ``explain`` renders binds by walking the argv, so the ONE outcome it could
    not show is the one that emits no bind: a host with no resolvable scratch
    root. That is precisely the case an operator needs named, because
    ``start`` will REFUSE on it (``_apptainer_scratch.ensure_uvwork_for_launch``)
    while ``explain`` stays read-only. So the plan states the decision itself
    rather than leaving the reader to notice an absence.
    """
    from ..runtimes._apptainer_scratch import plan_uvwork_bind

    plan = plan_uvwork_bind(config)
    prefix = "⚠ /uvwork — `start` WILL REFUSE" if plan.refused else "/uvwork"
    return f"{prefix}: {plan.reason}"


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


def _worktree_policy_lines(config: AgentConfig) -> list[str]:
    """Read-only resolution through the same executable gate as start."""
    from .._lifecycle._worktree_policy import (
        WorktreePolicyError,
        enforce_task_worktree_policy,
    )

    try:
        proof = enforce_task_worktree_policy(config, provision=False)
    except WorktreePolicyError as exc:
        return ["Worktree policy: START WILL REFUSE", f"  {exc}"]
    if proof is None:
        return ["Worktree policy: not applicable (AgentProxy)"]
    return [
        f"Worktree policy: {proof.worktree_action}",
        f"  authored: {proof.authored_workdir}",
        f"  resolved: {proof.resolved_workdir}",
        f"  branch: {proof.branch}",
        f"  policy_sha256: {proof.policy_sha256}",
        f"  projection_sha256: {proof.projection_sha256}",
    ]


def _delegation_line(config: AgentConfig) -> str:
    """Effective spawn permission and child bound from the loaded spec."""
    allowed = bool(getattr(getattr(config, "lineage", None), "may_spawn", True))
    policy = getattr(config, "delegation", None)
    maximum = getattr(policy, "max_concurrent_children", 2)
    isolated = bool(getattr(policy, "worktree_isolation", True))
    state = "enabled" if allowed else "disabled (delegate_task removed)"
    isolation = "requested" if isolated else "off"
    return (
        f"Delegation: {state}; max children: {maximum}; "
        f"Git worktree isolation: {isolation}"
    )


def _a2a_line(config: AgentConfig, *, port_reader=None) -> str:
    """Render configured intent beside the durable resolved bridge port."""
    from .._lifecycle._status import _a2a_status

    status = _a2a_status(config.name, config, port_reader=port_reader)
    configured = status["configured_port"]
    resolved = status["resolved_port"]
    return (
        f"A2A port: configured={configured if configured is not None else 'none'}; "
        f"resolved={resolved if resolved is not None else 'none'}; "
        f"source={status['resolution_source']}"
    )


def render_plan_summary(config: AgentConfig, *, spec_path: Path | None = None) -> str:
    """Short variant of :func:`render_plan` for ``sac agents start``'s
    refuse-without-``--yes`` preview.

    Reuses the same already-computed identity/workdir/model pieces as the
    full plan, but stops there — no Mounts, Env, Flags/Channels, Skills,
    Startup prompts, Hooks, Instruction sections, Settings sources, or Host
    deep-merge. Use ``sac agents explain <name>`` (``render_plan``) for the
    full detail.
    """
    policy_lines = _worktree_policy_lines(config)
    argv = _argv_for(config)
    binds = _binds(argv)
    pwd = argv[argv.index("--pwd") + 1] if "--pwd" in argv else "(none)"
    sif = next((a for a in argv if isinstance(a, str) and a.endswith(".sif")), "(none)")
    claude = getattr(config, "claude", None)

    lines = _identity_lines(config, spec_path=spec_path, sif=sif, claude=claude)
    lines.append("")
    lines.append(_workdir_line(pwd, binds))
    lines.extend(policy_lines)

    model = getattr(claude, "model", "") or getattr(config, "model", "")
    lines.append("")
    lines.append(f"Model: {model}")
    lines.append(_a2a_line(config))
    lines.append(_delegation_line(config))
    return "\n".join(lines)


def render_plan(config: AgentConfig, *, spec_path: Path | None = None) -> str:
    """Return the human-readable effective launch plan for ``config``."""
    policy_lines = _worktree_policy_lines(config)
    argv = _argv_for(config)
    binds = _binds(argv)
    pwd = argv[argv.index("--pwd") + 1] if "--pwd" in argv else "(none)"
    sif = next((a for a in argv if isinstance(a, str) and a.endswith(".sif")), "(none)")
    claude = getattr(config, "claude", None)

    lines = _identity_lines(config, spec_path=spec_path, sif=sif, claude=claude)

    # WHICH ENGINE, AND WHO DECIDED — the only defence against a fleet
    # engine library that has diverged between hosts. See _explain_engine.
    lines.append("")
    lines += engine_lines(config, spec_path)

    lines.append("")
    lines.append(_workdir_line(pwd, binds))
    lines.extend(policy_lines)

    lines.append("")
    lines.append("Mounts (apptainer.binds — the single source of truth):")
    width = max((len(s) for s, _d, _m in binds), default=0)
    for src, dst, mode in binds:
        note = _annotate(src, dst)
        note = f"   [{note}]" if note else ""
        lines.append(f"  {src:<{width}}  →  {dst}  ({mode}){note}")

    lines.append("")
    lines.append(_uvwork_line(config))

    envs = _envs(argv)
    if envs:
        lines.append("")
        lines.append("Env (--env):")
        for e in envs:
            lines.append(f"  {e}")

    model = getattr(claude, "model", "") or getattr(config, "model", "")
    flags = getattr(claude, "flags", []) or []
    channels = getattr(getattr(config, "comms", None), "channels", []) or []
    lines.append("")
    lines.append(f"Model: {model}")
    lines.append(_a2a_line(config))
    lines.append(_delegation_line(config))
    if flags:
        lines.append(f"Flags: {' '.join(flags)}")
    if channels:
        lines.append(f"Channels: {', '.join(channels)}")
    channel_resolution = _channel_lines(config, channels)
    if channel_resolution:
        lines += channel_resolution

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
