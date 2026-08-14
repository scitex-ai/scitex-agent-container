"""`sac start` cold-start target parser (operator TODO 2026-06-17).

`sac start` accepts four convenience forms in addition to a plain agent name,
so an operator can launch an agent for an arbitrary project dir without first
hand-writing a spec:

  1. ``<label>@<host>:/path/to/workdir/``  — explicit label, host, workdir
  2. ``<host>:/path/to/workdir/``          — label = basename(workdir)
  3. ``/path/to/workdir/`` (or ``./rel``)  — host = the caller's host
  4. ``.``                                 — workdir = $CWD, host = caller

:func:`parse_start_target` is a PURE classifier: it returns a
:class:`ColdStartTarget` for the four forms, ``None`` for a plain agent name
(``proj-figrecipe`` → resolve through the existing registry flow), and raises
:class:`ColdStartParseError` on a malformed form (fail-fast, fail-loud, no
silent fallback — operator directive). Materialization + launch live in the
caller (:mod:`._start`); this module only decides "what did the operator mean?".
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

# Agent-name charset (mirrors cli_pkg._create validation): lowercase letters,
# digits, hyphen, underscore; must start with a letter.
_VALID_LABEL = re.compile(r"^[a-z][a-z0-9_-]*$")

# Standardized TUI spec for a cold-started agent. EVERY field is written
# explicitly — red-start ruling 2026-07-21 (superseding the 2026-06-23
# subset): an omitted field is a load ERROR, so anything sac itself renders
# must carry the complete required set. Non-curated values sit at their
# defaults. The sac MCP server + ``server:sac`` channel are still
# auto-injected by the loader. ``host`` is always set (dispatch runs remote
# when it names a peer).
_COLD_START_SPEC = """\
# {label} — cold-started by `sac start` ({stamp_note}).
# Standardized TUI spec; edit freely or `sac agents create` for the full tour.
apiVersion: scitex-agent-container/v3
kind: Agent
metadata:
  labels:
    project: {label}
spec:
  runtime: tui
  harness: anthropic
  host: {host}
  workdir: {workdir}
  python-venv: ""
  user: ""
  to_home: ./to_home
  startup_commands: []
  startup_prompts: []
  listen: []
  extensions: {{}}
  mcp_servers: {{}}
  container:
    image: scitex-agent-container:latest
    volumes: []
    network: host
    mount_host_claude: false
  apptainer:
    # Default SIF, named explicitly. ``binds: []`` = no extra mounts; apptainer's
    # default $HOME mount makes the workdir reachable. Add binds for more reach.
    image: ~/.scitex/agent-container/containers/scitex-agent-container.sif
    binds: []
    env: {{}}
    raw_args: []
    post: ""
    environment: {{}}
    def_file: ""
    nv: false
    rocm: false
    overlay: ""
    overlay_size: ""
    overlay_create_if_missing: true
    tmpfs_size: 2G
    relaxed: false
    fakeroot: false
    jail: false
    nested_build: false
  claude:
    model: sonnet
    flags:
      - --dangerously-skip-permissions
    channels: []
    raw_options: {{}}
    # null = role-derived (continue for coordinator roles, fresh otherwise)
    session: null
    continue_max_age_minutes: null
    resume_id: ""
    auto_accept: true
    account: ""
    credentials_file: ""
    credentials_files: []
    provider: null
  health:
    enabled: true
    interval: 60
    timeout: 5
    method: sdk-alive
  watchdog:
    enabled: false
    interval: 1.5
    responses:
      y_n: "1"
      y_y_n: "2"
      waiting: /speak-and-call
  restart:
    policy: on-failure
    max_retries: 3
    prune_on_stop: false
    backoff:
      initial: 30
      max: 300
      multiplier: 2
  autonomous:
    enabled: false
    drive_until: DONE
    max_turns: 50
    idle_kick_after_s: 120
    kick_text: Continue. Print DONE when finished.
  hooks:
    pre_start: []
    post_start: []
    pre_stop: []
    post_stop: []
    on_compact: []
    on_restart: []
    on_diff: []
  context_management:
    trigger_at_percent: 70.0
    strategy: noop
    warn_before_n_checks: 0
    check_interval_seconds: 300
    state_file: ~/.scitex/agent-container/state/<agent>.json
  a2a:
    host: 127.0.0.1
    port: auto
  comms:
    outbound:
      siblings: allow
      parent: allow
    inbound:
      siblings: allow
      parent: allow
    a2a:
      listen: true
  lineage:
    group: ""
    may_spawn: true
