"""Fleet-wide spec-source drift check for ``sac doctor --fleet``.

For each configured peer host (from ``config.yaml``'s ``peers:`` block)
this ssh-runs the SAME git-drift comparison the launch-time local check
does, against the peer's own agent-spec source repo, and collects a
:class:`DriftStatus` per host. The CLI layer renders a per-host table.

The remote check is a single self-contained POSIX-sh snippet (no
dependency on the peer's sac version): it resolves the agents dir,
asks git for the toplevel + upstream, and prints a one-line
``DRIFT <state> <ahead> <behind> <upstream>`` marker the local side
parses back into a :class:`DriftStatus`. Unreachable / non-git / no-
upstream peers degrade to UNREACHABLE / NOT_A_REPO — never an exception.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass

from .._state.host_config import PeerSpec, build_ssh_argv
from ._status import DriftState, DriftStatus

# Remote agents-dir path. On fleet hosts this symlinks into the dotfiles
# checkout; ``git -C`` follows it to the real repo. ``$HOME`` is expanded
# by the remote shell. Honour SCITEX_DIR for relocated user-state roots.
_REMOTE_AGENTS_REL = ".scitex/agent-container/agents"

# Marker the remote snippet prints; parsed back on the local side.
_MARKER = "SAC_DRIFT"

# POSIX-sh that runs on the peer. Resolves the agents dir, finds the git
# toplevel, compares HEAD against @{upstream} after a fetch, and prints
# exactly one ``SAC_DRIFT <state> <ahead> <behind> <upstream>`` line.
# Every failure path prints a marker line too (never silent) so the
# local parser always has something to read.
_REMOTE_SNIPPET = r"""
set -u
base="${SCITEX_DIR:-$HOME/.scitex}"
dir="$base/agent-container/agents"
if [ ! -e "$dir" ]; then dir="$HOME/.scitex/agent-container/agents"; fi
if ! command -v git >/dev/null 2>&1; then
  echo "SAC_DRIFT not-a-repo 0 0 - (git unavailable)"; exit 0
fi
top=$(git -C "$dir" rev-parse --show-toplevel 2>/dev/null)
if [ -z "$top" ]; then
  echo "SAC_DRIFT not-a-repo 0 0 - (spec source not in a git repo)"; exit 0
fi
up=$(git -C "$top" rev-parse --abbrev-ref --symbolic-full-name '@{upstream}' 2>/dev/null)
if [ -z "$up" ]; then
  echo "SAC_DRIFT unreachable 0 0 - (no upstream configured)"; exit 0
fi
if ! git -C "$top" fetch --quiet 2>/dev/null; then
  echo "SAC_DRIFT unreachable 0 0 $up (fetch failed)"; exit 0
fi
ahead=$(git -C "$top" rev-list --count "$up..HEAD" 2>/dev/null)
behind=$(git -C "$top" rev-list --count "HEAD..$up" 2>/dev/null)
if [ -z "$ahead" ] || [ -z "$behind" ]; then
  echo "SAC_DRIFT unreachable 0 0 $up (compare failed)"; exit 0
fi
if [ "$ahead" -gt 0 ] && [ "$behind" -gt 0 ]; then st=diverged
elif [ "$behind" -gt 0 ]; then st=behind
elif [ "$ahead" -gt 0 ]; then st=ahead
else st=current; fi
echo "SAC_DRIFT $st $ahead $behind $up"
"""


@dataclass(frozen=True)
class HostDrift:
    """One peer's drift verdict for the fleet table."""

    host: str
    status: DriftStatus

    def to_dict(self) -> dict:
        d = {"host": self.host}
        d.update(self.status.to_dict())
        return d


