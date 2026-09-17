"""Write an inline v3 Agent spec to the canonical install root.

Used by ``POST /agents`` when the request body carries a
``spec`` dict instead of (or alongside) a bare ``name``. Lets external
orchestrators register-and-start agents in one HTTP call without
staging YAML on the sac host out-of-band.
"""

from __future__ import annotations

import fcntl
import os
import shutil
import stat
import subprocess
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from starlette.responses import JSONResponse


def _twin_parent_from_spec(spec: dict) -> str:
    body = spec.get("spec")
    if not isinstance(body, dict):
        return ""
    apptainer = body.get("apptainer")
    if not isinstance(apptainer, dict):
        return ""
    env = apptainer.get("env")
    if not isinstance(env, dict):
        return ""
    from .._lifecycle._twin import TWIN_PARENT_ENV

    return str(env.get(TWIN_PARENT_ENV) or "").strip()


@dataclass
class InlineSpecHandoff:
    """Ownership ledger for the fork transaction and detached handoff."""

    worktree_parent: Path | None = None
    worktree_path: Path | None = None
    worktree_created: bool = False
    seed_path: Path | None = None
    seed_created: bool = False
    authority_parent_spec: Path | None = None
    authority_snapshot_path: Path | None = None
    authority_snapshot_created: bool = False
    spec_path: Path | None = None
    spec_created: bool = False
    spec_previous: bytes | None = None
    runtime_path: Path | None = None
    runtime_created: bool = False
    overlay_path: Path | None = None
    overlay_created: bool = False
    lock_fd: int | None = None

    def release_lock(self) -> None:
        if self.lock_fd is not None:
            os.close(self.lock_fd)
            self.lock_fd = None

    def rollback(self) -> list[str]:
        """Remove only owned artifacts, verify removals, and report failures."""
        failures: list[str] = []

        def remove_tree(path: Path, label: str) -> None:
            try:
                if os.path.lexists(path):
                    if path.is_symlink():
                        path.unlink()
                    else:
                        shutil.rmtree(path)
                if os.path.lexists(path):
                    failures.append(f"{label} still exists: {path}")
            except OSError as exc:
                failures.append(f"could not remove {label} {path}: {exc}")

        try:
            if self.runtime_created and self.runtime_path is not None:
                remove_tree(self.runtime_path, "runtime")
            elif self.seed_created and self.seed_path is not None:
                try:
                    self.seed_path.unlink(missing_ok=True)
                    if os.path.lexists(self.seed_path):
                        failures.append(f"seed still exists: {self.seed_path}")
                except OSError as exc:
                    failures.append(f"could not remove seed {self.seed_path}: {exc}")
            if self.overlay_created and self.overlay_path is not None:
                remove_tree(self.overlay_path, "overlay")
            if self.spec_path is not None:
                try:
                    if self.spec_created:
                        self.spec_path.unlink(missing_ok=True)
                        if os.path.lexists(self.spec_path):
                            failures.append(f"spec still exists: {self.spec_path}")
                        try:
                            self.spec_path.parent.rmdir()
                        except OSError:
                            pass
                    elif self.spec_previous is not None:
                        self.spec_path.write_bytes(self.spec_previous)
                except OSError as exc:
                    failures.append(f"could not restore spec {self.spec_path}: {exc}")
            if (
                self.authority_snapshot_created
                and self.authority_snapshot_path is not None
            ):
                remove_tree(self.authority_snapshot_path, "authority snapshot")
            if (
                self.worktree_created
                and self.worktree_parent is not None
                and self.worktree_path is not None
            ):
                removed = subprocess.run(
                    [
                        "git",
                        "-C",
                        str(self.worktree_parent),
                        "worktree",
                        "remove",
                        "--force",
                        str(self.worktree_path),
                    ],
                    check=False,
                    capture_output=True,
                    text=True,
                )
                if removed.returncode != 0 or os.path.lexists(self.worktree_path):
                    failures.append(
                        f"could not remove worktree {self.worktree_path}: "
                        f"{(removed.stderr or removed.stdout).strip()}"
                    )
                subprocess.run(
                    ["git", "-C", str(self.worktree_parent), "worktree", "prune"],
                    check=False,
                    capture_output=True,
                    text=True,
                )
        finally:
            self.release_lock()
        return failures

    def commit(self) -> None:
        """Keep all artifacts and release the child-name transaction lock."""
        self.release_lock()


