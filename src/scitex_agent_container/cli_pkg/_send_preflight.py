"""Pre-dispatch creds expiry probe for ``sac agents send``.

Companion to :mod:`scitex_agent_container._state._preflight_creds`
(which guards ``sac agents start``). Same hard-rule shape: refuse the
dispatch loudly when the target host can't authenticate, so a stale
OAuth token surfaces as a clear ``creds-expired`` status instead of
silently becoming a 401 buried in ``session.jsonl``.

Two surfaces are probed:

* The lead-local credentials file (``~/.claude/.credentials.json``)
  always gates the call — the lead's token is what the in-container
  agent will bind-mount and use, regardless of which host runs the
  actual subprocess.
* When the target row points at a remote peer, an ``ssh`` probe runs a
  tiny Python one-liner on the peer to verify that *its* credentials
  file is still good. Failure modes are categorised explicitly:

  =======================  =============================================
  ssh exit code            mapped status
  =======================  =============================================
  0                        pass (return None)
  1                        ``status="creds-expired"``
  any other (incl. raise)  ``status="error"``
  =======================  =============================================

Skipped entirely when ``ANTHROPIC_API_KEY`` or ``SAC_ANTHROPIC_API_KEY``
is set — that's the API-key auth path and the OAuth credentials file
is moot.
"""

from __future__ import annotations

import os
import subprocess
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


def _api_key_env_is_set() -> bool:
    """True when the operator has opted into the API-key auth path."""
    return bool(
        os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("SAC_ANTHROPIC_API_KEY")
    )


def default_ssh_runner(
    peer_host: str, remote_creds_path: str
) -> "subprocess.CompletedProcess[str]":
    """Run the OAuth probe on ``peer_host`` via ssh, return the CompletedProcess.

    Kept as a module-level function (not a closure) so tests can swap
    it via the ``ssh_runner=`` parameter without monkeypatching.
    """
    return subprocess.run(
        ["ssh", peer_host, "python3", "-c", PROBE_PYTHON_SCRIPT, remote_creds_path],
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
    """Probe lead + (optionally) peer creds; return ``None`` on pass.

    Returns
    -------
    None
        Every surface looks healthy — caller may proceed with dispatch.
    dict
        Failure payload shaped like the rest of ``_send.py``'s error
        returns:

        * ``{"status": "creds-expired", "error": str, "agent": str, ...}``
          when an OAuth token is expired / near-expiry on either side.
        * ``{"status": "error", "error": str, "agent": str, ...}`` when
          the ssh probe itself fails for an unexpected reason
          (non-{0,1} exit code or runner exception).

    Parameters
    ----------
    name
        Agent name; echoed back in the failure payload so the caller
        doesn't have to re-thread it.
    peer_host
        ``row["host"]`` from state.db — the host that will actually
        receive the /v1/turn POST.
    current_host
        Lead-side resolved host (``_resolve_host(None)``); when equal
        to ``peer_host`` the ssh probe is skipped and only the lead's
        local creds are checked.
    lead_creds_path
        Override path for the lead-local credentials file. Defaults to
        ``~/.claude/.credentials.json``. Tests pass an explicit
        ``tmp_path`` so the operator's real file is never read.
    remote_creds_path
        Path to the credentials file on the peer (passed verbatim to
        ``ssh ... python3 -c <probe> <path>``). Defaults to
        ``~/.claude/.credentials.json``.
    ssh_runner
        Injection seam for tests. Defaults to :func:`default_ssh_runner`.
    now
        Override for the wall clock; threaded through to
        :func:`check_oauth_token_expiry` for deterministic tests.
    """
    if _api_key_env_is_set():
        return None

    from .._state._preflight_creds import check_oauth_token_expiry

    # Lead-local creds: always check.
    try:
        check_oauth_token_expiry(
            lead_creds_path or (Path.home() / ".claude" / ".credentials.json"),
            now=now,
        )
    except (FileNotFoundError, ValueError, RuntimeError) as exc:
        return {
            "status": "creds-expired",
            "error": f"lead creds: {exc}",
            "agent": name,
        }

    # Cross-host: ssh-probe the peer.
    if peer_host != current_host:
        runner = ssh_runner or default_ssh_runner
        try:
            result = runner(peer_host, remote_creds_path)
        except Exception as exc:  # noqa: BLE001 — categorise loudly below
            return {
                "status": "error",
                "error": f"ssh probe to {peer_host} failed: {exc}",
                "agent": name,
                "peer": peer_host,
            }
        if result.returncode == 0:
            return None
        if result.returncode == 1:
            return {
                "status": "creds-expired",
                "error": f"creds expired on {peer_host}",
                "agent": name,
                "peer": peer_host,
            }
        return {
            "status": "error",
            "error": (
                f"ssh probe to {peer_host} returned rc={result.returncode}: "
                f"{result.stderr[:200]}"
            ),
            "agent": name,
            "peer": peer_host,
        }

    return None
