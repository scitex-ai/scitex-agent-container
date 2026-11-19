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

:func:`main` is the agent pane's entry (resolves the run through the state
dir's run link); :func:`supervise` is the run itself, also called in the
foreground by ``sac sandbox exec`` (:mod:`._exec`) with a deadline, a
stdout/stderr tee and caller-stop attribution (:class:`SuperviseOptions`).
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
from dataclasses import dataclass
from pathlib import Path

from ._layout import RunPaths, run_link
from ._outcome import atomic_write_json, atomic_write_text, manifest, read_json, utc_now
from ._proxy import EgressProxy
from ._watch import Deadline, SizeWatchdog, run_dir_bytes, start_tee

__all__ = [
    "SizeWatchdog",
    "SuperviseOptions",
    "main",
    "minimal_process_env",
    "rewrite_run_paths",
    "run_dir_bytes",
    "seal_env_flags",
    "supervise",
]

#: Exit status when the run cannot even be set up (no container started).
EXIT_SETUP = 96
#: Exit status of a run its ``timeout`` ended (as coreutils ``timeout``).
EXIT_TIMEOUT = 124


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


@dataclass(frozen=True)
class SuperviseOptions:
    """How :func:`supervise` runs one prepared run.

    ``stop_by`` names who a stop signal came from when it is not ``sac
    agents stop`` (the plain-command run: ``"caller"``); ``timeout_s`` > 0
    ends the run after that many seconds; ``tee`` copies the container's
    stdout/stderr to the caller and to ``out/stdout.log`` / ``out/stderr.log``.
    """

    image: str
    egress_allow: tuple[tuple[str, int], ...] = ()
    allow_env: frozenset[str] = frozenset()
    size_mb: int = 0
    scrub_roots: tuple[Path, ...] = ()
    timeout_s: float = 0.0
    tee: bool = False
    stop_by: str | None = None


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
    cmd = rewrite_run_paths(
        cmd, {str(link): str(run_dir), str(state_dir / "home"): str(rp.home)}
    )
    allow = []
    for entry in a.egress_allow:
        host, _, port = entry.rpartition(":")
        allow.append((host, int(port)))
    opts = SuperviseOptions(
        image=a.image,
        egress_allow=tuple(allow),
        allow_env=frozenset(a.allow_env),
        size_mb=a.size_mb,
        scrub_roots=(state_dir.resolve(), rp.home.resolve()),
    )
    return supervise(run_dir, cmd, opts)


def _stop_reason(signum: int, stop_by: str | None) -> str:
    name = signal.Signals(signum).name
    if stop_by:
        return f"terminated by {name} from the {stop_by}; forwarded to the container"
    return f"terminated by {name} (e.g. `sac agents stop`); forwarded to the container"