def _acquire_child_lock(
    agents_root: Path, name: str, handoff: InlineSpecHandoff
) -> JSONResponse | None:
    """Take a non-blocking cross-process lock for one child identity."""
    lock_root = agents_root / ".fork-locks"
    lock_root.mkdir(mode=0o700, parents=True, exist_ok=True)
    lock_path = lock_root / f"{name}.lock"
    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
    fd: int | None = None
    try:
        fd = os.open(lock_path, flags, 0o600)
        info = os.fstat(fd)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.geteuid()
            or stat.S_IMODE(info.st_mode) != 0o600
        ):
            raise PermissionError("fork lock is not an owner-controlled 0600 file")
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        if fd is not None:
            os.close(fd)
        return JSONResponse(
            {
                "error": f"fork transaction for {name!r} is already in progress",
                "kind": "already_exists",
            },
            status_code=409,
        )
    except OSError as exc:
        if fd is not None:
            os.close(fd)
        return JSONResponse(
            {
                "error": f"cannot acquire fork transaction lock: {exc}",
                "kind": "twin_lock_failed",
            },
            status_code=500,
        )
    assert fd is not None
    handoff.lock_fd = fd
    return None


def _git_authority(repo: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(repo), *args],
        check=False,
        capture_output=True,
        text=True,
    )
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout).strip()
        raise OSError(f"git {' '.join(args)} failed: {detail}")
    return completed.stdout.strip()


def _write_hermes_fork_authority(
    *,
    name: str,
    spec: dict,
    parent_spec_path: Path,
    primary: Path,
    handoff: InlineSpecHandoff,
) -> Path:
    """Commit a fork spec in a new immutable authority snapshot and link it."""
    import yaml

    parent_spec = Path(parent_spec_path).resolve(strict=True)
    parent_repo = Path(
        _git_authority(parent_spec.parent, "rev-parse", "--show-toplevel")
    ).resolve()
    parent_head = _git_authority(parent_repo, "rev-parse", "--verify", "HEAD")
    origin = _git_authority(parent_repo, "remote", "get-url", "origin")
    source = origin.rstrip("/").rsplit("/", 1)[-1].rsplit(":", 1)[-1]
    if source.endswith(".git"):
        source = source[:-4]
    # A snapshot origin may itself carry the pinned suffix; strip it so the
    # next immutable snapshot keeps the original source identity.
    import re

    source = re.sub(r"-[0-9a-f]{40}$", "", source).lstrip(".")
    if not re.fullmatch(r"[A-Za-z0-9._-]+", source):
        raise OSError(f"invalid authority source identity {source!r}")
    snapshot_parent = (
        parent_repo.parent
        if parent_repo.parent.name == "sac-authority"
        else Path.home() / ".scitex" / "agent-container" / "sac-authority"
    )
    snapshot_parent.mkdir(parents=True, exist_ok=True)
    stage = snapshot_parent / f".{source}-fork-{name}-{uuid.uuid4().hex}"
    try:
        subprocess.run(
            ["git", "clone", "--quiet", "--shared", str(parent_repo), str(stage)],
            check=True,
            capture_output=True,
            text=True,
        )
        _git_authority(stage, "remote", "set-url", "origin", origin)
        _git_authority(stage, "checkout", "--quiet", "--detach", parent_head)
        parent_rel = parent_spec.relative_to(parent_repo)
        child_rel = parent_rel.parent.parent / name / "spec.yaml"
        child_path = stage / child_rel
        child_path.parent.mkdir(parents=True, exist_ok=True)
        child_path.write_text(yaml.safe_dump(spec, sort_keys=False), encoding="utf-8")
        _git_authority(stage, "add", "--", child_rel.as_posix())
        _git_authority(
            stage,
            "-c",
            "user.name=SAC Fork Authority",
            "-c",
            "user.email=sac-fork@invalid",
            "commit",
            "--quiet",
            "-m",
            f"sac fork spec: {name}",
        )
        child_head = _git_authority(stage, "rev-parse", "--verify", "HEAD")
        snapshot = snapshot_parent / f"{source}-{child_head}"
        if os.path.lexists(snapshot):
            raise OSError(f"fork authority snapshot already exists: {snapshot}")
        os.replace(stage, snapshot)
        handoff.authority_snapshot_path = snapshot
        handoff.authority_snapshot_created = True
        authoritative_spec = snapshot / child_rel
        primary.mkdir(parents=True, exist_ok=True)
        spec_path = primary / "spec.yaml"
        handoff.spec_path = spec_path
        handoff.spec_created = True
        spec_path.symlink_to(authoritative_spec)
        return spec_path
    except Exception:
        shutil.rmtree(stage, ignore_errors=True)
        raise


