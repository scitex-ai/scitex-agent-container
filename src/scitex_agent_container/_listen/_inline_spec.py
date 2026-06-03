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


def materialize_inline_spec(
    name: str, spec: object, *, overwrite: bool
) -> JSONResponse | None:
    """Write ``spec`` to ``~/.scitex/agent-container/agents/<name>/spec.yaml``.

    Returns ``None`` on success, or a ``JSONResponse`` carrying the
    failure (so the handler can ``return`` it verbatim). Validation
    pipeline (ordered cheap-to-expensive):

      1. ``spec`` is a dict + v3 apiVersion + Agent kind (basic shape).
      2. ``kind="spec_invalid"`` for any of (1) — wire-stable.
      3. **bind preflight**: every ``spec.apptainer.binds[*]`` host
         source is ``stat()``-checked. Any missing source aborts with
         HTTP 400 + ``kind="bind_unresolvable"`` (PR-1 fail-loud). This
         catches the SAC-from-SAC silent FATAL where the spec carries
         in-SIF ``/work/...`` paths that the host can't see.
      4. ``kind="already_exists"`` for the overwrite-guard collision.
      5. ``kind="spec_invalid"`` for write failure (disk full, RO fs).
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
