#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""``sac agents twin`` — spawn a context-inheriting twin of a running agent.

A TWIN is a NEW agent forked from PARENT's live session: it inherits the
parent's conversation transcript at birth, then diverges. The parent never
stops. See the ``twin-spawning`` skill + docs/adr/0019 for when to use one
(inherit-but-don't-share-future-context / split work / don't block the
parent) and the safety-critical identity contract (author = twin, card
owner = parent).

This command is a thin front-end: it derives the twin's inline spec from
the parent's on-disk spec (``_lifecycle._twin.derive_twin_spec``) and POSTs
it to the host ``sac listen`` via the shared spawn substrate
(``_lifecycle._spawn_client.request_spawn``) — the same host-broker path
``agent_spawn`` / ``spawn-from-here`` use, so it works both on the bare host
and brokered from inside a parent's container. The host materialises the
spec and starts the twin; the host-side ``seed_twin_from_parent`` step then
copies the parent's transcript and seeds the twin's session marker so its
``session: continue`` resumes it (context inheritance, first boot only).
"""

from __future__ import annotations

import json
import sys

import click

from .._helpers import agent_name_complete, console


def _parse_ttl(raw: str) -> int:
    """Parse a ``--ttl`` duration into whole seconds.

    Accepts a bare integer (seconds) or an integer with a ``s`` / ``m`` /
    ``h`` / ``d`` suffix (e.g. ``90s``, ``30m``, ``2h``, ``1d``). Raises
    :class:`click.BadParameter` on anything else — fail loud, never
    silently coerce a typo to 0.
    """
    s = str(raw).strip().lower()
    if not s:
        raise click.BadParameter("empty --ttl")
    units = {"s": 1, "m": 60, "h": 3600, "d": 86400}
    mult = 1
    if s[-1] in units:
        mult = units[s[-1]]
        s = s[:-1]
    try:
        value = int(s)
    except ValueError as exc:
        raise click.BadParameter(
            f"invalid --ttl {raw!r}; use seconds or a s/m/h/d suffix "
            "(e.g. 90s, 30m, 2h, 1d)."
        ) from exc
    if value <= 0:
        raise click.BadParameter(f"--ttl must be positive, got {raw!r}")
    return value * mult


def _schedule_ttl_stop(twin_name: str, ttl_seconds: int) -> str:
    """Schedule a detached host-side ``sac agents stop <twin> --force`` after TTL.

    Best-effort soft cap (a detached timer, not a durable scheduler — it
    does not survive a host reboot). Runs on the HOST where the twin lives:
    directly via a detached subprocess when on the bare host, or brokered
    through the ``sac listen`` host_exec bypass when called from inside a
    container. Returns a short human note describing what was scheduled.
    """
    import shlex

    from ..._lifecycle._in_sif_broker import is_in_sif

    inner = f"sleep {ttl_seconds}; sac agents stop {shlex.quote(twin_name)} --force"
    if is_in_sif():
        # Broker to the host: background + setsid so the daemon call returns
        # immediately instead of blocking for the whole TTL.
        from ..._lifecycle._host_exec_client import (
            HostExecRequestError,
            request_host_exec,
        )

        detached = f"setsid sh -c {json.dumps(inner)} </dev/null >/dev/null 2>&1 &"
        try:
            request_host_exec(["/bin/sh", "-c", detached], timeout_s=15.0)
        except HostExecRequestError as exc:
            return f"WARNING: could not schedule --ttl auto-stop on host ({exc})"
        return f"auto-stop scheduled on host in {ttl_seconds}s (detached timer)"

    import subprocess

    # stx-allow: fallback (reason: a failed TTL scheduling must warn, not
    # abort — the twin is already spawned; the operator can stop it by hand)
    try:
        subprocess.Popen(
            ["/bin/sh", "-c", inner],
            start_new_session=True,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except OSError as exc:
        return f"WARNING: could not schedule --ttl auto-stop ({exc})"
    return f"auto-stop scheduled in {ttl_seconds}s (detached timer)"


@click.command(name="twin")
@click.argument("parent", type=str, shell_complete=agent_name_complete)
@click.option(
    "--name",
    "twin_name",
    type=str,
    default=None,
    help="Twin agent name (default: <parent>-twin, bumped to -2/-3 if taken).",
)
@click.option(
    "--task",
    type=str,
    default=None,
    help="Boot-kick prompt fed to the twin after it resumes the parent's "
    "session (its divergence mission). Omit to have the twin stand by.",
)
@click.option(
    "--persist",
    is_flag=True,
    default=False,
    help="Long-lived companion twin (restart.policy: always). Default is "
    "ephemeral (restart.policy: never). Mutually exclusive with --ttl.",
)
@click.option(
    "--ttl",
    type=str,
    default=None,
    help="Auto-stop the (ephemeral) twin after this duration "
    "(e.g. 90s, 30m, 2h, 1d). Mutually exclusive with --persist.",
)
@click.option(
    "--role",
    type=str,
    default=None,
    help="Override metadata.labels.role on the twin (else inherits parent's).",
)
@click.option(
    "--caller",
    type=str,
    default=None,
    help="Override the spawn caller identity for the lineage/ACL gate. "
    "Defaults to SAC_NAME (the parent when an agent spawns its own twin).",
)
@click.option(
    "--json",
    "as_json",
    is_flag=True,
    default=False,
    help="Emit a structured JSON report instead of human prose.",
)
def twin(
    parent: str,
    twin_name: str | None,
    task: str | None,
    persist: bool,
    ttl: str | None,
    role: str | None,
    caller: str | None,
    as_json: bool,
) -> None:
    """Spawn a context-inheriting TWIN of PARENT.

    The twin inherits PARENT's live conversation at birth (a fork of its
    session) and then diverges. PARENT is never touched. Repo / workdir /
    image / binds / model are inherited verbatim; the twin gets its own
    name, a fresh a2a port, ``session: continue`` (seeded from the parent at
    first boot), and the identity-split env (``SCITEX_TODO_AGENT_ID`` = twin,
    ``SAC_TWIN_PARENT`` = parent).

    \b
    Examples:
      # ephemeral triage twin, inherits context, auto-stops in 30m
      sac agents twin neurovista --task "audit the failing figures" --ttl 30m

      # persistent writer companion sitting beside the parent
      sac agents twin neurovista --name neurovista-writer --persist \\
          --task "draft the results section"

    Identity contract (enforced by the boot-kick + the twin skill): the
    twin AUTHORS scitex-todo writes under its own name, but card OWNERSHIP
    stays with PARENT — the twin passes assignee=$SAC_TWIN_PARENT on every
    card write. scitex-todo cannot default owner=parent from env, so this
    is a hard rule, not an env guarantee.
    """
    from ..._lifecycle._twin import TwinSeedError, prepare_twin_spawn

    def _fail(msg: str, code: int = 2) -> None:
        if as_json:
            click.echo(json.dumps({"status": "error", "reason": msg}))
        else:
            click.echo(f"Error: {msg}", err=True)
        sys.exit(code)

    if persist and ttl:
        _fail("--persist and --ttl are mutually exclusive (a persistent twin "
              "has no TTL).")
    ttl_seconds = _parse_ttl(ttl) if ttl else None

    # Resolve parent spec + twin name and derive the inline twin doc (the
    # shared front-half reused by the agent_twin MCP tool). Fail loud on an
    # unknown parent or a taken explicit --name.
    try:
        resolved_name, doc = prepare_twin_spawn(
            parent, twin_name=twin_name, task=task, persist=persist, role=role
        )
    except TwinSeedError as exc:
        _fail(str(exc))

    # POST to the host listen (brokers on both host + in-container paths).
    import os

    from ..._lifecycle._spawn_client import SpawnRequestError, request_spawn

    base_url = (os.environ.get("SAC_LISTEN_BASE_URL", "") or "").strip() or None
    if base_url is None:
        # Bare-host invocation: env not set — fall back to the canonical
        # host listen URL so the operator need not export it.
        from ..._listen._config import listen_base_url

        base_url = listen_base_url()

    try:
        result = request_spawn(
            resolved_name,
            spec=doc,
            caller=caller,
            base_url=base_url,
            assume_yes=True,
        )
    except SpawnRequestError as exc:
        _fail(f"spawn of twin {resolved_name!r} failed: {exc}", code=1)

    rc = result.get("returncode") if isinstance(result, dict) else None
    ttl_note = ""
    if rc == 0 and ttl_seconds is not None:
        ttl_note = _schedule_ttl_stop(resolved_name, ttl_seconds)

    if as_json:
        click.echo(json.dumps({
            "status": "ok" if rc == 0 else "error",
            "twin": resolved_name,
            "parent": parent,
            "persist": persist,
            "ttl_seconds": ttl_seconds,
            "returncode": rc,
            "ttl_note": ttl_note,
            "result": result,
        }, ensure_ascii=False))
    else:
        if rc == 0:
            lifetime = "persistent" if persist else "ephemeral"
            console.print(
                f"[green]spawned twin[/green] {resolved_name} "
                f"({lifetime}, inheriting {parent}'s session)"
            )
            if ttl_note:
                console.print(f"  {ttl_note}")
            console.print(
                f"  identity: writes attributed to {resolved_name}; "
                f"cards must stay owned by {parent} (assignee=$SAC_TWIN_PARENT)."
            )
        else:
            click.echo(
                f"Error: host accepted the spawn of {resolved_name!r} but "
                f"`sac agents start` returned rc={rc}. "
                f"stderr: {(result.get('stderr') or '').strip()[:500]}",
                err=True,
            )
            sys.exit(1)


__all__ = ["twin"]