def _inject_twin_workdir_bind(
    spec: dict, host_worktree: Path, container_workdir: str
) -> dict:
    """Replace any exact workdir bind with the detached child worktree."""
    import copy

    from ._inline_spec_bind_translate import _parse_bind

    out = copy.deepcopy(spec)
    body = out["spec"]
    apptainer = body["apptainer"]
    binds = apptainer.get("binds")
    binds = binds if isinstance(binds, list) else []
    retained: list[Any] = []
    for bind in binds:
        parsed = _parse_bind(bind)
        if parsed is not None and parsed[1] == container_workdir:
            continue
        retained.append(bind)
    apptainer["binds"] = [
        f"{host_worktree}:{container_workdir}:rw",
        *retained,
    ]
    return out


def _prepare_twin_host_isolation(
    name: str,
    spec: dict,
    *,
    authoritative_parent: tuple[str, Path, dict[str, Any]] | None,
    handoff: InlineSpecHandoff,
) -> tuple[dict, JSONResponse | None]:
    """Create host isolation from one already-authenticated parent snapshot."""
    parent_name = _twin_parent_from_spec(spec)
    if not parent_name:
        return spec, None
    if authoritative_parent is None:
        return spec, JSONResponse(
            {
                "error": (
                    "fork execution specs are server-derived and require an "
                    "authenticated host-owner fork request"
                ),
                "kind": "twin_authority_required",
            },
            status_code=403,
        )

    parent_identity, parent_path, parent_doc = authoritative_parent
    if parent_name != parent_identity:
        return spec, JSONResponse(
            {
                "error": "derived fork lineage does not match its parent",
                "kind": "twin_parent_mismatch",
            },
            status_code=400,
        )

    from .._lifecycle._twin import (
        TwinSeedError,
        _ensure_twin_worktree,
        _reject_symlink_components,
        _resolve_host_repo_from_binds,
        _twin_isolation_paths,
        _validate_agent_component,
    )

    try:
        _validate_agent_component(parent_name, role="parent")
    except TwinSeedError as exc:
        return spec, JSONResponse(
            {"error": str(exc), "kind": "invalid_agent_name"}, status_code=400
        )
    if name == parent_name:
        return spec, JSONResponse(
            {
                "error": "fork agent name must differ from its parent",
                "kind": "twin_identity_mismatch",
            },
            status_code=400,
        )

    parent_spec = parent_doc.get("spec") if isinstance(parent_doc, dict) else None
    if not isinstance(parent_spec, dict):
        return spec, JSONResponse(
            {
                "error": "authoritative parent spec has no mapping spec block",
                "kind": "twin_parent_unavailable",
            },
            status_code=400,
        )
    from ..config._harness_lookup import canonical_harness
    from ..config._harness_types import resolve_spec_harness

    parent_family = canonical_harness(resolve_spec_harness(parent_spec))
    parent_apptainer = parent_spec.get("apptainer")
    parent_apptainer = parent_apptainer if isinstance(parent_apptainer, dict) else {}
    container_workdir = str(parent_spec.get("workdir") or "")
    parent_binds = parent_apptainer.get("binds")
    parent_binds = parent_binds if isinstance(parent_binds, list) else []
    try:
        host_parent = _resolve_host_repo_from_binds(container_workdir, parent_binds)
        expected_worktree, expected_overlay = _twin_isolation_paths(
            str(host_parent),
            str(parent_apptainer.get("overlay") or ""),
            name,
        )
        host_worktree = Path(expected_worktree)
        overlay_path = Path(expected_overlay)
        _reject_symlink_components(overlay_path, label="fork overlay")
        if os.path.lexists(overlay_path):
            raise TwinSeedError(
                f"fork overlay must be fresh; refusing preexisting path: {overlay_path}"
            )
        parent_overlay = Path(str(parent_apptainer.get("overlay") or ""))
        if parent_overlay and os.path.realpath(overlay_path) == os.path.realpath(
            parent_overlay
        ):
            raise TwinSeedError("fork overlay aliases the parent overlay")
        if host_parent.exists() and host_worktree.exists():
            parent_stat = host_parent.stat()
            child_stat = host_worktree.stat()
            if (parent_stat.st_dev, parent_stat.st_ino) == (
                child_stat.st_dev,
                child_stat.st_ino,
            ):
                raise TwinSeedError("fork worktree aliases the parent worktree")
        handoff.overlay_path = overlay_path
        handoff.overlay_created = True
    except TwinSeedError as exc:
        return spec, JSONResponse(
            {"error": str(exc), "kind": "twin_parent_unavailable"},
            status_code=400,
        )

    child_spec = spec.get("spec") or {}
    child_apptainer = child_spec.get("apptainer") or {}
    if (
        str(child_spec.get("workdir") or "") != container_workdir
        or str(child_apptainer.get("overlay") or "") != expected_overlay
    ):
        return spec, JSONResponse(
            {
                "error": "fork workdir/overlay do not match canonical isolation paths",
                "kind": "twin_isolation_mismatch",
            },
            status_code=400,
        )
    try:
        import copy

        from ..config._validation import validate_raw

        errors = validate_raw(copy.deepcopy(spec), f"<inline-twin:{name}>")
        if errors:
            return spec, JSONResponse(
                {
                    "error": "derived fork spec failed v3 validation",
                    "kind": "spec_invalid",
                    "details": {"validation": errors[:5]},
                },
                status_code=400,
            )
        created = _ensure_twin_worktree(str(host_parent), str(host_worktree))
    except TwinSeedError as exc:
        return spec, JSONResponse(
            {"error": str(exc), "kind": "twin_isolation_failed"},
            status_code=400,
        )
    handoff.worktree_parent = host_parent
    handoff.worktree_path = host_worktree
    handoff.worktree_created = created
    if parent_family == "hermes":
        handoff.authority_parent_spec = parent_path
        from .._lifecycle._twin import _materialize_hermes_fork_seed
        from ..runtimes._hermes_tui_rpc import HermesTuiRpcError

        try:
            from .._lifecycle._twin import HERMES_FORK_SEED_FILE
            from .._runners._session_state import state_dir_for

            expected_seed = state_dir_for(name) / HERMES_FORK_SEED_FILE
            seed_existed = os.path.lexists(expected_seed)
            handoff.seed_path = _materialize_hermes_fork_seed(
                parent_name=parent_name,
                child_name=name,
                parent_spec=parent_spec,
                child_spec=child_spec,
            )
            handoff.seed_created = not seed_existed
        except (TwinSeedError, HermesTuiRpcError, OSError) as exc:
            return spec, JSONResponse(
                {"error": str(exc), "kind": "twin_context_failed"},
                status_code=400,
            )
    return _inject_twin_workdir_bind(spec, host_worktree, container_workdir), None


