"""Selected local-target auth guard for the library send surface.

Container prompts use the authenticated host send route before this function.
Foreign live endpoints use the canonical authenticated SSH/turn route; the
sender must never infer that peer's model credentials from a local spec or a
hardcoded Claude file. The selected local spec owns any genuine Claude check.
The legacy SSH probe remains exported for compatibility but is not invoked by
send dispatch.
"""

from __future__ import annotations

import subprocess
from copy import deepcopy
from pathlib import Path
from typing import Any, Callable

__all__ = ["preflight_send_creds", "default_ssh_runner", "PROBE_PYTHON_SCRIPT"]

# Tiny remote probe. Reads the peer's credentials.json (path is argv[1]),
# compares ``expiresAt`` (milliseconds) against ``now + 5min`` skew, and
# exits 0 if the token has enough life left, 1 otherwise. Any other
# exit code (parse failure, missing key, etc.) bubbles up as a generic
# ``status="error"`` so the operator can investigate.
PROBE_PYTHON_SCRIPT = (
    "import json,sys,time;"
    "d=json.load(open(sys.argv[1]));"
    "now=time.time()*1000;"
    "ea=d['claudeAiOauth']['expiresAt'];"
    "sys.exit(0 if ea>now+300000 else 1)"
)

SshRunner = Callable[..., "subprocess.CompletedProcess[str]"]


def default_ssh_runner(
    peer_host: str, remote_creds_path: str
) -> "subprocess.CompletedProcess[str]":
    """Run the OAuth probe on ``peer_host`` via ssh, return the CompletedProcess.

    Kept as a module-level function (not a closure) so tests can swap
    it via the ``ssh_runner=`` parameter without monkeypatching.

    The probe shares the package-wide ssh ControlMaster pool (see
    :func:`scitex_agent_container._state.host_config.ssh_control_options`)
    so a `sac send` storm against the same peer doesn't pay N TCP
    handshakes nor blow through Spartan's MaxSessions cap.
    """
    from .._state.host_config import ssh_control_options

    return subprocess.run(
        [
            "ssh",
            *ssh_control_options(),
            peer_host,
            "python3",
            "-c",
            PROBE_PYTHON_SCRIPT,
            remote_creds_path,
        ],
        capture_output=True,
        text=True,
        check=False,
        timeout=15,
    )


def preflight_send_creds(
    name: str,
    *,
    peer_host: str,
    current_host: str,
    lead_creds_path: Path | None = None,
    remote_creds_path: str = "~/.claude/.credentials.json",
    ssh_runner: SshRunner | None = None,
    now: float | None = None,
) -> dict[str, Any] | None:
    """Validate local selected auth; foreign transport keeps peer authority.

    ``lead_creds_path`` is a legacy test seam for an unpinned Claude target
    only. It never changes a spec-declared account/file and cannot substitute
    for a missing, ambiguous or invalid selected target spec.
    """
    if peer_host != current_host:
        # Exactly the CLI's canonical foreign SSH/live-turn route. The peer's
        # existing runtime owns provider auth; sender Claude files are irrelevant.
        return None

    from .._state._preflight_creds import (
        check_spec_oauth_credentials,
        spec_credential_candidates,
    )
    from ..config import load_config
    from ..config._harness_lookup import canonical_harness
    from ..config._resolve import resolve_with_prefix
    from .lifecycle._common import _local_host_names
    from .lifecycle._host_routing import classify_spec_host_route

    try:
        config = load_config(resolve_with_prefix(name))
        family = canonical_harness(config.harness)
        if family is None:
            raise ValueError(f"unknown selected harness {config.harness!r}")
        kind, _peer = classify_spec_host_route(
            config.hosts_spec.host,
            current_host,
            {},
            local_names=_local_host_names(current_host),
        )
        if kind != "local":
            raise ValueError(
                "selected spec host conflicts with the local live endpoint"
            )
    except (LookupError, OSError, ValueError, RuntimeError) as exc:
        return {
            "status": "error",
            "agent": name,
            "error": f"selected target spec: {exc}",
        }
    if family != "anthropic" or config.claude.provider is not None:
        return None

    _candidates, declared = spec_credential_candidates(config.claude)
    if lead_creds_path is not None and not declared:
        config = deepcopy(config)
        config.claude.credentials_file = str(lead_creds_path)
    try:
        check_spec_oauth_credentials(config, now=now)
    except (OSError, ValueError, RuntimeError) as exc:
        return {
            "status": "creds-expired",
            "agent": name,
            "error": f"target creds: {exc}",
        }
    return None
