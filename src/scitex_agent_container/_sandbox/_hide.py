"""Run one command of a sealed agent with SAC's control plane hidden from it.

    python -m scitex_agent_container._sandbox._hide \\
        --hide /state/<name> --hide <SAC prefix> ... -- CMD...

Inside a sealed run SAC's in-container owner (``_hermes_tui_owner``) needs
the per-agent state dir (gateway projection, the gateway API key, logs) and
the launching SAC installation; the agent it starts needs neither. The owner
therefore starts each agent process (the Hermes gateway, the TUI and so every
tool child) through this launcher, which:

1. enters a NEW user + mount + PID namespace (nested inside the egress
   gateway's own user namespace; no privilege outside it);
2. in the namespace's init: makes every mount private, covers each existing
   ``--hide`` directory with an empty read-only ``mode=000`` tmpfs and each
   existing ``--hide`` file with ``/dev/null``, also covering the
   ``scitex_agent_container`` package of every hidden SAC console script's
   environment, and mounts a fresh ``/proc`` that shows only this namespace's
   processes (the owner, ``tini`` and the gateway stay invisible, so their
   ``/proc/<pid>/root``, ``environ`` and fds are unreachable);
3. REFUSES to start CMD (exit :data:`EXIT_HIDE_FAILED`) when any step fails
   or a ``sac`` CLI is still executable on CMD's ``PATH``;
4. drops every capability, and starts CMD as the unprivileged mapped user:
   the covering mounts were made in a namespace CMD holds no capability in,
   and a further namespace CMD creates inherits them LOCKED, so CMD can
   neither unmount nor see beneath them.

The launcher (outside the new PID namespace) and the namespace init forward
SIGTERM / SIGINT / SIGHUP to CMD and exit with CMD's status (128+N for a
signal). Both are non-dumpable so CMD cannot read their environment.

Stdlib only; linux only.
"""

from __future__ import annotations

import argparse
import ctypes
import glob
import os
import shutil
import signal
import sys
from pathlib import Path

__all__ = [
    "EXIT_HIDE_FAILED",
    "HIDE_MODULE",
    "SAC_CONSOLE_SCRIPTS",
    "covered_paths",
    "hide_argv",
    "main",
]

HIDE_MODULE = "scitex_agent_container._sandbox._hide"
#: Exit status when the control plane could not be hidden (CMD never ran).
EXIT_HIDE_FAILED = 125
#: Console scripts that drive SAC; none may remain runnable under the mask.
SAC_CONSOLE_SCRIPTS = ("sac", "scitex-agent-container")
PACKAGE = "scitex_agent_container"

CLONE_NEWNS, CLONE_NEWUSER, CLONE_NEWPID = 0x00020000, 0x10000000, 0x20000000
MS_RDONLY, MS_NOSUID, MS_NODEV, MS_NOEXEC = 0x1, 0x2, 0x4, 0x8
MS_BIND, MS_REC, MS_PRIVATE = 0x1000, 0x4000, 0x40000
PR_SET_PDEATHSIG, PR_SET_DUMPABLE, PR_SET_NO_NEW_PRIVS = 1, 4, 38
PR_CAP_AMBIENT, PR_CAP_AMBIENT_CLEAR_ALL = 47, 4
LINUX_CAPABILITY_VERSION_3 = 0x20080522
FORWARDED = (signal.SIGTERM, signal.SIGINT, signal.SIGHUP)


class HideError(RuntimeError):
    """A control-plane path could not be hidden; CMD must not start."""


def hide_argv(python: str, hidden: list[str], argv: list[str]) -> list[str]:
    """``argv`` started through this launcher with ``hidden`` covered."""
    out = [str(python), "-m", HIDE_MODULE]
    for path in hidden:
        out += ["--hide", str(path)]
    return [*out, "--", *argv]


def covered_paths(hidden: list[str], pythonpath: str = "") -> list[Path]:
    """The existing paths to cover: each ``hidden`` one, plus SAC's package.

    A hidden SAC console script (``<env>/bin/sac``) also hides the
    ``scitex_agent_container`` package of that environment, and a
    ``pythonpath`` entry carrying the package has it hidden too, so neither
    ``sac`` nor ``python -m scitex_agent_container`` runs. Paths absent in
    this container are skipped (nothing to hide); symlinks are resolved, so
    the real file or directory is covered; paths under an already covered
    directory are dropped (it is empty once covered).
    """
    found: list[Path] = []
    for raw in hidden:
        path = Path(raw)
        if not path.is_absolute():
            raise HideError(f"hidden path must be absolute: {raw!r}")
        if path.exists():
            found.append(path.resolve())
        if path.name in SAC_CONSOLE_SCRIPTS and path.parent.name == "bin":
            env = path.parent.parent
            for site in ("site-packages", "dist-packages"):
                pattern = str(env / "lib" / "python*" / site / PACKAGE)
                found += [Path(p).resolve() for p in sorted(glob.glob(pattern))]
    for entry in pythonpath.split(os.pathsep):
        if entry and os.path.isabs(entry) and (Path(entry) / PACKAGE).is_dir():
            found.append((Path(entry) / PACKAGE).resolve())
    unique = sorted(dict.fromkeys(found), key=lambda p: len(p.parts))
    kept: list[Path] = []
    for path in unique:
        if not any(path == k or k in path.parents for k in kept if k.is_dir()):
            kept.append(path)
    return kept


