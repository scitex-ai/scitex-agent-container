"""Host-side fail-fast: refuse specs whose ``$HOME`` is operator-writable.

The agent's canonical ``$HOME`` (``/home/agent``) is overlay/tmpfs-backed and
per-agent, which is precisely what stops Claude Code + shell writes
(``~/.claude/.credentials.json``, session transcripts, ``~/.bash_history``,
``~/.config``, caches) from CLOBBERING the operator's REAL dotfiles. A
``relaxed: true`` spec can move ``$HOME`` via ``--home`` in ``raw_args`` onto a
real host directory that is ALSO bind-mounted read-write — then the agent's
home writes overwrite the operator's files. The in-container preflight
(:mod:`._apptainer_preflight`) is bypassed by relaxed specs and runs too late.
This guard runs on the host BEFORE the container spawns and rejects that class
of spec.
"""

from __future__ import annotations

from pathlib import PurePosixPath

from ..config import AgentConfig
from ._to_home_overlay import resolve_container_home

__all__ = ["assert_home_not_operator_writable"]


def _iter_operator_bind_specs(config: AgentConfig) -> list[str]:
    """Bind specs the OPERATOR declared: ``apptainer.binds`` + ``raw_args``
    ``--bind``/``-B`` values. sac's own default binds are added later in
    build_argv and are intentionally NOT considered here.
    """
    ap = getattr(config, "apptainer", None)
    if ap is None:
        return []
    specs: list[str] = list(getattr(ap, "binds", None) or [])
    raw = list(getattr(ap, "raw_args", None) or [])
    i = 0
    while i < len(raw):
        arg = str(raw[i])
        value: str | None = None
        if arg in ("--bind", "-B"):
            if i + 1 < len(raw):
                value = str(raw[i + 1])
                i += 1
        elif arg.startswith("--bind="):
            value = arg[len("--bind=") :]
        elif arg.startswith("-B="):
            value = arg[len("-B=") :]
        if value:
            # A single --bind value may carry several comma-separated specs.
            specs.extend(part for part in value.split(",") if part)
        i += 1
    return specs


def _parse_bind(spec: str) -> tuple[str, str]:
    """Return ``(dst, mode)`` for a ``src[:dst[:mode]]`` bind. A missing mode
    is ``rw`` (apptainer's default); a bare ``src`` binds ``src:src`` rw.
    """
    parts = spec.split(":")
    if len(parts) == 1:
        return parts[0], "rw"
    dst = parts[1]
    mode = parts[2] if len(parts) >= 3 and parts[2] else "rw"
    return dst, mode


def assert_home_not_operator_writable(config: AgentConfig) -> None:
    """Raise if the resolved container ``$HOME`` equals or is a subpath of any
    operator-declared read-write bind destination — the dotfile-clobber vector.
    """
    home = PurePosixPath(resolve_container_home(config))
    for spec in _iter_operator_bind_specs(config):
        dst, mode = _parse_bind(spec)
        if mode.startswith("ro"):
            continue
        dst_p = PurePosixPath(dst)
        if home == dst_p or dst_p in home.parents:
            raise RuntimeError(
                f"ERROR[sac-home-guard]: agent $HOME={home} is served by "
                f"operator read-write bind '{spec}' — writes to the agent home "
                f"would CLOBBER real host files at {dst}. Use the canonical "
                f"overlay-backed /home/agent, or bind that path read-only (:ro)."
            )
