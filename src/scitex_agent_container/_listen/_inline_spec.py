"""Write an inline v3 Agent spec to the canonical install root.

Used by ``POST /agents`` when the request body carries a
``spec`` dict instead of (or alongside) a bare ``name``. Lets external
orchestrators register-and-start agents in one HTTP call without
staging YAML on the sac host out-of-band.
"""

from __future__ import annotations

import os
from pathlib import Path

from starlette.responses import JSONResponse


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
      6. ``kind="already_exists"`` for the overwrite-guard collision.
      7. ``kind="spec_invalid"`` for write failure (disk full, RO fs).

    Args:
        name: target agent name.
        spec: the inline v3 Agent spec dict from the POST body.
        overwrite: 409 if a spec already exists at the target path
            unless this is ``True``.
        caller: PR-2 — the spawning node's name. ``None`` (or an
            unknown caller) disables bind-translate and the spec is
            forwarded to the preflight unchanged. The same caller
            field drives the WI-2 spawn gate one level up in the
            request handler.
    """
    import yaml

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

    # PR-2 — bind translate. Run BEFORE the PR-1 preflight so the
    # common SAC-from-SAC case (parent launcher posts a child spec
    # whose bind sources are the parent's in-SIF view, e.g.
    # ``/work/data/X``) no longer requires the launcher to
    # pre-translate paths. Read-only, best-effort: any failure to
    # resolve the caller's parent record collapses to a no-op and
    # PR-1 catches the leak. The translated spec is the one PR-1
    # validates and (on success) the handler persists to disk.
    from ._inline_spec_bind_translate import translate_binds_in_spec

    spec = translate_binds_in_spec(
        spec, caller, parent_binds_lookup=_resolve_parent_binds
    )[0]

    # PR-1 — fail-loud bind-source preflight. Done BEFORE writing the
    # spec to disk so a rejected spawn leaves zero artifacts (no spec
    # dir, no lineage record, no runtime dir). The clew capsule-0201225
    # incident on 2026-06-02 took 50 minutes to diagnose because the
    # apptainer FATAL was silent at the HTTP layer; this turns it into
    # a structured 400 the caller can branch on by ``kind``.
    from ._inline_spec_preflight import (
        preflight_bind_sources,
        preflight_failure_response_body,
    )

    preflight = preflight_bind_sources(spec)
    if not preflight.ok:
        return JSONResponse(preflight_failure_response_body(preflight), status_code=400)

    # Follow-up to PR-1/2/3 — startup_commands first-token lint. The
    # clew launcher #70 incident on 2026-06-03 put the agent's CLAUDE
    # mission prompt into spec.startup_commands by mistake; the first
    # line ``"You: ..."`` ran as a shell command and bash logged
    # ``You: command not found``. The bind preflight above does not
    # cover startup_commands, so this sibling preflight catches the
    # class. Done AFTER bind preflight so the cheap-to-expensive order
    # holds (shlex+which is cheap but the bind stat() chain is even
    # cheaper). Wire shape: ``kind="spec_invalid"`` (re-using the
    # existing enum already used by apiVersion/kind validation above)
    # with per-entry ``reason`` sub-shade enum.
    from ._inline_spec_startup_lint import (
        preflight_failure_response_body as startup_lint_failure_body,
    )
    from ._inline_spec_startup_lint import (
        preflight_startup_commands,
    )

    startup_lint = preflight_startup_commands(spec)
    if not startup_lint.ok:
        return JSONResponse(startup_lint_failure_body(startup_lint), status_code=400)

    primary = (
        Path(os.path.expanduser("~")) / ".scitex" / "agent-container" / "agents" / name
    )
    spec_path = primary / "spec.yaml"
    if spec_path.exists() and not overwrite:
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
    try:
        primary.mkdir(parents=True, exist_ok=True)
        spec_path.write_text(yaml.safe_dump(spec, sort_keys=False), encoding="utf-8")
    except OSError as exc:
        return JSONResponse(
            {"error": f"failed to write spec: {exc}", "kind": "spec_invalid"},
            status_code=500,
        )
    return None
