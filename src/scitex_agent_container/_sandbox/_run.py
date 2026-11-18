"""Per-run lifecycle of a sealed run: ``begin_run`` (start) / ``end_run`` (stop).

``begin_run`` runs in the ``sac`` process BEFORE the workspace is
materialised, so the to_home tree, the Hermes profile and every host-side
reader of ``<state_dir>/home`` land in the FRESH run's home:

1. finalize a previous run that was never finalized (killed supervisor,
   crashed host) — its record states that instead of vanishing;
2. create ``<run.root>/<agent>/<run_id>/`` (0700) on durable disk, check
   free space, create the ``run.size``-capped sparse overlay image;
3. point ``<state_dir>/home`` and ``<state_dir>/sandbox/run`` at it;
4. write ``outcome.json`` (``status: prepared``).

``end_run`` runs after the container is gone (``sac agents stop``): it adds
the harness token usage and the files the run wrote, applies ``run.after``
(``remove`` deletes every layer but ``out/``; ``archive`` keeps them) and
marks the record ``finalized``. ``secrets/`` is always deleted.
"""

from __future__ import annotations

import os
import secrets as _secrets
import shutil
import stat
import time
from pathlib import Path
from typing import Any

from ._env_policy import validate_sandbox_config
from ._layout import RunPaths, run_link
from ._outcome import (
    SCHEMA,
    atomic_write_json,
    hermes_usage,
    manifest,
    manifest_diff,
        read_json,
    utc_now,
)

__all__ = ["SandboxRunError", "begin_run", "current_run", "end_run", "new_run_id"]


class SandboxRunError(RuntimeError):
    """A sealed run cannot be prepared; the start is refused."""


def new_run_id() -> str:
    return time.strftime("%Y%m%dT%H%M%SZ", time.gmtime()) + "-" + _secrets.token_hex(3)


def _sac_version() -> str:
    try:
        from importlib.metadata import version

        return version("scitex-agent-container")
    except Exception:  # stx-allow: fallback (metadata absent in a source checkout)
        from .. import __version__

        return str(__version__)


def current_run(state_dir: Path) -> Path | None:
    link = run_link(state_dir)
    if not link.is_symlink():
        return None
    target = Path(os.readlink(link))
    return target if target.is_dir() else None


def _home_aside(home: Path) -> None:
    """Make ``<state_dir>/home`` free for the run's symlink."""
    if home.is_symlink():
        home.unlink()
    elif home.is_dir():
        if any(home.iterdir()):
            home.rename(home.with_name(f"home.pre-sandbox-{new_run_id()}"))
        else:
            home.rmdir()
    elif home.exists():
        raise SandboxRunError(f"{home} exists and is not a directory")


def _symlink(target: Path, link: Path) -> None:
    link.parent.mkdir(parents=True, exist_ok=True)
    tmp = link.with_name(f".{link.name}.{os.getpid()}.tmp")
    tmp.unlink(missing_ok=True)
    tmp.symlink_to(target)
    os.replace(tmp, link)



