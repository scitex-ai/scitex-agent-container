"""``sac agents spawn-from-here`` — in-SIF spawn POST with outcome JSON.

PR-3 Checkpoint 3 — the dedicated CLI surface for the SAC-from-SAC
spawn pathway. The existing ``sac agents start`` already brokers
in-SIF spawns via :func:`._lifecycle._in_sif_broker.maybe_broker_in_sif_spawn`,
but its output is the legacy free-form text. ``spawn-from-here``
gives the SAC-from-SAC consumer (clew launcher, parent agent's
scripts) a stable wire shape:

  * one JSON line to stdout, the PR-3 Checkpoint 2 outcome shape
    ``{ok, kind, exit_code, http_status, details}``;
  * process exit code mapped from the host listen's ``kind`` per
    the frozen table (0 success / 1 transport / 2-6 server kinds /
    99 unknown).

The lineage edge is recorded by the host listen on accept (same
as ``maybe_broker_in_sif_spawn``); the caller identity is the
``SAC_NAME`` env injected at SIF launch unless overridden.

Distinct from ``start``: ``start`` is the legacy operator-style
verb that does runtime materialisation locally then maybe brokers
the actual spawn; ``spawn-from-here`` is the in-SIF-native verb
that ALWAYS goes through the host listen (and returns the wire-
stable outcome JSON). When the CLI is not running inside a SIF,
``spawn-from-here`` still POSTs to the host listen — it's a
one-purpose verb, not an auto-fallback.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import click


@click.command(name="spawn-from-here")
@click.argument("child_name")
@click.option(
    "--spec-file",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    default=None,
    help=(
        "Path to a v3 Agent spec YAML to inline-POST (the host materialises "
        "it under ~/.scitex/agent-container/agents/<child_name>/spec.yaml). "
        "Omit to spawn an already-host-registered child by name."
    ),
)
@click.option(
    "--caller",
    type=str,
    default=None,
    help=(
        "Override the spawning agent's identity. Defaults to SAC_NAME from "
        "the container env. Empty string forces the admin path (allowed by "
        "the host's check_spawn / lineage gate)."
    ),
)
@click.option(
    "--overwrite",
    is_flag=True,
    default=False,
    help=(
        "Pass overwrite=true in the POST body. Only meaningful with "
        "--spec-file; 409 already_exists when omitted on a name clash."
    ),
)
def spawn_from_here(
    child_name: str,
    spec_file: Path | None,
    caller: str | None,
    overwrite: bool,
) -> None:
    """Post a spawn for CHILD_NAME to the host listen; print outcome JSON.

    \b
    Examples:
      # spawn an already-registered child (host has its spec.yaml)
      sac agents spawn-from-here cohort-a-capsule-0

      # inline-POST a fresh spec
      sac agents spawn-from-here cohort-a-capsule-1 \\
          --spec-file ./capsule-1.spec.yaml

    Stdout: one PR-3 Checkpoint 2 outcome JSON line:
        {"ok", "kind", "exit_code", "http_status", "details"}

    Exit code: per the PR-3 table —
        0 success, 1 transport, 2 bind_unresolvable, 3 spec_invalid,
        4 already_exists, 5 acl_deny, 99 unknown kind.
    """
    from .._env import getenv
    from .._lifecycle._in_sif_http_client import (
        HostListenTransportError,
        host_listen_call,
    )
    from .._lifecycle._in_sif_outcome import (
        build_outcome,
        outcome_to_stdout_json,
        transport_outcome,
    )

    body: dict = {"name": child_name}
    # Caller resolution: explicit > SAC_NAME env > None (admin path).
    if caller is not None:
        body["caller"] = caller or None  # empty string -> None (admin)
    else:
        sac_name = (getenv("NAME", "") or "").strip() or None
        if sac_name:
            body["caller"] = sac_name
    if spec_file is not None:
        try:
            import yaml

            with open(spec_file, encoding="utf-8") as fh:
                spec_dict = yaml.safe_load(fh)
            if not isinstance(spec_dict, dict):
                raise ValueError(
                    f"spec file {spec_file!s} did not parse to a YAML mapping "
                    f"(got {type(spec_dict).__name__!r}); a v3 Agent spec "
                    "must be a dict with apiVersion / kind / spec keys."
                )
        except (OSError, ValueError, yaml.YAMLError) as exc:
            # Mirror the outcome JSON shape on local YAML-load
            # errors so the consumer doesn't have to special-case
            # CLI usage failures.
            sys.stdout.write(
                json.dumps(
                    {
                        "ok": False,
                        "kind": "spec_invalid",
                        "exit_code": 3,
                        "http_status": None,
                        "details": {
                            "error": f"could not read spec file: {exc}",
                            "path": str(spec_file),
                        },
                    }
                )
                + "\n"
            )
            sys.exit(3)
        body["spec"] = spec_dict
        if overwrite:
            body["overwrite"] = True
    try:
        status, resp = host_listen_call("POST", "/agents", body=body)
        outcome = build_outcome(http_status=status, body=resp)
    except HostListenTransportError as exc:
        outcome = transport_outcome(str(exc), url=exc.url)
    sys.stdout.write(outcome_to_stdout_json(outcome))
    sys.exit(outcome.exit_code)


__all__ = ["spawn_from_here"]