def _parse_marker(line: str) -> DriftStatus:
    """Parse a ``SAC_DRIFT <state> <ahead> <behind> <upstream> [detail]`` line.

    Anything unparseable degrades to UNREACHABLE with the raw line as
    detail — the local side never raises on remote output.
    """
    parts = line.strip().split(None, 5)
    if len(parts) < 5 or parts[0] != _MARKER:
        return DriftStatus(
            state=DriftState.UNREACHABLE,
            detail=f"unparseable remote output: {line.strip()[:120]!r}",
        )
    _marker, state_raw, ahead_raw, behind_raw, upstream = parts[:5]
    detail = parts[5].strip("() ") if len(parts) == 6 else ""
    try:
        ahead = int(ahead_raw)
        behind = int(behind_raw)
    except (
        ValueError
    ):  # stx-allow: fallback (reason: malformed remote counts → treat as unknown drift)
        return DriftStatus(
            state=DriftState.UNREACHABLE,
            detail=f"non-numeric remote counts: {line.strip()[:120]!r}",
        )
    try:
        state = DriftState(state_raw)
    except ValueError:  # stx-allow: fallback (reason: unknown state token from a future/older peer → unknown drift)
        return DriftStatus(
            state=DriftState.UNREACHABLE,
            detail=f"unknown remote state {state_raw!r}",
        )
    upstream_clean = "" if upstream == "-" else upstream
    return DriftStatus(
        state=state,
        ahead=ahead,
        behind=behind,
        upstream=upstream_clean,
        detail=detail,
    )


def _extract_marker_line(stdout: str) -> str | None:
    """Return the last ``SAC_DRIFT`` line in ``stdout`` (or None).

    The peer's login shell may emit motd / rc noise before our marker;
    scan for the marker line specifically rather than trusting the
    whole stdout to be just our echo.
    """
    found = None
    for ln in stdout.splitlines():
        if ln.strip().startswith(_MARKER + " "):
            found = ln
    return found


def check_peer_drift(
    peer_name: str,
    peers: dict[str, PeerSpec],
    *,
    timeout: int = 30,
    runner=subprocess.run,
) -> HostDrift:
    """ssh ``peer_name`` and run the remote drift snippet; never raises.

    Args:
        peer_name: peer key from config.yaml's ``peers:`` block.
        peers: the parsed peers map (for ssh argv + ProxyJump chain).
        timeout: ssh wall-clock cap; a hung peer maps to UNREACHABLE.
        runner: injectable ``subprocess.run``-shaped callable (real;
            tests pass a real callable so no mocks are used).

    Returns:
        A :class:`HostDrift`. Unreachable / non-git / no-upstream peers
        all degrade gracefully — the fleet check never crashes on one
        bad host.
    """
    try:
        ssh_argv = build_ssh_argv(
            peer_name,
            ["sh", "-c", _REMOTE_SNIPPET],
            peers,
            extra_opts=["-o", f"ConnectTimeout={min(timeout, 15)}"],
        )
    except KeyError:
        return HostDrift(
            host=peer_name,
            status=DriftStatus(
                state=DriftState.UNREACHABLE,
                detail="peer not defined in config.yaml",
            ),
        )
    try:
        proc = runner(
            ssh_argv,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return HostDrift(
            host=peer_name,
            status=DriftStatus(
                state=DriftState.UNREACHABLE, detail=f"ssh timed out after {timeout}s"
            ),
        )
    except (
        FileNotFoundError,
        OSError,
        subprocess.SubprocessError,
    ) as exc:  # stx-allow: fallback (reason: ssh missing / spawn error → unreachable, never crash the fleet check)
        return HostDrift(
            host=peer_name,
            status=DriftStatus(
                state=DriftState.UNREACHABLE,
                detail=f"ssh failed: {type(exc).__name__}",
            ),
        )
    marker = _extract_marker_line(proc.stdout or "")
    if marker is None:
        stderr_tail = (proc.stderr or "").strip().splitlines()[-1:] or [""]
        return HostDrift(
            host=peer_name,
            status=DriftStatus(
                state=DriftState.UNREACHABLE,
                detail=(
                    f"no drift marker in remote output (exit {proc.returncode}): "
                    f"{stderr_tail[0][:120]}"
                ),
            ),
        )
    return HostDrift(host=peer_name, status=_parse_marker(marker))


def check_fleet_drift(
    peers: dict[str, PeerSpec],
    *,
    timeout: int = 30,
    runner=subprocess.run,
) -> list[HostDrift]:
    """Check every peer's spec-source drift; return one row per peer.

    Serial ssh round-trips (fleets are small; parallelism is a future
    optimization, not a correctness concern). Order matches the sorted
    peer names for a stable table.
    """
    rows: list[HostDrift] = []
    for name in sorted(peers):
        rows.append(check_peer_drift(name, peers, timeout=timeout, runner=runner))
    return rows


__all__ = [
    "HostDrift",
    "check_fleet_drift",
    "check_peer_drift",
]
