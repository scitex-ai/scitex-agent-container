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
import sys
import time
from pathlib import Path
from typing import Any

from ._env_policy import validate_sandbox_config
from ._layout import RunPaths, run_link
from ._outcome import (
    SCHEMA,
    atomic_write_json,
    hermes_transcript,
    hermes_usage,
    manifest,
    manifest_diff,
    read_json,
    utc_now,
)

__all__ = [
    "SandboxRunError",
    "begin_run",
    "current_run",
    "end_run",
    "finalize_run",
    "mark_stop_requested",
    "new_run_id",
    "prepare_run_dir",
    "stop_attribution",
]


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


def prepare_run_dir(sb: Any, name: str, *, record: dict | None = None) -> Path:
    """Create ONE fresh run dir ``<run.root>/<name>/<run_id>/`` and its record.

    The run's layers (``$HOME``, overlay, workdir, ``/uvwork``), the
    read-only launchers, ``out/`` and ``outcome.json`` (``status:
    prepared``). Touches nothing outside the run dir: the agent launch adds
    its state-dir links (:func:`begin_run`); ``sac sandbox exec`` uses the
    run dir directly. ``record`` adds keys to the prepared record.
    """
    root = Path(sb.run_root)
    root.mkdir(parents=True, exist_ok=True)
    free_mb = shutil.disk_usage(root).free // (1 << 20)
    if free_mb < sb.run_size_mb:
        raise SandboxRunError(
            f"{root} has {free_mb} MB free; the run size cap needs {sb.run_size_mb} MB"
        )
    run_id = new_run_id()
    run_dir = root / name / run_id
    rp = RunPaths(run_dir)
    run_dir.mkdir(parents=True, mode=0o700)
    for d in (
        rp.home,
        rp.overlay,
        rp.workdir,
        rp.uvwork,
        rp.bin,
        rp.sock,
        rp.secrets,
        rp.out,
    ):
        d.mkdir(parents=True, mode=0o700, exist_ok=True)
    # Stdlib-only launchers the image's ``python3 -I`` runs BY PATH: the
    # egress gateway and the control-plane mask (``_mask.masked_argv``).
    for src, dst in (("netgw.py", "netgw.py"), ("_hide.py", "hide.py")):
        shutil.copyfile(Path(__file__).with_name(src), rp.bin / dst)
        os.chmod(rp.bin / dst, 0o444)
    atomic_write_json(
        rp.outcome,
        {
            "schema": SCHEMA,
            "status": "prepared",
            "agent": name,
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
            **(record or {}),
        },
    )
    return run_dir


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
    run_dir = prepare_run_dir(sb, str(getattr(config, "name", "agent")))
    _home_aside(state_dir / "home")
    _symlink(RunPaths(run_dir).home, state_dir / "home")
    _symlink(run_dir, run_link(state_dir))
    return run_dir


def _force_writable(func, path, _exc) -> None:
    """rmtree retry for overlay ``work/`` dirs the kernel leaves mode 000."""
    parent = os.path.dirname(path)
    os.chmod(parent, os.stat(parent).st_mode | stat.S_IRWXU)
    if os.path.isdir(path) and not os.path.islink(path):
        os.chmod(path, stat.S_IRWXU)
    func(path)


def _rmtree_forcing(path: Path) -> None:
    """``rmtree`` with the chmod retry; ``onexc`` exists only on Python >= 3.12."""
    if sys.version_info >= (3, 12):
        shutil.rmtree(path, onexc=_force_writable)
    else:
        shutil.rmtree(path, onerror=_force_writable)


def _remove_layers(rp: RunPaths) -> list[str]:
    removed = []
    for p in (rp.home, rp.workdir, rp.uvwork, rp.root / "rt", rp.overlay):
        if p.is_dir() and not p.is_symlink():
            _rmtree_forcing(p)
        elif p.exists() or p.is_symlink():
            p.unlink(missing_ok=True)
        removed.append(str(p))
    return removed


def mark_stop_requested(state_dir: Path, *, by: str = "sac agents stop") -> bool:
    """Record that SAC asked this run to stop, BEFORE it tears the pane down.

    The teardown signals every process group of the pane; whichever exits
    first decides what the supervisor observes (a harness that handles
    SIGTERM may exit 1 before the supervisor's own signal arrives). This
    marker makes the outcome say the run was stopped by SAC either way.
    """
    run_dir = current_run(Path(state_dir).expanduser())
    if run_dir is None:
        return False
    rp = RunPaths(run_dir)
    rec = read_json(rp.outcome) or {}
    if rec.get("status") == "finalized":
        return False
    atomic_write_json(rp.stop_requested, {"t": utc_now(), "by": by})
    return True


