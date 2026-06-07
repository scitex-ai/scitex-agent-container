"""TunnelSpec dataclass for ``spec.claude.provider.tunnel`` (SSH ProxyJump).

Lives in its own module to keep ``_provider_types.py`` and ``_types.py``
under the project's 512-line cap (mirrors the existing per-axis split
``_provider_types.py`` / ``_proxy_types.py`` / ``_acl_types.py``).
Re-exported from :mod:`scitex_agent_container.config` so callers see one
canonical name regardless of which sibling file holds the dataclass.

Why a separate dataclass under ``provider``
-------------------------------------------

Some operator-targeted backends (a self-hosted vLLM endpoint on an
HPC compute node, an internal gateway behind a bastion, ...) are only
reachable through an SSH jump host — the canonical case at write time
is Qwen vLLM at ``spartan-gpgpu171:4000`` reachable only via
``ssh -J spartan-login spartan-gpgpu171`` and then localhost:4000 on
the compute node. The provider's ``base_url`` cannot point at the
remote address directly (no routable network path), so sac stands up
a local SSH ``-L`` forward at boot and rewrites ``base_url`` to
``http://localhost:<local_port>``. The forward is supervised so an
``ssh`` disconnect doesn't silently strand the agent on a dead tunnel.

The forward is **declarative**: the operator puts the jump hop,
target hop, and remote port into the spec, and sac handles the rest
(port allocation, supervisor spawn, readiness probe, teardown on
``agent stop``). No imperative pre-step is required.

Fail-loud (never silent fallback)
---------------------------------

* ``jump_host`` / ``target_host`` / ``remote_port`` are mandatory.
  An incomplete tunnel block would either fail at ``ssh`` time
  (cryptic argv error) or silently bind nothing (operator confused
  why ``base_url`` is dead). The parser + validator reject the
  incomplete shape before the runtime starts.
* The supervisor (see :mod:`_network._tunnel_supervisor`) propagates
  the ``ssh`` child's exit code into its own stderr log line — no
  silent restart loop hides a permanent failure.
* If the tunnel never binds within ``wait_timeout_s`` the manager
  raises :class:`_network._tunnel_manager.TunnelUpError` with the
  concrete ``ssh -J <jump> <target>`` reproducer recipe.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class TunnelSpec:
    """SSH ProxyJump-based local forward to the provider endpoint.

    Maps 1:1 onto an ``ssh -N -L <local_port>:localhost:<remote_port> -J
    <jump_host> <target_host>`` invocation supervised by sac for the
    lifetime of the agent. See the module docstring for the WHY.

    Fields are intentionally minimal — anything else the operator wants
    to pass to the underlying ``ssh`` lives in :attr:`ssh_opts`, which
    is appended verbatim to the supervisor's argv. The supervisor
    ALWAYS sets ``-N`` (no remote command), ``-o ServerAliveInterval=30``,
    ``-o ServerAliveCountMax=3``, ``-o ExitOnForwardFailure=yes``, and
    ``-o BatchMode=yes`` so a misconfigured ssh-agent or interactive
    prompt cannot silently hang the supervisor.
    """

    jump_host: str = ""
    """ssh alias / host for the ``-J`` jump. Must be resolvable from
    the host running sac (typically configured in ``~/.ssh/config``).
    Required."""

    target_host: str = ""
    """The final hop reachable from ``jump_host``. The local forward's
    upstream is ``<target_host>:<remote_port>``. Required."""

    remote_port: int = 0
    """TCP port on :attr:`target_host` to forward to. Required;
    validator enforces 1..65535."""

    local_port: int = 0
    """OPTIONAL bind port on ``localhost``. ``0`` (default) requests
    an ephemeral port from the OS — the manager picks a free port via
    a transient socket bind. When the operator pins a port the
    validator enforces the unprivileged range 1024..65535."""

    wait_timeout_s: int = 30
    """OPTIONAL seconds to wait for the local forward to start
    accepting TCP connects before :meth:`TunnelManager.up` raises
    :class:`TunnelUpError`. Default 30 covers a slow ssh ControlMaster
    bootstrap on a busy bastion."""

    respawn_backoff_s: int = 2
    """OPTIONAL seconds the supervisor sleeps between ``ssh`` respawns
    after the child exits. Default 2 keeps a transient drop from
    pounding the bastion. ``0`` is allowed for tight tests; not
    recommended in production."""

    ssh_opts: list[str] = field(default_factory=list)
    """OPTIONAL extra ssh argv tokens, appended verbatim AFTER the
    sac-fixed options. Example: ``["-o", "ServerAliveInterval=15"]``
    overrides sac's default keepalive (``-o`` is last-wins per ssh).
    """


__all__ = ["TunnelSpec"]