# ------------------------------------------------------------ syscalls --
_libc = ctypes.CDLL(None, use_errno=True)


def _check(rc: int, what: str) -> None:
    if rc != 0:
        err = ctypes.get_errno()
        raise HideError(f"{what}: {os.strerror(err)}")


def _prctl(*args: int) -> None:
    _check(_libc.prctl(*[ctypes.c_ulong(a) for a in args]), f"prctl{args}")


def _mount(
    src: str | None, target: str, fstype: str | None, flags: int, data: str | None
) -> None:
    enc = lambda s: None if s is None else s.encode()  # noqa: E731
    rc = _libc.mount(
        enc(src), enc(target), enc(fstype), ctypes.c_ulong(flags), enc(data)
    )
    _check(rc, f"mount {fstype or src} on {target}")


def _drop_all_caps() -> None:
    class Header(ctypes.Structure):
        _fields_ = [("version", ctypes.c_uint32), ("pid", ctypes.c_int)]

    class Data(ctypes.Structure):
        _fields_ = [
            (n, ctypes.c_uint32) for n in ("effective", "permitted", "inheritable")
        ]

    data = (Data * 2)()
    _check(
        _libc.capset(ctypes.byref(Header(LINUX_CAPABILITY_VERSION_3, 0)), data),
        "capset",
    )
    _prctl(PR_CAP_AMBIENT, PR_CAP_AMBIENT_CLEAR_ALL, 0, 0, 0)


def _write(path: str, text: str) -> None:
    with open(path, "w") as fh:
        fh.write(text)


def _enter_namespaces() -> None:
    uid, gid = os.geteuid(), os.getegid()
    _check(_libc.unshare(CLONE_NEWUSER | CLONE_NEWNS | CLONE_NEWPID), "unshare")
    _write("/proc/self/setgroups", "deny")
    _write("/proc/self/uid_map", f"{uid} {uid} 1\n")
    _write("/proc/self/gid_map", f"{gid} {gid} 1\n")


def _cover(paths: list[Path]) -> None:
    _mount(None, "/", None, MS_REC | MS_PRIVATE, None)
    for path in paths:
        if path.is_dir():
            _mount(
                "sac-hidden",
                str(path),
                "tmpfs",
                MS_RDONLY | MS_NOSUID | MS_NODEV | MS_NOEXEC,
                "size=4k,mode=000",
            )
        else:
            _mount("/dev/null", str(path), None, MS_BIND, None)
    _mount("proc", "/proc", "proc", MS_NOSUID | MS_NODEV | MS_NOEXEC, None)


def _refuse_visible_sac(env_path: str) -> None:
    for name in SAC_CONSOLE_SCRIPTS:
        located = shutil.which(name, path=env_path)
        if located:
            raise HideError(f"{name} is still executable at {located}")


def _status(wait_status: int) -> int:
    if os.WIFSIGNALED(wait_status):
        return 128 + os.WTERMSIG(wait_status)
    return os.WEXITSTATUS(wait_status)


def _supervise(child: int, *, reap_all: bool) -> int:
    """Forward stop signals to ``child``; return its status once it exits."""

    def forward(signum, _frame):
        try:
            os.kill(child, signum)
        except ProcessLookupError:
            pass

    for sig in FORWARDED:
        signal.signal(sig, forward)
    while True:
        pid, wait_status = os.waitpid(-1 if reap_all else child, 0)
        if pid == child:
            return _status(wait_status)


def _namespace_init(paths: list[Path], cmd: list[str]) -> int:
    """PID 1 of the new namespace: cover, verify, drop privilege, run CMD."""
    try:
        _prctl(PR_SET_PDEATHSIG, signal.SIGKILL, 0, 0, 0)
        _cover(paths)
        _refuse_visible_sac(os.environ.get("PATH", os.defpath))
        _drop_all_caps()
        _prctl(PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0)
    except (HideError, OSError) as exc:
        sys.stderr.write(f"[sac-hide] refusing to start the agent: {exc}\n")
        return EXIT_HIDE_FAILED
    child = os.fork()
    if child == 0:
        for sig in FORWARDED:
            signal.signal(sig, signal.SIG_DFL)
        try:
            os.execvp(cmd[0], cmd)
        except OSError as exc:
            sys.stderr.write(f"[sac-hide] cannot execute {cmd[0]}: {exc}\n")
            os._exit(127)
    return _supervise(child, reap_all=True)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="sac-hide")
    ap.add_argument("--hide", action="append", default=[])
    ap.add_argument("cmd", nargs=argparse.REMAINDER)
    a = ap.parse_args(argv)
    cmd = a.cmd[1:] if a.cmd[:1] == ["--"] else a.cmd
    if not cmd:
        ap.error("no command")
    try:
        paths = covered_paths(a.hide, os.environ.get("PYTHONPATH", ""))
        _enter_namespaces()
        # Non-dumpable (inherited by the namespace init) before CMD exists:
        # both carry the caller's environment, which CMD must not read via
        # /proc. Only after the id maps: it makes /proc/self root-owned.
        _prctl(PR_SET_DUMPABLE, 0, 0, 0, 0)
    except (HideError, OSError) as exc:
        sys.stderr.write(f"[sac-hide] refusing to start the agent: {exc}\n")
        return EXIT_HIDE_FAILED
    child = os.fork()
    if child == 0:
        os._exit(_namespace_init(paths, cmd))
    return _supervise(child, reap_all=False)


if __name__ == "__main__":
    sys.exit(main())
