"""The ONE mutating operation in ``sac host sync``: a fast-forward.

Everything else in :mod:`.._hostsync` is inert. Keeping the single write
in its own small module means the blast radius of this verb can be
audited by reading one file.

Fast-forward-only, and git enforces it
--------------------------------------
The remote command is ``git merge --ff-only <ref>``. Not ``pull``
(which can merge), not ``rebase``, and above all not ``reset --hard``.
If the peer's HEAD is not an ancestor of the ref, git itself REFUSES and
exits non-zero — so the "never destroy remote commits" invariant is
enforced by the tool of record, not merely by our own upstream checks.
The caller's :func:`.._model.sync_decision` has already refused that
case; this is the second lock on the same door, and it is the one that
still holds if the first is ever refactored away.

``--force`` does not reach here. It relaxes the CI-idle SCHEDULING
guard; it never buys a destructive git operation. There is deliberately
no code path in sac that discards a remote commit or an uncommitted
remote edit: those are printed and handed to a human.

Credentials, later
------------------
Code and credentials are both centre → remote, one-way, and both want
exactly this shape: resolve the peer through the registry, probe before
writing, refuse on drift, verify after. When credential distribution is
decided (the ``--push-to`` proposal is explicitly marked "do not
deploy"), it should ride THIS channel — a sibling apply step behind the
same preconditions — rather than growing a second, unguarded path to the
same hosts. This PR deliberately ships code sync only.
"""

from __future__ import annotations

import shlex
import subprocess
from dataclasses import dataclass

from .._state.host_config import PeerSpec, build_ssh_argv

__all__ = ["FastForwardResult", "apply_fast_forward", "render_apply_snippet"]


@dataclass(frozen=True)
class FastForwardResult:
    """Outcome of the remote ``git merge --ff-only``."""

    ok: bool
    exit_code: int = 0
    stdout: str = ""
    stderr: str = ""

    @property
    def message(self) -> str:
        """The remote's own words — never a summary we invented."""
        return (self.stderr or self.stdout or "").strip()


def render_apply_snippet(repo: str, ref: str) -> str:
    """Render the fast-forward-only merge that runs on the peer.

    Both arguments are shell-quoted. ``repo`` is the ABSOLUTE checkout
    root the probe resolved by asking the peer's interpreter where it
    loads sac from — no ``~`` is expanded on either side, which is the
    bug this whole subsystem exists to avoid.
    """
    return f"set -eu\ngit -C {shlex.quote(repo)} merge --ff-only {shlex.quote(ref)}\n"


def apply_fast_forward(
    peer: str,
    peers: dict[str, PeerSpec],
    *,
    repo: str,
    ref: str,
    timeout: int = 120,
    runner=subprocess.run,
) -> FastForwardResult:
    """Fast-forward ``peer``'s checkout at ``repo`` to ``ref``. Never raises.

    Rides :func:`build_ssh_argv`, sac's single remote-dispatch choke
    point, so the peer's ProxyJump chain and Lmod ``env_preamble`` apply
    here exactly as they do everywhere else. A second ssh path would be
    a second thing to fix; this codebase has been bitten by a primitive
    repaired at one of six call sites.

    Returns a :class:`FastForwardResult` carrying the remote's own
    stdout/stderr. A non-fast-forward is reported as a failure with
    git's message intact — we never paraphrase a refusal into a success.
    """
    try:
        ssh_argv = build_ssh_argv(
            peer,
            ["sh", "-c", render_apply_snippet(repo, ref)],
            peers,
            extra_opts=["-o", f"ConnectTimeout={min(timeout, 15)}"],
        )
    except KeyError:
        return FastForwardResult(
            ok=False,
            exit_code=2,
            stderr=f"peer '{peer}' is not defined in config.yaml",
        )
    try:
        proc = runner(
            ssh_argv, capture_output=True, text=True, timeout=timeout, check=False
        )
    except subprocess.TimeoutExpired:
        return FastForwardResult(
            ok=False, exit_code=124, stderr=f"ssh timed out after {timeout}s"
        )
    except (
        FileNotFoundError,
        OSError,
        subprocess.SubprocessError,
    ) as exc:  # stx-allow: fallback (reason: ssh spawn failure → a LOUD failed sync, never a claimed success)
        return FastForwardResult(
            ok=False, exit_code=1, stderr=f"ssh failed: {type(exc).__name__}: {exc}"
        )
    return FastForwardResult(
        ok=proc.returncode == 0,
        exit_code=proc.returncode,
        stdout=proc.stdout or "",
        stderr=proc.stderr or "",
    )