def _resolve_parent_binds(caller: str) -> list[str] | None:
    """Return the parent agent's persisted ``apptainer.binds`` or ``None``.

    Injected into :func:`translate_binds_in_spec` so the translate
    module stays decoupled from the on-host config-resolution chain.
    A return of ``None`` (caller unknown, spec unreadable, config
    invalid) lands as ``skipped_reason="caller_unknown"`` and the
    spec is forwarded to PR-1 unchanged.

    The resolution goes through ``resolve_config`` → ``load_config``
    (the same path :func:`agent_status` uses), so any spec the host
    can introspect via ``GET /agents/<name>/status`` is the same
    spec PR-2 reads here. Imports are local so a unit test that
    patches ``resolve_config`` only needs to wire the lookup, not
    the heavy parser chain.
    """
    # stx-allow: fallback (reason: any failure in the resolve / load /
    # parse chain must collapse to no-op so PR-1 stays the SoT; the
    # translate module itself also catches but we centralize the
    # "lookup is allowed to fail silently" rule HERE so the callable
    # passed to the translate module is contract-clean)
    try:
        from ..config import load_config
        from ..config._resolve import resolve_config

        spec_path = resolve_config(caller)
        cfg = load_config(spec_path)
    except Exception:  # stx-allow: fallback (reason: see inline comment)
        return None
    apt = getattr(cfg, "apptainer", None)
    if apt is None:
        return None
    binds = getattr(apt, "binds", None)
    if not isinstance(binds, list):
        return None
    return [b for b in binds if isinstance(b, str)]