"""


class ColdStartParseError(ValueError):
    """A `sac start` target looked like a cold-start form but was malformed.

    Raised (never swallowed) so the operator sees exactly what was wrong
    instead of the arg being silently misread as an agent name.
    """


class ColdStartConflictError(RuntimeError):
    """A cold-start label already exists with a DIFFERENT workdir/host.

    Fail-loud (operator directive): never silently clobber a customised spec
    nor silently launch the wrong workdir. The operator resolves it with a
    different label or ``--force``.
    """


@dataclass(frozen=True)
class ColdStartTarget:
    """A parsed cold-start request: which agent, where, in what dir."""

    label: str
    host: str
    workdir: str


def _derive_label(workdir: str) -> str:
    """Agent label = sanitized basename of the workdir. Fail loud if empty."""
    base = os.path.basename(workdir.rstrip("/"))
    if not base:
        raise ColdStartParseError(
            f"cannot derive an agent label from workdir {workdir!r} "
            "(empty basename) — pass an explicit <label>@<host>:<path>."
        )
    return base


def _validate_label(label: str) -> str:
    if not _VALID_LABEL.match(label):
        raise ColdStartParseError(
            f"invalid agent label {label!r}: must match {_VALID_LABEL.pattern} "
            "(lowercase letter first, then letters/digits/-/_). Rename the dir "
            "or pass an explicit <label>@<host>:<path>."
        )
    return label


def _split_host_path(hostpath: str, *, original: str) -> tuple[str, str]:
    """Split ``<host>:/path`` into ``(host, path)``; fail loud if malformed."""
    if ":" not in hostpath:
        raise ColdStartParseError(
            f"{original!r}: expected '<host>:/path' after '@', got {hostpath!r}."
        )
    host, path = hostpath.split(":", 1)
    if not host:
        raise ColdStartParseError(f"{original!r}: empty host before ':'.")
    if not path:
        raise ColdStartParseError(f"{original!r}: empty workdir after '<host>:'.")
    return host, path


def _looks_local_path(arg: str) -> bool:
    return arg == "." or arg.startswith(("/", "./", "~", "../"))


def parse_start_target(
    arg: str,
    *,
    caller_host: str,
    cwd: str | None = None,
) -> ColdStartTarget | None:
    """Classify a ``sac start`` positional target.

    Returns ``None`` for a plain agent name (existing registry flow), a
    :class:`ColdStartTarget` for the four cold-start forms, or raises
    :class:`ColdStartParseError` for a malformed form. ``cwd`` defaults to the
    process working directory (injectable for tests).
    """
    arg = arg.strip()
    cwd = cwd if cwd is not None else os.getcwd()

    # Forms 3 & 4 — a local path / "." (host defaults to the caller).
    if _looks_local_path(arg):
        workdir = cwd if arg == "." else os.path.abspath(os.path.expanduser(arg))
        return ColdStartTarget(
            label=_validate_label(_derive_label(workdir)),
            host=caller_host,
            workdir=workdir,
        )

    # Form 1 — <label>@<host>:/path.
    if "@" in arg:
        label, hostpath = arg.split("@", 1)
        if not label:
            raise ColdStartParseError(f"{arg!r}: empty label before '@'.")
        host, path = _split_host_path(hostpath, original=arg)
        return ColdStartTarget(
            label=_validate_label(label),
            host=host,
            workdir=path,
        )

    # Form 2 — <host>:/path (label derived from the workdir basename).
    if ":" in arg:
        host, path = _split_host_path(arg, original=arg)
        return ColdStartTarget(
            label=_validate_label(_derive_label(path)),
            host=host,
            workdir=path,
        )

    # No path / host markers → a plain agent name; existing flow resolves it.
    return None


@dataclass(frozen=True)
class ColdStartPlan:
    """Outcome of :func:`materialize_cold_start` — what was (or would be) done."""

    label: str
    spec_path: str
    host: str
    workdir: str
    action: str  # "create" | "reuse" | "would-create" | "would-reuse"


def _default_agents_root() -> Path:
    return Path.home() / ".scitex" / "agent-container" / "agents"


def _existing_matches(spec_path: Path, target: ColdStartTarget) -> bool:
    """True iff an existing spec already points at this exact workdir+host."""
    from ...config import load_config

    cfg = load_config(str(spec_path))
    existing_host = getattr(cfg, "host", "") or getattr(
        getattr(cfg, "hosts_spec", None), "host", ""
    )
    return (getattr(cfg, "workdir", "") or "") == target.workdir and (
        existing_host or ""
    ) == target.host


def materialize_cold_start(
    target: ColdStartTarget,
    *,
    base_dir: Path | None = None,
    dry_run: bool = False,
    force: bool = False,
) -> ColdStartPlan:
    """Write a minimal standardized TUI spec for ``target`` and return a plan.

    Idempotent + fail-loud:
      * fresh label → write ``<base>/<label>/{spec.yaml,to_home/}``.
      * existing label pointing at the SAME workdir+host → reuse (no write).
      * existing label, DIFFERENT workdir/host → :class:`ColdStartConflictError`
        unless ``force`` (then overwrite).
      * ``dry_run`` → never write; ``action`` reports the intended outcome.
    """
    base = base_dir if base_dir is not None else _default_agents_root()
    agent_dir = Path(base) / target.label
    spec_path = agent_dir / "spec.yaml"

    if spec_path.is_file() and not force:
        if _existing_matches(spec_path, target):
            return ColdStartPlan(
                label=target.label,
                spec_path=str(spec_path),
                host=target.host,
                workdir=target.workdir,
                action="would-reuse" if dry_run else "reuse",
            )
        raise ColdStartConflictError(
            f"agent {target.label!r} already exists at {spec_path} with a "
            f"different workdir/host than {target.workdir!r}@{target.host!r}. "
            "Use a different <label>, or pass --force to overwrite."
        )

    if dry_run:
        return ColdStartPlan(
            label=target.label,
            spec_path=str(spec_path),
            host=target.host,
            workdir=target.workdir,
            action="would-create",
        )

    agent_dir.mkdir(parents=True, exist_ok=True)
    (agent_dir / "to_home").mkdir(exist_ok=True)
    spec_path.write_text(
        _COLD_START_SPEC.format(
            label=target.label,
            host=target.host,
            workdir=target.workdir,
            stamp_note="minimal standardized template",
        )
    )
    return ColdStartPlan(
        label=target.label,
        spec_path=str(spec_path),
        host=target.host,
        workdir=target.workdir,
        action="create",
    )


def _dir_has_agents_default(p: Path) -> bool:
    """Fallback bulk-dir detector: any ``<child>/spec.yaml`` under ``p``.

    The command injects the production ``_iter_agent_yamls`` (which also
    accepts the ``<name>/<name>.yaml`` layout); this default keeps the helper
    usable + unit-testable without importing the command (no import cycle).
    """
    try:
        return any((c / "spec.yaml").is_file() for c in p.iterdir() if c.is_dir())
    except OSError:
        return False


def _is_existing_spec_target(arg: str, dir_has_agents) -> bool:
    """True when ``arg`` is an EXISTING spec/agent target — not a cold-start.

    Guards the path-shaped cold-start forms (``/path``, ``.``) from hijacking
    the existing ``sac start`` targets:

      * a ``*.yaml`` / ``*.yml`` path → an explicit spec file;
      * a directory containing ``spec.yaml`` → an agent dir;
      * a directory that ``dir_has_agents`` recognizes as a bulk agents root.

    Such targets flow through the existing resolver untouched. A plain project
    *workdir* (no agent spec inside) is left for the cold-start parser.
    """
    if arg.endswith((".yaml", ".yml")):
        return True
    p = Path(arg).expanduser()
    if not p.is_dir():
        return False
    if (p / "spec.yaml").is_file():
        return True
    if dir_has_agents(p):
        return True
    # An EMPTY directory is treated as an existing (empty) bulk target — the
    # clean "nothing to start" no-op — not a cold-start workdir. A real project
    # workdir is non-empty; cold-start needs ``.``/``<host>:``/``<label>@`` or a
    # non-empty path. (Preserves the existing empty-bulk-dir contract.)
    try:
        return not any(p.iterdir())
    except OSError:
        return False


def resolve_cold_start_targets(
    targets,
    *,
    caller_host: str,
    dry_run: bool = False,
    force: bool = False,
    base_dir: Path | None = None,
    cwd: str | None = None,
    dir_has_agents=None,
):
    """Rewrite raw ``sac start`` targets, materializing cold-start forms.

    Returns ``(rewritten_targets, plans)``: ``rewritten_targets`` is the list to
    hand to the existing launch flow (cold-start forms replaced by their agent
    label; everything else passed through), and ``plans`` is the list of
    :class:`ColdStartPlan` for the cold-started ones (for the "what's happening"
    message + ``--json``). A ``dry_run`` ``would-create`` is NOT added to the
    launch list (no spec exists yet to start). Raises
    :class:`ColdStartParseError` / :class:`ColdStartConflictError` (fail-loud).
    """
    dir_has_agents = dir_has_agents or _dir_has_agents_default
    rewritten: list[str] = []
    plans: list[ColdStartPlan] = []
    for t in targets:
        if _is_existing_spec_target(t, dir_has_agents):
            rewritten.append(t)
            continue
        cs = parse_start_target(t, caller_host=caller_host, cwd=cwd)
        if cs is None:
            rewritten.append(t)
            continue
        plan = materialize_cold_start(
            cs, base_dir=base_dir, dry_run=dry_run, force=force
        )
        plans.append(plan)
        if not (dry_run and plan.action == "would-create"):
            rewritten.append(plan.label)
    return rewritten, plans


def render_cold_start_plans(plans, *, as_json: bool, emit_json, console) -> None:
    """Print the resolved cold-start plan(s).

    Extracted from ``_start.py``'s click entry to keep it under the
    per-file line cap. ``--json`` emits one ``{"cold_start": {...}}``
    object per plan via ``emit_json``; otherwise renders a human row to
    ``console``. Behaviour is byte-identical to the inline loop.
    """
    for plan in plans:
        if as_json:
            emit_json(
                {
                    "cold_start": {
                        "label": plan.label,
                        "host": plan.host,
                        "workdir": plan.workdir,
                        "spec_path": plan.spec_path,
                        "action": plan.action,
                    }
                }
            )
        else:
            console.print(
                f"[bold]cold-start[/bold] [cyan]{plan.label}[/cyan] "
                f"[dim]({plan.action})[/dim]  host=[cyan]{plan.host}[/cyan]  "
                f"workdir=[cyan]{plan.workdir}[/cyan]\n"
                f"  spec: [dim]{plan.spec_path}[/dim]"
            )


__all__ = [
    "ColdStartConflictError",
    "ColdStartParseError",
    "ColdStartPlan",
    "ColdStartTarget",
    "materialize_cold_start",
    "parse_start_target",
    "render_cold_start_plans",
    "resolve_cold_start_targets",
]
