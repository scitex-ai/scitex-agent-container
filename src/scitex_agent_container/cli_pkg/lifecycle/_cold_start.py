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

# Agent-name charset (mirrors cli_pkg._new validation): lowercase letters,
# digits, hyphen, underscore; must start with a letter.
_VALID_LABEL = re.compile(r"^[a-z][a-z0-9_-]*$")

# Minimal standardized TUI spec for a cold-started agent. Deliberately small
# (operator: keep agent defs pure) — the sac MCP server + ``server:sac``
# channel are auto-injected by the loader, so only what's UNIQUE to this agent
# is written here. ``host`` is always set (dispatch runs locally when it
# matches the caller's host, remote otherwise).
_COLD_START_SPEC = """\
# {label} — cold-started by `sac start` ({stamp_note}).
# Minimal standardized TUI spec; edit freely or `sac agents new` for the full tour.
apiVersion: scitex-agent-container/v3
kind: Agent
metadata:
  labels:
    project: {label}
spec:
  runtime: tui
  host: {host}
  workdir: {workdir}
  claude:
    flags:
      - --dangerously-skip-permissions
  a2a:
    port: auto
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


__all__ = [
    "ColdStartConflictError",
    "ColdStartParseError",
    "ColdStartPlan",
    "ColdStartTarget",
    "materialize_cold_start",
    "parse_start_target",
]