def materialize_inline_spec(
    name: str,
    spec: object,
    *,
    overwrite: bool,
    caller: str | None = None,
    fork_params: dict[str, Any] | None = None,
    owner_authorized: bool = False,
    handoff: InlineSpecHandoff | None = None,
) -> JSONResponse | None:
    """Write ``spec`` to ``~/.scitex/agent-container/agents/<name>/spec.yaml``.

    Returns ``None`` on success, or a ``JSONResponse`` carrying the
    failure (so the handler can ``return`` it verbatim). Validation
    pipeline (ordered cheap-to-expensive):

      1. ``spec`` is a dict + v3 apiVersion + Agent kind (basic shape).
      2. ``kind="spec_invalid"`` for any of (1) — wire-stable.
      3. **PR-2 bind translate (opt-in convenience)**. When ``caller``
         is a known SAC-managed agent, the parent's host-side bind
         map is used to rewrite any of the child spec's bind sources
         that name an in-SIF prefix (``/work/...``) the parent's
         container view exposes. Read-only, best-effort: any failure
         to resolve the parent collapses to no-op and PR-1 catches
         whatever leaked through.
      4. **bind preflight**: every ``spec.apptainer.binds[*]`` host
         source is ``stat()``-checked. Any missing source aborts with
         HTTP 400 + ``kind="bind_unresolvable"`` (PR-1 fail-loud).
         This is the SoT for "is this bind safe?" — PR-2 just
         pre-cleans the common SAC-from-SAC case.
      5. **startup_commands lint**: every
         ``spec.startup_commands[*].command`` first token is checked
         via :func:`shlex.split` + :func:`shutil.which` against the
         SAC host PATH. Misses, colon-suffixed prompt-text barewords
         (the ``"You:"`` smoking gun from the clew launcher #70
         incident on 2026-06-03), and shell-syntax errors abort
         with HTTP 400 + ``kind="spec_invalid"`` carrying a per-entry
         ``reason`` sub-shade enum. Mirrors PR-1's wire shape so the
         caller can branch on ``kind`` + per-entry ``reason``.
      6. ``kind="already_exists"`` for the overwrite-guard collision or an
         authority-managed symlink. ``overwrite=True`` may replace a regular,
         host-owned spec, but never writes through a symlink installed by
         ``sac agents link-specs``.
      7. ``kind="spec_invalid"`` for write failure (disk full, RO fs).

    Args:
        name: target agent name.
        spec: the inline v3 Agent spec dict from the POST body.
        overwrite: 409 if a regular, host-owned spec already exists at the
            target path unless this is ``True``. Authority-managed symlinks
            are never overwritten; update their source and redeploy instead.
        caller: PR-2 — the spawning node's name. ``None`` (or an
            unknown caller) disables bind-translate and the spec is
            forwarded to the preflight unchanged. The same caller
            field drives the WI-2 spawn gate one level up in the
            request handler.
    """
    import yaml

    from .._lifecycle._twin import TwinSeedError, _validate_agent_component

    try:
        _validate_agent_component(name, role="target")
    except TwinSeedError as exc:
        return JSONResponse(
            {"error": str(exc), "kind": "invalid_agent_name"}, status_code=400
        )
    agents_dir = (
        Path(os.path.expanduser("~")) / ".scitex" / "agent-container" / "agents"
    )
    primary = agents_dir / name
    try:
        primary.relative_to(agents_dir)
    except ValueError:
        return JSONResponse(
            {
                "error": f"target escapes agents root: {name!r}",
                "kind": "invalid_agent_name",
            },
            status_code=400,
        )
    spec_path = primary / "spec.yaml"
    active_handoff = handoff if handoff is not None else InlineSpecHandoff()
    authoritative_parent: tuple[str, Path, dict[str, Any]] | None = None

    if fork_params is not None:
        if not owner_authorized:
            kind = (
                "twin_agent_auth_unavailable" if caller else "twin_authority_required"
            )
            return JSONResponse(
                {
                    "error": (
                        "fork creation requires the separate authenticated "
                        "host-owner principal; the shared container bearer is insufficient"
                    ),
                    "kind": kind,
                },
                status_code=403,
            )
        if caller:
            return JSONResponse(
                {
                    "error": "agent-authenticated remote forks are not cryptographically bound",
                    "kind": "twin_agent_auth_unavailable",
                },
                status_code=403,
            )
        if spec is not None:
            return JSONResponse(
                {
                    "error": "fork requests may contain parameters only, not an execution spec",
                    "kind": "twin_spec_forbidden",
                },
                status_code=400,
            )
        allowed = {"parent", "task", "persist", "role"}
        unknown = sorted(set(fork_params) - allowed)
        parent_name = fork_params.get("parent")
        if unknown or not isinstance(parent_name, str) or not parent_name:
            return JSONResponse(
                {
                    "error": "invalid fork parameters",
                    "kind": "twin_parameters_invalid",
                    "details": {"unknown": unknown},
                },
                status_code=400,
            )
        if type(fork_params.get("persist", False)) is not bool:
            return JSONResponse(
                {
                    "error": "fork persist must be boolean",
                    "kind": "twin_parameters_invalid",
                },
                status_code=400,
            )
        for optional in ("task", "role"):
            value = fork_params.get(optional)
            if value is not None and not isinstance(value, str):
                return JSONResponse(
                    {
                        "error": f"fork {optional} must be a string",
                        "kind": "twin_parameters_invalid",
                    },
                    status_code=400,
                )

        lock_error = _acquire_child_lock(agents_dir, name, active_handoff)
        if lock_error is not None:
            return lock_error
        from .._runners._session_state import state_dir_for

        runtime_path = state_dir_for(name)
        if os.path.lexists(runtime_path):
            active_handoff.release_lock()
            return JSONResponse(
                {
                    "error": f"fork runtime state already exists: {runtime_path}",
                    "kind": "already_exists",
                },
                status_code=409,
            )
        active_handoff.runtime_path = runtime_path
        active_handoff.runtime_created = True
        if os.path.lexists(primary):
            active_handoff.release_lock()
            return JSONResponse(
                {
                    "error": f"fork agent path already exists: {primary}",
                    "kind": "already_exists",
                },
                status_code=409,
            )

        # One authoritative read under the per-child transaction lock.  The
        # client supplies no image, harness, provider/model, session, bind,
        # startup, to_home, Cards or lineage execution fields.
        from .._lifecycle._twin import _resolve_parent_to_home, derive_twin_spec
        from ..config import resolve_config

        try:
            parent_path = Path(resolve_config(parent_name))
            parent_doc = yaml.safe_load(parent_path.read_text(encoding="utf-8"))
            if not isinstance(parent_doc, dict):
                raise ValueError("parent YAML is not a mapping")
            spec = derive_twin_spec(
                parent_doc,
                twin_name=name,
                parent_name=parent_name,
                persist=fork_params.get("persist", False),
                role=fork_params.get("role"),
                task=fork_params.get("task"),
                to_home=_resolve_parent_to_home(str(parent_path), parent_doc),
            )
        except (OSError, ValueError, yaml.YAMLError, TwinSeedError) as exc:
            active_handoff.rollback()
            return JSONResponse(
                {
                    "error": f"cannot derive fork from authoritative parent: {exc}",
                    "kind": "twin_parent_unavailable",
                },
                status_code=400,
            )
        authoritative_parent = (parent_name, parent_path, parent_doc)

    if fork_params is None and isinstance(spec, dict) and _twin_parent_from_spec(spec):
        untrusted_parent = _twin_parent_from_spec(spec)
        try:
            _validate_agent_component(untrusted_parent, role="parent")
        except TwinSeedError as exc:
            return JSONResponse(
                {"error": str(exc), "kind": "invalid_agent_name"}, status_code=400
            )
        if untrusted_parent == name:
            return JSONResponse(
                {
                    "error": "fork agent name must differ from its parent",
                    "kind": "twin_identity_mismatch",
                },
                status_code=400,
            )
        return JSONResponse(
            {
                "error": "client-supplied fork execution specs are forbidden",
                "kind": (
                    "twin_agent_auth_unavailable"
                    if caller
                    else "twin_authority_required"
                ),
            },
            status_code=403,
        )

    if not isinstance(spec, dict):
        return JSONResponse(
            {
                "error": "'spec' must be a JSON object (v3 Agent dict)",
                "kind": "spec_invalid",
            },
            status_code=400,
        )
    if spec.get("apiVersion") != "scitex-agent-container/v3":
        return JSONResponse(
            {
                "error": (
                    "inline spec must declare apiVersion: scitex-agent-container/v3"
                ),
                "kind": "spec_invalid",
            },
            status_code=400,
        )
    if spec.get("kind") != "Agent":
        return JSONResponse(
            {
                "error": "inline spec must declare kind: Agent",
                "kind": "spec_invalid",
            },
            status_code=400,
        )

    from ._inline_spec_bind_translate import translate_binds_in_spec

    spec = translate_binds_in_spec(
        spec, caller, parent_binds_lookup=_resolve_parent_binds
    )[0]

    if primary.is_symlink() or spec_path.is_symlink():
        active_handoff.rollback()
        return JSONResponse(
            {
                "error": (
                    f"spec at {spec_path} is authority-managed through a symlink; "
                    "inline spawn cannot overwrite it. Update the authoritative "
                    "source and redeploy with 'sac agents link-specs'"
                ),
                "kind": "already_exists",
            },
            status_code=409,
        )
    spec_existed = spec_path.exists()
    if spec_existed and not overwrite:
        active_handoff.rollback()
        return JSONResponse(
            {
                "error": (
                    f"spec already exists at {spec_path}; pass "
                    "'overwrite': true to replace"
                ),
                "kind": "already_exists",
            },
            status_code=409,
        )

    spec, twin_error = _prepare_twin_host_isolation(
        name,
        spec,
        authoritative_parent=authoritative_parent,
        handoff=active_handoff,
    )
    if twin_error is not None:
        active_handoff.rollback()
        return twin_error

    from ._inline_spec_preflight import (
        preflight_bind_sources,
        preflight_failure_response_body,
    )

    preflight = preflight_bind_sources(spec)
    if not preflight.ok:
        active_handoff.rollback()
        return JSONResponse(preflight_failure_response_body(preflight), status_code=400)

    from ._inline_spec_startup_lint import (
        preflight_failure_response_body as startup_lint_failure_body,
    )
    from ._inline_spec_startup_lint import preflight_startup_commands

    startup_lint = preflight_startup_commands(spec)
    if not startup_lint.ok:
        active_handoff.rollback()
        return JSONResponse(startup_lint_failure_body(startup_lint), status_code=400)

    previous = spec_path.read_bytes() if spec_path.is_file() else None
    try:
        if active_handoff.authority_parent_spec is not None:
            _write_hermes_fork_authority(
                name=name,
                spec=spec,
                parent_spec_path=active_handoff.authority_parent_spec,
                primary=primary,
                handoff=active_handoff,
            )
        else:
            primary.mkdir(parents=True, exist_ok=True)
            active_handoff.spec_path = spec_path
            active_handoff.spec_created = not spec_existed
            active_handoff.spec_previous = previous
            spec_path.write_text(
                yaml.safe_dump(spec, sort_keys=False), encoding="utf-8"
            )
    except (OSError, subprocess.SubprocessError) as exc:
        active_handoff.rollback()
        return JSONResponse(
            {"error": f"failed to write spec: {exc}", "kind": "spec_invalid"},
            status_code=500,
        )
    if handoff is None:
        active_handoff.commit()
    return None
