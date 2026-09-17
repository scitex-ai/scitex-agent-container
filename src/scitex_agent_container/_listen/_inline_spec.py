"""Write an inline v3 Agent spec to the canonical install root.

Used by ``POST /agents`` when the request body carries a
``spec`` dict instead of (or alongside) a bare ``name``. Lets external
orchestrators register-and-start agents in one HTTP call without
staging YAML on the sac host out-of-band.
"""

from __future__ import annotations

import os
import subprocess
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
    """Rollback metadata for writes performed before the start handoff."""

    worktree_parent: Path | None = None
    worktree_path: Path | None = None
    worktree_created: bool = False
    seed_path: Path | None = None
    spec_path: Path | None = None
    spec_created: bool = False
    spec_previous: bytes | None = None

    def rollback(self) -> None:
        """Remove only artifacts created by this request."""
        if self.seed_path is not None:
            self.seed_path.unlink(missing_ok=True)
            try:
                self.seed_path.parent.rmdir()
            except OSError:
                pass
        if self.spec_path is not None:
            if self.spec_created:
                self.spec_path.unlink(missing_ok=True)
                try:
                    self.spec_path.parent.rmdir()
                except OSError:
                    pass
            elif self.spec_previous is not None:
                self.spec_path.write_bytes(self.spec_previous)
        if (
            self.worktree_created
            and self.worktree_parent is not None
            and self.worktree_path is not None
        ):
            subprocess.run(
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
            subprocess.run(
                ["git", "-C", str(self.worktree_parent), "worktree", "prune"],
                check=False,
                capture_output=True,
                text=True,
            )


def _inject_twin_workdir_bind(spec: dict, host_worktree: Path, container_workdir: str) -> dict:
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
    caller: str | None,
    authority: str | None,
    agents_root: Path,
    handoff: InlineSpecHandoff,
) -> tuple[dict, JSONResponse | None]:
    """Validate a twin claim, create its host worktree, and inject its bind."""
    parent_name = _twin_parent_from_spec(spec)
    if not parent_name:
        return spec, None

    import yaml

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
            {"error": "fork agent name must differ from its parent", "kind": "twin_identity_mismatch"},
            status_code=400,
        )
    if authority == "admin" and caller is None:
        pass
    elif caller:
        return spec, JSONResponse(
            {
                "error": (
                    "agent-authenticated fork spawning is unavailable: the current "
                    "host-wide bearer does not cryptographically bind caller identity"
                ),
                "kind": "twin_agent_auth_unavailable",
            },
            status_code=403,
        )
    else:
        return spec, JSONResponse(
            {
                "error": "fork spawn requires explicit admin authority",
                "kind": "twin_authority_required",
            },
            status_code=403,
        )

    from ..config import resolve_config

    try:
        parent_path = Path(resolve_config(parent_name))
        parent_doc = yaml.safe_load(parent_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, yaml.YAMLError) as exc:
        return spec, JSONResponse(
            {
                "error": f"cannot read authoritative parent spec: {exc}",
                "kind": "twin_parent_unavailable",
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
        _reject_symlink_components(
            Path(expected_overlay), label="fork overlay"
        )
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
        from .._lifecycle._twin import _materialize_hermes_fork_seed
        from ..runtimes._hermes_tui_rpc import HermesTuiRpcError

        try:
            handoff.seed_path = _materialize_hermes_fork_seed(
                parent_name=parent_name,
                child_name=name,
                parent_spec=parent_spec,
                child_spec=child_spec,
            )
        except (TwinSeedError, HermesTuiRpcError, OSError) as exc:
            handoff.rollback()
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
    authority: str | None = None,
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
            {"error": f"target escapes agents root: {name!r}", "kind": "invalid_agent_name"},
            status_code=400,
        )
    spec_path = primary / "spec.yaml"
    active_handoff = handoff if handoff is not None else InlineSpecHandoff()

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
        caller=caller,
        authority=authority,
        agents_root=agents_dir,
        handoff=active_handoff,
    )
    if twin_error is not None:
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
        primary.mkdir(parents=True, exist_ok=True)
        spec_path.write_text(yaml.safe_dump(spec, sort_keys=False), encoding="utf-8")
    except OSError as exc:
        active_handoff.rollback()
        return JSONResponse(
            {"error": f"failed to write spec: {exc}", "kind": "spec_invalid"},
            status_code=500,
        )
    active_handoff.spec_path = spec_path
    active_handoff.spec_created = not spec_existed
    active_handoff.spec_previous = previous
    return None
