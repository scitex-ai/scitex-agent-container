"""Host-side supervisor of ONE sealed run (the tmux pane's process).

    python -I -m scitex_agent_container._sandbox supervise \\
        --state-dir S --agent N --image SIF [--egress-allow H:P ...] \\
        [--allow-env NAME ...] -- apptainer exec ...

Before the container starts it resolves the prepared run (``begin_run``),
rewrites the stable run-link paths in the argv to the real run dir, SEALS
the environment (every ``--env`` / ``--env-file`` entry not in the allow set
is dropped; kept env-file entries are re-written into 0600 files under
``secrets/``, deleted again as soon as the gateway reports the container is
up), records the image's sha256 and a manifest of the fresh ``$HOME``, and
starts :class:`._proxy.EgressProxy`. apptainer itself is started with a
MINIMAL process environment — never ``os.environ``.

When the container exits it writes the exit status (``exit_code`` file and
``outcome.json``, both atomically) and the egress summary, and exits with
the container's status. Teardown that may be slow (file listings, removal)
is ``end_run``'s, in ``sac agents stop``.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import pwd
import re
import signal
import subprocess
import sys
import threading
from pathlib import Path

from ._layout import RunPaths, run_link
from ._outcome import atomic_write_json, atomic_write_text, manifest, read_json, utc_now
from ._proxy import EgressProxy

__all__ = [
    "SizeWatchdog",
    "main",
    "minimal_process_env",
    "rewrite_run_paths",
    "run_dir_bytes",
    "seal_env_flags",
]

#: Exit status when the run cannot even be set up (no container started).
EXIT_SETUP = 96
#: How often the run.size watchdog measures the run dir, and the grace
#: between its SIGTERM and SIGKILL.
SIZE_POLL_S = 5.0
SIZE_KILL_GRACE_S = 10.0


def run_dir_bytes(run_dir: Path, exclude: tuple[str, ...] = ("out", "secrets")) -> int:
    """Allocated bytes under ``run_dir`` (overlay, $HOME, /tmp, /uvwork, ...)."""
    total = 0
    for entry in os.scandir(run_dir):
        if entry.name in exclude:
            continue
        if not entry.is_dir(follow_symlinks=False):
            total += entry.stat(follow_symlinks=False).st_blocks * 512
            continue
        for dirpath, _dirs, files in os.walk(entry.path):
            for fn in files:
                try:
                    total += os.lstat(os.path.join(dirpath, fn)).st_blocks * 512
                except OSError:
                    continue
    return total


class SizeWatchdog(threading.Thread):
    """Enforces ``run.size`` over the WHOLE writable run dir.

    A FUSE ext3 overlay image would cap only the rootfs layer and is slow
    on loaded hosts; measuring the run dir caps $HOME, /tmp and /uvwork too.
    On overflow the container is terminated and the reason recorded.
    """

    def __init__(
        self, run_dir: Path, limit_bytes: int, child, *, poll_s: float = SIZE_POLL_S
    ):
        super().__init__(name="sac-size-watchdog", daemon=True)
        self.run_dir, self.limit, self.child, self.poll_s = (
            run_dir,
            limit_bytes,
            child,
            poll_s,
        )
        self.exceeded_at_bytes: int | None = None
        self.peak = 0
        self.stop_evt = threading.Event()

    def run(self) -> None:
        while not self.stop_evt.wait(self.poll_s):
            used = run_dir_bytes(self.run_dir)
            self.peak = max(self.peak, used)
            if used > self.limit:
                self.exceeded_at_bytes = used
                self.child.terminate()
                if (
                    self.stop_evt.wait(SIZE_KILL_GRACE_S) is False
                    and self.child.poll() is None
                ):
                    self.child.kill()
                return


def minimal_process_env(parent: dict | None = None) -> dict[str, str]:
    """The ONLY environment apptainer is started with (no host/fleet vars)."""
    parent = parent or {}
    pw = pwd.getpwuid(os.getuid())
    env = {
        "PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
        "HOME": pw.pw_dir,
        "USER": pw.pw_name,
        "LOGNAME": pw.pw_name,
        "LANG": "C.UTF-8",
    }
    if parent.get("TERM"):
        env["TERM"] = parent["TERM"]
    return env


def rewrite_run_paths(argv: list[str], mapping: dict[str, str]) -> list[str]:
    """Replace each path prefix in ``mapping`` (at a path boundary) in every arg."""
    pats = [
        (re.compile(re.escape(old) + r"(?=$|[/:])"), new)
        for old, new in sorted(mapping.items(), key=lambda kv: -len(kv[0]))
    ]
    out = []
    for a in argv:
        for pat, new in pats:
            a = pat.sub(new, a)
        out.append(a)
    return out


def _read_env_file(path: Path) -> list[tuple[str, str]]:
    pairs = []
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return pairs
    for line in text.splitlines():
        s = line.strip()
        if not s or s.startswith("#") or "=" not in s:
            continue
        if s.startswith("export "):
            s = s[len("export ") :].lstrip()
        k, _, v = s.partition("=")
        pairs.append((k.strip(), v))
    return pairs


def _under(path: Path, roots: tuple[Path, ...]) -> bool:
    try:
        real = path.resolve()
    except OSError:
        return False
    return any(real == r or r in real.parents for r in roots)


def seal_env_flags(
    argv: list[str],
    allowed: frozenset[str],
    secrets_dir: Path,
    *,
    scrub_roots: tuple[Path, ...] = (),
) -> tuple[list[str], dict]:
    """Drop disallowed env; re-write kept env-file entries into ``secrets_dir``.

    An env file that lives under one of ``scrub_roots`` (dirs the container
    can READ: the bound state dir, the run's $HOME) is also rewritten IN
    PLACE with the allowed entries only, or removed when none remain: the
    seal must cover the file, not just the variables apptainer exports
    (SAC's 0600 secret env file sits in the state dir bound at
    /state/<name>; $HOME/.env carries pool tokens and .envrc values).
    """
    out: list[str] = []
    kept: set[str] = set()
    dropped: set[str] = set()
    n_files = 0
    i = 0
    while i < len(argv):
        a = argv[i]
        value, width = None, 1
        if a in ("--env", "-e", "--env-file") and i + 1 < len(argv):
            value, width = argv[i + 1], 2
            flag = "--env-file" if a == "--env-file" else "--env"
        elif a.startswith("--env="):
            value, flag = a[len("--env=") :], "--env"
        elif a.startswith("--env-file="):
            value, flag = a[len("--env-file=") :], "--env-file"
        if value is None:
            out.append(a)
            i += 1
            continue
        if flag == "--env":
            key = value.split("=", 1)[0]
            if key in allowed:
                kept.add(key)
                out += ["--env", value]
            else:
                dropped.add(key)
        else:
            src = Path(value)
            pairs = _read_env_file(src)
            good = [(k, v) for k, v in pairs if k in allowed]
            dropped.update(k for k, _ in pairs if k not in allowed)
            if src.is_file() and _under(src, scrub_roots):
                if good:
                    atomic_write_text(src, "".join(f"{k}={v}\n" for k, v in good))
                else:
                    src.unlink(missing_ok=True)
            if good:
                n_files += 1
                dest = secrets_dir / f"env-{n_files:02d}"
                atomic_write_text(dest, "".join(f"{k}={v}\n" for k, v in good))
                kept.update(k for k, _ in good)
                out += ["--env-file", str(dest)]
        i += width
    return out, {"passed": sorted(kept), "dropped": sorted(dropped - kept)}


def _sha256(path: Path) -> str | None:
    h = hashlib.sha256()
    try:
        with open(path, "rb") as fh:
            for chunk in iter(lambda: fh.read(8 << 20), b""):
                h.update(chunk)
    except OSError:
        return None
    return h.hexdigest()


def _fail_setup(msg: str) -> int:
    sys.stderr.write(f"[sac-sandbox] {msg}\n")
    return EXIT_SETUP


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="sac-sandbox supervise")
    ap.add_argument("--state-dir", required=True)
    ap.add_argument("--agent", required=True)
    ap.add_argument("--image", required=True)
    ap.add_argument("--egress-allow", action="append", default=[])
    ap.add_argument("--allow-env", action="append", default=[])
    ap.add_argument("--size-mb", type=int, default=0)
    ap.add_argument("cmd", nargs=argparse.REMAINDER)
    a = ap.parse_args(argv)
    cmd = a.cmd[1:] if a.cmd[:1] == ["--"] else a.cmd
    if not cmd:
        return _fail_setup("no container command")
    state_dir = Path(a.state_dir)
    link = run_link(state_dir)
    if not link.is_symlink() or not link.resolve().is_dir():
        return _fail_setup(
            f"no prepared run at {link} (start through `sac agents start`)"
        )
    run_dir = link.resolve()
    rp = RunPaths(run_dir)
    rec = read_json(rp.outcome)
    if rec is None or rec.get("status") != "prepared":
        return _fail_setup(
            f"run {run_dir} is not in state 'prepared' (got {rec and rec.get('status')})"
        )

    cmd = rewrite_run_paths(
        cmd, {str(link): str(run_dir), str(state_dir / "home"): str(rp.home)}
    )
    cmd, env_names = seal_env_flags(
        cmd,
        frozenset(a.allow_env),
        rp.secrets,
        scrub_roots=(state_dir.resolve(), rp.home.resolve()),
    )
    allow = []
    for entry in a.egress_allow:
        host, _, port = entry.rpartition(":")
        allow.append((host, int(port)))
    atomic_write_json(rp.out / "home_manifest_start.json", manifest(rp.home))
    rec.update(
        {
            "status": "running",
            "started_at": utc_now(),
            "image": {
                "path": str(Path(a.image).resolve()),
                "sha256": _sha256(Path(a.image)),
            },
            "env": env_names,
            "supervisor_pid": os.getpid(),
        }
    )
    proxy = EgressProxy(rp.proxy_sock, rp.out, allow, ingress_path=rp.ingress_sock)
    try:
        proxy.start()
    except (OSError, RuntimeError, ValueError) as exc:
        rec.update(
            {
                "status": "exited",
                "ended_at": utc_now(),
                "exit": {"code": None, "reason": f"egress proxy failed: {exc}"},
            }
        )
        atomic_write_json(rp.outcome, rec)
        return _fail_setup(f"egress proxy failed: {exc}")
    rec["egress"] = {"mode": "allowlist", "logs": proxy.summary()["logs"]}
    atomic_write_json(rp.outcome, rec)

    # The kept env-file values are consumed by apptainer at container start;
    # drop them as soon as the gateway is up (and at the latest on exit).
    def _shred_when_up():
        while not stop_evt.wait(0.5):
            if proxy.gateway_events.get("gateway_up"):
                break
        for p in rp.secrets.glob("env-*"):
            p.unlink(missing_ok=True)

    stop_evt = threading.Event()
    threading.Thread(
        target=_shred_when_up, name="sac-secret-shred", daemon=True
    ).start()

    child = subprocess.Popen(cmd, env=minimal_process_env(dict(os.environ)))
    watchdog = None
    if a.size_mb > 0:
        watchdog = SizeWatchdog(run_dir, a.size_mb * 1024 * 1024, child)
        watchdog.start()

    def _forward(signum, _frame):
        # Record the termination FIRST: a pane teardown follows SIGTERM with
        # SIGKILL within seconds, often before apptainer's runtime parent has
        # finished unmounting, so the post-wait write below may never run.
        if rec.get("status") == "running":
            rec.update(
                {
                    "status": "exited",
                    "ended_at": utc_now(),
                    "exit": {
                        "code": None,
                        "signal": signum,
                        "reason": f"terminated by {signal.Signals(signum).name} "
                        "(e.g. `sac agents stop`); forwarded to the container",
                    },
                    "egress": proxy.summary(),
                }
            )
            if watchdog is not None:
                rec["size"] = {"limit_mb": a.size_mb, "peak_bytes": watchdog.peak}
            atomic_write_text(rp.exit_code, f"{128 + signum}\n", mode=0o644)
            atomic_write_json(rp.outcome, rec)
        try:
            child.send_signal(signum)
        except ProcessLookupError:
            pass

    for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        signal.signal(sig, _forward)
    rc = child.wait()
    stop_evt.set()
    summary = proxy.stop()
    for p in rp.secrets.glob("env-*"):
        p.unlink(missing_ok=True)
    exit_doc = {"code": rc} if rc >= 0 else {"code": None, "signal": -rc}
    if watchdog is not None:
        watchdog.stop_evt.set()
        rec["size"] = {"limit_mb": a.size_mb, "peak_bytes": watchdog.peak}
        if watchdog.exceeded_at_bytes is not None:
            exit_doc["reason"] = (
                f"run.size exceeded: {watchdog.exceeded_at_bytes} bytes > {a.size_mb} MB; "
                "container terminated by SAC"
            )
    rec.update(
        {"status": "exited", "ended_at": utc_now(), "exit": exit_doc, "egress": summary}
    )
    atomic_write_text(rp.exit_code, f"{rc if rc >= 0 else 128 - rc}\n", mode=0o644)
    atomic_write_json(rp.outcome, rec)
    return rc if rc >= 0 else 128 - rc
