"""Write an inline v3 Agent spec to the canonical install root.

Used by ``POST /v1/sac/agents`` when the request body carries a
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
    failure (so the handler can ``return`` it verbatim). Validates the
    bare minimum (dict, v3 apiVersion, Agent kind) so a malformed body
    becomes a 400 instead of a downstream ``sac agent start`` crash.
    """
    import yaml

    if not isinstance(spec, dict):
        return JSONResponse(
            {"error": "'spec' must be a JSON object (v3 Agent dict)"},
            status_code=400,
        )
    if spec.get("apiVersion") != "scitex-agent-container/v3":
        return JSONResponse(
            {
                "error": (
                    "inline spec must declare apiVersion: scitex-agent-container/v3"
                )
            },
            status_code=400,
        )
    if spec.get("kind") != "Agent":
        return JSONResponse(
            {"error": "inline spec must declare kind: Agent"}, status_code=400
        )

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
                )
            },
            status_code=409,
        )
    try:
        primary.mkdir(parents=True, exist_ok=True)
        spec_path.write_text(yaml.safe_dump(spec, sort_keys=False), encoding="utf-8")
    except OSError as exc:
        return JSONResponse({"error": f"failed to write spec: {exc}"}, status_code=500)
    return None