def begin_run(config: Any, state_dir: Path) -> Path | None:
    """Prepare a fresh run for a sandboxed ``config``; ``None`` when not sandboxed."""
    sb = getattr(config, "sandbox", None)
    if sb is None:
        return None
    validate_sandbox_config(config)
    state_dir = Path(state_dir).expanduser()
    previous = current_run(state_dir)
    if previous is not None:
        end_run(config, state_dir)
    agent = str(getattr(config, "name", "agent"))
    root = Path(sb.run_root)
    root.mkdir(parents=True, exist_ok=True)
    free_mb = shutil.disk_usage(root).free // (1024 * 1024)
    if free_mb < sb.run_size_mb:
        raise SandboxRunError(
            f"{root} has {free_mb} MB free; spec.sandbox.run.size needs {sb.run_size_mb} MB"
        )
    run_id = new_run_id()
    run_dir = root / agent / run_id
    rp = RunPaths(run_dir)
    run_dir.mkdir(parents=True, mode=0o700)
    for d in (rp.home, rp.overlay, rp.workdir, rp.uvwork, rp.bin, rp.sock, rp.secrets, rp.out):
        d.mkdir(parents=True, mode=0o700, exist_ok=True)
    src = Path(__file__).with_name("netgw.py")
    shutil.copyfile(src, rp.bin / "netgw.py")
    os.chmod(rp.bin / "netgw.py", 0o444)
    _home_aside(state_dir / "home")
    _symlink(rp.home, state_dir / "home")
    _symlink(run_dir, run_link(state_dir))
    atomic_write_json(
        rp.outcome,
        {
            "schema": SCHEMA,
            "status": "prepared",
            "agent": agent,
            "run_id": run_id,
            "run_dir": str(run_dir),
            "prepared_at": utc_now(),
            "sac_version": _sac_version(),
            "policy": {
                "egress_allow": sb.egress_allow_strings,
                "env_allow": list(sb.env_allow),
                "model_key": sb.model_key,
                "overlay_size_mb": sb.run_size_mb,
                "after": sb.run_after,
            },
        },
    )
    return run_dir


def _force_writable(func, path, _exc) -> None:
    """rmtree retry for overlay ``work/`` dirs the kernel leaves mode 000."""
    parent = os.path.dirname(path)
    os.chmod(parent, os.stat(parent).st_mode | stat.S_IRWXU)
    if os.path.isdir(path) and not os.path.islink(path):
        os.chmod(path, stat.S_IRWXU)
    func(path)


def _remove_layers(rp: RunPaths) -> list[str]:
    removed = []
    for p in (rp.home, rp.workdir, rp.uvwork, rp.root / "rt", rp.overlay):
        if p.is_dir() and not p.is_symlink():
            shutil.rmtree(p, onexc=_force_writable)
        elif p.exists() or p.is_symlink():
            p.unlink(missing_ok=True)
        removed.append(str(p))
    return removed


def end_run(config: Any, state_dir: Path) -> dict | None:
    """Finalize the current run (idempotent); returns the final record."""
    state_dir = Path(state_dir).expanduser()
    run_dir = current_run(state_dir)
    if run_dir is None:
        return None
    rp = RunPaths(run_dir)
    rec = read_json(rp.outcome) or {"schema": SCHEMA, "run_dir": str(run_dir)}
    if rec.get("status") == "finalized":
        return rec
    status = rec.get("status")
    if status == "prepared":
        rec["exit"] = {
            "code": None,
            "reason": "never launched (start did not reach the container)",
        }
    elif status == "running":
        rec["ended_at"] = rec.get("ended_at") or utc_now()
        rec["exit"] = {
            "code": None,
            "reason": "supervisor ended before the container exit was observed "
            "(killed with its pane); see egress logs and harness logs",
        }
    before = read_json(rp.out / "home_manifest_start.json") or {}
    rec["files"] = {
        "read": None,
        "read_reason": "not recorded: no syscall trace is taken by SAC",
        "home_written": manifest_diff(before, manifest(rp.home)),
        "overlay_written": manifest_diff({}, manifest(rp.overlay / "upper")),
    }
    rec["token_usage"] = hermes_usage(rp.home / ".hermes" / "state.db") or {
        "available": False,
        "reason": "the harness left no usage record SAC can read",
    }
    shutil.rmtree(rp.secrets, ignore_errors=True)
    after = (rec.get("policy") or {}).get("after", "remove")
    if after == "remove":
        rec["layers"] = {"after": "remove", "removed": _remove_layers(rp)}
    else:
        rec["layers"] = {"after": "archive", "kept": str(run_dir)}
    home = state_dir / "home"
    if home.is_symlink() and Path(os.readlink(home)) == rp.home:
        home.unlink()
    rec["finalized_at"] = utc_now()
    rec["status"] = "finalized"
    atomic_write_json(rp.outcome, rec)
    return rec
