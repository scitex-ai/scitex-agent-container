"""Sealed run of a PLAIN COMMAND — ``sac sandbox exec`` (no harness, no model key).

    sac sandbox exec --image SIF --run-root DIR --timeout S \\
        [--egress-allow H:P ...] [--bind SRC:DST[:ro|rw] ...] \\
        [--env-allow NAME ...] [--size-mb N] [--after archive|remove] \\
        [--name N] [--pwd DIR] -- argv...

The same machinery as a sealed agent, minus the agent: :func:`._run.
prepare_run_dir` makes the fresh run dir, :func:`._argv.run_flags` +
:func:`._argv.netgw_wrap` seal the network (``--network none``; the only
way out is the allowlisting host proxy, empty by default), the supervisor
(:func:`._supervisor.supervise`) runs in the FOREGROUND with the env seal,
the proxy, the size watchdog, a deadline and a stdout/stderr tee, and
:func:`._run.finalize_run` lists the files written and applies ``--after``.

What the command sees: the image, the ``--bind`` mounts (read-only unless
``:rw``), a fresh ``$HOME`` (``/home/agent``), ``/tmp`` and a writable
rootfs layer, and the run's gateway files under ``/run/sac-sandbox``.
Nothing of SAC (no venv, no state dir, no fleet binds) is mounted. Its
environment is the ``--env-allow`` names (values from the caller's env)
plus ``LANG`` / ``TERM``; ``PATH`` comes from the image and ``HOME`` is
``/home/agent``. The caller's other variables never reach it; their NAMES
are recorded as dropped.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

from ._argv import netgw_wrap, run_flags
from ._layout import C_ROOT, RunPaths
from ._outcome import atomic_write_json, atomic_write_text, read_json
from ._spec import AFTER_MODES, SandboxSpec, parse_allow_entry, parse_run_root

__all__ = [
    "C_HOME",
    "FIXED_ENV",
    "ExecRequest",
    "ExecResult",
    "build_request",
    "caller_env",
    "container_argv",
    "parse_bind",
    "plan",
    "run_exec",
]

C_HOME = "/home/agent"
#: Names the command always gets besides ``--env-allow`` (non-secret).
FIXED_ENV = ("LANG", "TERM")
DEFAULT_SIZE_MB = 4096
_ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_RUN_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
#: Container paths a ``--bind`` may not cover (the seal's own mounts).
_RESERVED = ("/", "/proc", "/sys", "/dev", C_ROOT, C_HOME)


@dataclass(frozen=True)
class ExecRequest:
    """A validated ``sac sandbox exec`` invocation (immutable)."""

    apptainer: str
    image: str
    argv: tuple[str, ...]
    spec: SandboxSpec
    timeout_s: float
    binds: tuple[str, ...] = ()
    name: str = "exec"
    pwd: str | None = None


def _bad(msg: str) -> ValueError:
    return ValueError(f"sac sandbox exec: {msg}")


def parse_bind(text: str) -> str:
    """``SRC:DST[:ro|rw]`` -> ``"<real SRC>:DST:ro|rw"`` (read-only by default).

    SRC must exist (a bind that cannot deliver is refused, never mounted
    empty); DST must be absolute and outside the seal's own mounts.
    """
    parts = str(text).split(":")
    mode = "ro"
    if len(parts) == 3 and parts[2] in ("ro", "rw"):
        mode = parts.pop()
    if len(parts) != 2 or not all(parts):
        raise _bad(f"--bind {text!r} must be SRC:DST[:ro|rw]")
    src, dst = parts
    if "," in text:
        raise _bad(f"--bind {text!r}: paths with ',' cannot be bound")
    if not os.path.isabs(src) or not os.path.exists(src):
        raise _bad(f"--bind {text!r}: SRC must be an existing absolute path")
    dst = os.path.normpath(dst)
    if not os.path.isabs(dst):
        raise _bad(f"--bind {text!r}: DST must be absolute")
    for r in _RESERVED:
        if dst == r or (r not in ("/", C_HOME) and dst.startswith(r + "/")):
            raise _bad(f"--bind {text!r}: {dst} is reserved by the sealed run")
    return f"{os.path.realpath(src)}:{dst}:{mode}"


def build_request(
    *,
    apptainer: str | None,
    image: str,
    argv: list[str] | tuple[str, ...],
    run_root: str,
    timeout_s: float,
    egress_allow: tuple[str, ...] | list[str] = (),
    binds: tuple[str, ...] | list[str] = (),
    env_allow: tuple[str, ...] | list[str] = (),
    size_mb: int = DEFAULT_SIZE_MB,
    after: str = "archive",
    name: str = "exec",
    pwd: str | None = None,
) -> ExecRequest:
    """Validate every input; raises ``ValueError`` naming the bad one."""
    if not apptainer:
        raise _bad("apptainer is not on PATH")
    if not argv:
        raise _bad("no command (pass it after --)")
    if not Path(image).is_file():
        raise _bad(f"--image {image!r} is not a file (a SIF image)")
    if not timeout_s or float(timeout_s) <= 0:
        raise _bad("--timeout must be > 0 seconds")
    if int(size_mb) <= 0:
        raise _bad("--size-mb must be > 0")
    if after not in AFTER_MODES:
        raise _bad(f"--after {after!r} must be one of {list(AFTER_MODES)}")
    if not _RUN_NAME.match(str(name)):
        raise _bad(f"--name {name!r} must match {_RUN_NAME.pattern}")
    if pwd is not None and not os.path.isabs(pwd):
        raise _bad(f"--pwd {pwd!r} must be an absolute container path")
    for n in env_allow:
        if not _ENV_NAME.match(str(n)):
            raise _bad(f"--env-allow {n!r} is not an environment variable name")
    spec = SandboxSpec(
        egress_allow=tuple(dict.fromkeys(parse_allow_entry(e) for e in egress_allow)),
        env_allow=tuple(dict.fromkeys(env_allow)),
        model_key=None,
        run_root=parse_run_root(run_root),
        run_size_mb=int(size_mb),
        run_after=after,
    )
    return ExecRequest(
        apptainer=str(apptainer),
        image=str(Path(image).resolve()),
        argv=tuple(argv),
        spec=spec,
        timeout_s=float(timeout_s),
        binds=tuple(dict.fromkeys(parse_bind(b) for b in binds)),
        name=str(name),
        pwd=pwd,
    )


#: Characters apptainer does not carry through intact: it parses the env
#: file as shell AND evaluates each value once more inside the container
#: (measured: ``'$HOME'`` arrives expanded, ``'`` arrives as ``\\'``).
UNSAFE_VALUE_CHARS = frozenset("$`\"'\\\n\r")


def _quote(value: str) -> str:
    """One env-file value, single-quoted (contains no UNSAFE_VALUE_CHARS)."""
    return f"'{value}'"


def caller_env(
    req: ExecRequest, environ: Mapping[str, str]
) -> tuple[dict[str, str], dict]:
    """(the variables the command gets, the NAMES record for outcome.json).

    Refuses a value apptainer would rewrite (see UNSAFE_VALUE_CHARS) rather
    than hand the command something other than what the caller set.
    """
    kept = {n: environ[n] for n in req.spec.env_allow if n in environ}
    if "TERM" in environ and not UNSAFE_VALUE_CHARS & set(environ["TERM"]):
        kept.setdefault("TERM", environ["TERM"])
    unsafe = sorted(n for n, v in kept.items() if UNSAFE_VALUE_CHARS & set(v))
    if unsafe:
        raise _bad(
            f"--env-allow {unsafe}: the value holds a character apptainer would "
            "rewrite ($ ` \" ' \\ or a line break); pass it through a --bind file"
        )
    kept.setdefault("LANG", "C.UTF-8")
    return kept, {
        "requested": list(req.spec.env_allow),
        "missing": sorted(n for n in req.spec.env_allow if n not in environ),
        "fixed": sorted(n for n in FIXED_ENV if n in kept),
        "dropped": sorted(set(environ) - set(kept)),
    }


def container_argv(
    req: ExecRequest, rp: RunPaths, env_file: Path | None = None
) -> list[str]:
    """The ``apptainer exec`` argv of the run at ``rp`` (pure)."""
    argv = [
        req.apptainer, "exec", "--containall", "--cleanenv",
        "--home", C_HOME, "--bind", f"{rp.home}:{C_HOME}",
    ]  # fmt: skip
    for b in req.binds:
        argv += ["--bind", b]
    argv += run_flags(rp)
    if req.pwd:
        argv += ["--pwd", req.pwd]
    if env_file is not None:
        argv += ["--env-file", str(env_file)]
    return [*argv, req.image, *netgw_wrap(list(req.argv))]


def _command_record(req: ExecRequest) -> dict:
    return {
        "argv": list(req.argv),
        "binds": list(req.binds),
        "pwd": req.pwd or C_HOME,
        "timeout_s": req.timeout_s,
    }


def plan(req: ExecRequest) -> dict:
    """What ``run_exec`` would do, without touching the disk (``--dry-run``)."""
    rp = RunPaths(Path(req.spec.run_root) / req.name / "<run_id>")
    return {
        "run_dir": str(rp.root),
        "command": _command_record(req),
        "policy": {
            "egress_allow": req.spec.egress_allow_strings,
            "env_allow": list(req.spec.env_allow),
            "size_mb": req.spec.run_size_mb,
            "after": req.spec.run_after,
        },
        "argv": container_argv(req, rp, rp.secrets / "env-00"),
    }


@dataclass(frozen=True)
class ExecResult:
    status: int
    run_dir: Path
    outcome: dict


def run_exec(req: ExecRequest, environ: Mapping[str, str]) -> ExecResult:
    """Prepare, supervise (foreground) and finalize ONE plain-command run."""
    from ._run import finalize_run, prepare_run_dir
    from ._supervisor import SuperviseOptions, supervise

    env, env_record = caller_env(req, environ)  # refuses before any disk write
    run_dir = prepare_run_dir(
        req.spec,
        req.name,
        record={"kind": "command", "command": _command_record(req)},
    )
    rp = RunPaths(run_dir)
    env_file = rp.secrets / "env-00"
    atomic_write_text(env_file, "".join(f"{k}={_quote(v)}\n" for k, v in env.items()))
    try:
        status = supervise(
            run_dir,
            container_argv(req, rp, env_file),
            SuperviseOptions(
                image=req.image,
                egress_allow=req.spec.egress_allow,
                allow_env=frozenset(env),
                size_mb=req.spec.run_size_mb,
                scrub_roots=(rp.home.resolve(),),
                timeout_s=req.timeout_s,
                tee=True,
                stop_by="caller",
            ),
        )
    finally:  # the record is finalized (and secrets/ deleted) even on a crash
        rec = read_json(rp.outcome) or {}
        rec["env"] = {**(rec.get("env") or {}), **env_record}
        rec["output"] = {
            "stdout": str(rp.out / "stdout.log"),
            "stderr": str(rp.out / "stderr.log"),
        }
        atomic_write_json(rp.outcome, rec)
        outcome = finalize_run(rp)
    return ExecResult(status=status, run_dir=run_dir, outcome=outcome)