def stop_attribution(rp: RunPaths) -> dict | None:
    """``{"stopped_by": "sac", ...}`` when SAC requested this run's stop."""
    marker = read_json(rp.stop_requested)
    if not marker:
        return None
    return {
        "stopped_by": "sac",
        "stop_requested_at": marker.get("t"),
        "stop_requested_by": marker.get("by"),
    }


def _apply_after_policy(rec: dict, rp: RunPaths) -> None:
    """``run.after``; only once the container is gone."""
    after = (rec.get("policy") or {}).get("after", "remove")
    if after == "remove":
        rec["layers"] = {"after": "remove", "removed": _remove_layers(rp)}
    else:
        rec["layers"] = {"after": "archive", "kept": str(rp.root)}


def _unlink_home(rp: RunPaths, state_dir: Path) -> None:
    home = state_dir / "home"
    if home.is_symlink() and Path(os.readlink(home)) == rp.home:
        home.unlink()


def end_run(
    config: Any, state_dir: Path, *, teardown_verified: bool = True
) -> dict | None:
    """Finalize the current run of an agent (idempotent); the final record.

    ``teardown_verified=False`` (the pane/cgroup teardown check failed or
    was too slow) still FINALIZES the record — exit, usage, files — but
    keeps the run's layers and marks them ``deferred``: removing a layer a
    still-running container uses would be wrong. A later call with a
    verified teardown (the next stop, or the next start's begin_run)
    completes the deferred ``run.after``.
    """
    state_dir = Path(state_dir).expanduser()
    run_dir = current_run(state_dir)
    if run_dir is None:
        return None
    rp = RunPaths(run_dir)
    rec = finalize_run(rp, teardown_verified=teardown_verified)
    if teardown_verified:
        _unlink_home(rp, state_dir)
    return rec


def finalize_run(rp: RunPaths, *, teardown_verified: bool = True) -> dict:
    """Finalize the run at ``rp`` (idempotent): files, usage, ``run.after``.

    A plain-command record (``kind: command``, ``sac sandbox exec``) has no
    harness, so it carries no ``tool_calls`` / ``token_usage``.
    """
    run_dir = rp.root
    rec = read_json(rp.outcome) or {"schema": SCHEMA, "run_dir": str(run_dir)}
    if rec.get("status") == "finalized":
        if teardown_verified and (rec.get("layers") or {}).get("deferred"):
            _apply_after_policy(rec, rp)
            rec["layers"]["completed_at"] = utc_now()
            atomic_write_json(rp.outcome, rec)
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
    harness = rec.get("kind") != "command"
    transcript = (
        hermes_transcript(rp.home / ".hermes" / "state.db", rp.out / ".transcript-tmp")
        if harness
        else None
    )
    if harness:
        rec["tool_calls"] = transcript or {
            "available": False,
            "reason": "the harness left no transcript SAC can read",
        }
    attribution = stop_attribution(rp)
    if attribution and isinstance(rec.get("exit"), dict):
        rec["exit"].update(attribution)
    rec["files"] = {
        "home_written": manifest_diff(before, manifest(rp.home)),
        "overlay_written": manifest_diff({}, manifest(rp.overlay / "upper")),
    }
    if harness:
        rec["files"]["read"] = (transcript or {}).get("files_read")
        rec["files"]["read_source"] = (transcript or {}).get(
            "source", "unavailable: no harness transcript (SAC takes no syscall trace)"
        )
        rec["token_usage"] = hermes_usage(rp.home / ".hermes" / "state.db") or {
            "available": False,
            "reason": "the harness left no usage record SAC can read",
        }
    shutil.rmtree(rp.secrets, ignore_errors=True)
    if teardown_verified:
        _apply_after_policy(rec, rp)
    else:
        rec["layers"] = {
            "after": (rec.get("policy") or {}).get("after", "remove"),
            "deferred": "pane teardown could not be verified; layers kept until "
            "a later stop or start verifies the container is gone",
        }
    rec["finalized_at"] = utc_now()
    rec["status"] = "finalized"
    atomic_write_json(rp.outcome, rec)
    return rec