def supervise(run_dir: Path, cmd: list[str], opts: SuperviseOptions) -> int:
    """Run ``cmd`` (``apptainer exec ...``) as the prepared run at ``run_dir``.

    Returns the container's exit status (``EXIT_TIMEOUT`` when the deadline
    ended it, ``EXIT_SETUP`` when the run could not start).
    """
    rp = RunPaths(Path(run_dir))
    rec = read_json(rp.outcome)
    if rec is None or rec.get("status") != "prepared":
        return _fail_setup(
            f"run {run_dir} is not in state 'prepared' (got {rec and rec.get('status')})"
        )
    cmd, env_names = seal_env_flags(
        cmd, opts.allow_env, rp.secrets, scrub_roots=opts.scrub_roots
    )
    atomic_write_json(rp.out / "home_manifest_start.json", manifest(rp.home))
    rec.update(
        {
            "status": "running",
            "started_at": utc_now(),
            "image": {
                "path": str(Path(opts.image).resolve()),
                "sha256": _sha256(Path(opts.image)),
            },
            "env": env_names,
            "supervisor_pid": os.getpid(),
        }
    )
    proxy = EgressProxy(
        rp.proxy_sock, rp.out, list(opts.egress_allow), ingress_path=rp.ingress_sock
    )
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
    # The child and watchdog exist only once the container starts; the signal
    # handler is installed BEFORE the record says "running", so a stop that
    # arrives in between is still recorded (and the container never starts).
    live: dict = {"child": None, "watchdog": None}

    def _forward(signum, _frame):
        live.setdefault("signal", signum)
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
                        "reason": _stop_reason(signum, opts.stop_by),
                    },
                    "egress": proxy.summary(),
                }
            )
            from ._run import stop_attribution

            rec["exit"].update(stop_attribution(rp) or {})
            if opts.stop_by:
                rec["exit"]["stopped_by"] = opts.stop_by
            if live["watchdog"] is not None:
                rec["size"] = {
                    "limit_mb": opts.size_mb,
                    "peak_bytes": live["watchdog"].peak,
                }
            atomic_write_text(rp.exit_code, f"{128 + signum}\n", mode=0o644)
            atomic_write_json(rp.outcome, rec)
        if live["child"] is not None:
            try:
                live["child"].send_signal(signum)
            except ProcessLookupError:
                pass
        else:
            # No container yet (the handler can run inside Popen): deliver
            # this stop to the child as soon as it exists, below.
            live["pending"] = signum

    for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        signal.signal(sig, _forward)
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

    # Block the stop signals across check-and-launch so a stop aimed at THIS
    # thread waits for live["child"]. That alone is not enough: the mask is
    # per-thread, and the proxy / shred threads above can take a
    # process-directed SIGTERM, after which CPython runs the handler in this
    # thread at its next bytecode -- possibly inside Popen, before the child
    # exists. Such a stop is parked in live["pending"] and forwarded below.
    stop_signals = {signal.SIGTERM, signal.SIGINT, signal.SIGHUP}
    signal.pthread_sigmask(signal.SIG_BLOCK, stop_signals)
    if rec.get("status") != "running":  # stopped before the container started
        signal.pthread_sigmask(signal.SIG_UNBLOCK, stop_signals)
        stop_evt.set()
        proxy.stop()
        return 128 + int((rec.get("exit") or {}).get("signal") or 0)
    pipe = subprocess.PIPE if opts.tee else None
    child = subprocess.Popen(
        cmd,
        env=minimal_process_env(dict(os.environ)),
        stdout=pipe,
        stderr=pipe,
        # the child must not inherit the blocked mask
        preexec_fn=lambda: signal.pthread_sigmask(signal.SIG_UNBLOCK, stop_signals),
    )
    live["child"] = child
    pending = live.pop("pending", None)
    if pending is not None:  # a stop arrived while the container was starting
        try:
            child.send_signal(pending)
        except ProcessLookupError:
            pass
    signal.pthread_sigmask(signal.SIG_UNBLOCK, stop_signals)
    tees = []
    if opts.tee:
        tees = [
            start_tee(child.stdout, sys.stdout.buffer, rp.out / "stdout.log"),
            start_tee(child.stderr, sys.stderr.buffer, rp.out / "stderr.log"),
        ]
    watchdog = deadline = None
    if opts.size_mb > 0:
        watchdog = SizeWatchdog(run_dir, opts.size_mb * 1024 * 1024, child)
        live["watchdog"] = watchdog
        watchdog.start()
    if opts.timeout_s > 0:
        deadline = Deadline(opts.timeout_s, child)
        deadline.start()
    rc = child.wait()
    for t in tees:
        t.join(timeout=10)
    stop_evt.set()
    summary = proxy.stop()
    for p in rp.secrets.glob("env-*"):
        p.unlink(missing_ok=True)
    exit_doc = {"code": rc} if rc >= 0 else {"code": None, "signal": -rc}
    from ._run import stop_attribution

    attribution = stop_attribution(rp)
    if attribution:
        exit_doc.update(attribution)
        exit_doc.setdefault(
            "reason",
            "stopped by SAC (sac agents stop); the container's own exit status is code/signal",
        )
    if opts.stop_by and live.get("signal"):
        exit_doc.update(
            {
                "stopped_by": opts.stop_by,
                "stop_signal": live["signal"],
                "reason": _stop_reason(live["signal"], opts.stop_by),
            }
        )
    if watchdog is not None:
        watchdog.stop_evt.set()
        rec["size"] = {"limit_mb": opts.size_mb, "peak_bytes": watchdog.peak}
        if watchdog.exceeded_at_bytes is not None:
            exit_doc["reason"] = (
                f"run.size exceeded: {watchdog.exceeded_at_bytes} bytes > "
                f"{opts.size_mb} MB; container terminated by SAC"
            )
    status = rc if rc >= 0 else 128 - rc
    if deadline is not None:
        deadline.stop_evt.set()
        if deadline.fired:
            exit_doc.update(
                {
                    "timed_out": True,
                    "timeout_s": opts.timeout_s,
                    "reason": f"timeout: still running after {opts.timeout_s:g} s; "
                    "container terminated by SAC (code/signal are its own status)",
                }
            )
            status = EXIT_TIMEOUT
    rec.update(
        {"status": "exited", "ended_at": utc_now(), "exit": exit_doc, "egress": summary}
    )
    atomic_write_text(rp.exit_code, f"{status}\n", mode=0o644)
    atomic_write_json(rp.outcome, rec)
    return status
