"""``sac agent send`` — deliver one more turn to an agent's live session.

Delivery is ALWAYS over HTTP to the running agent: the host listen proxy
when this CLI is itself inside a SIF, the peer's ``/v1/turn`` when the
agent's active row lives on another host, else the agent's loopback
``/v1/turn``. There is no fourth path.

In particular there is no host-side ``claude --resume`` shellout. That
fallback existed for a "non-A2A, host-side runtime" which no longer
exists — apptainer is the only container engine
(:mod:`config._container_engine`) — and it was doubly wrong: it ran a
full Claude agent turn OUTSIDE the container holding the host's own
credentials, against a session that lives inside the container's
``~/.claude/projects/`` store and is therefore invisible to it. When no
A2A port is recorded the command now refuses and says so.

For programmatic callers that need a structured ``{status,
response_text, response_metadata}`` payload (e.g. the MCP
``agent_send`` tool), use
:func:`scitex_agent_container.cli_pkg._send.send_to_agent` — the
library-facing sibling helper that returns a dict instead of writing
to stdout.
"""

from __future__ import annotations

import os
from typing import NoReturn

import click

from .._runners._session_state import state_dir_for
from ..config._container_engine import CONTAINER_ENGINE
from ._helpers import agent_name_complete


class _RemoteA2APortMissingError(click.ClickException):
    """The remote agent's state.db row has no a2a_port recorded.

    Raised by :func:`_try_dispatch_remote_send` when an agent is active
    on a peer but never registered an A2A port (e.g. ``runtime`` doesn't
    expose ``/v1/turn``, or the runner hasn't claimed a port yet). The
    typed exception makes the failure mode explicit so callers can
    distinguish it from a generic network error and so tests can pin
    on the class rather than a message substring.
    """


def _send_via_host_listen(
    *,
    name: str,
    prompt: str,
    model: str | None,
    max_turns: int | None,
) -> None:
    """In-SIF send proxy — POST /agents/<name>/send → outcome JSON.

    PR-3 Checkpoint 3 — the path the ``sac agents send <name>
    <prompt>`` CLI takes when running inside an apptainer SIF. The
    ``--key`` (SIGINT) path is excluded by the call site because
    it needs local pid access; prompts route through the host
    listen so the running agent's in-process SDK session handles
    the turn end-to-end.

    The host's lineage-scoped ACL gate denies cross-lineage sends
    with ``kind=acl_deny`` + exit 5; other failures map per the
    standard outcome table.
    """
    import sys as _sys

    from .._lifecycle._in_sif_http_client import (
        HostListenTransportError,
        host_listen_call,
    )
    from .._lifecycle._in_sif_outcome import (
        build_outcome,
        outcome_to_stdout_json,
        transport_outcome,
    )

    body: dict = {"prompt": prompt}
    if model is not None:
        body["model"] = model
    if max_turns is not None:
        body["max_turns"] = max_turns
    try:
        status, resp = host_listen_call("POST", f"/agents/{name}/send", body=body)
        outcome = build_outcome(http_status=status, body=resp)
    except HostListenTransportError as exc:
        outcome = transport_outcome(str(exc), url=exc.url)
    _sys.stdout.write(outcome_to_stdout_json(outcome))
    _sys.exit(outcome.exit_code)


def _try_dispatch_remote_send(name: str, prompt: str) -> bool:
    """POST a turn to ``name`` on a remote peer via /v1/turn.

    Looks up the agent's active row in ``state.db.instances``; when
    ``host != current_host``, builds ``ssh://<host>:<a2a_port>/v1/turn``
    and lets :func:`post_turn_to_url` dispatch it through the ssh
    control plane (the same path ``sac peer post-turn`` already uses
    for ssh-as-transport).

    Returns:
        * ``True`` when the dispatch happened (reply printed to stdout).
        * ``False`` when no remote row exists (caller proceeds local).

    Raises:
        _RemoteA2APortMissingError: When the row has no ``a2a_port``
            recorded. This is a sharp failure surface — without a port
            we cannot reach the peer's /v1/turn, and silently falling
            back to local would mis-target the prompt (run claude with
            no session on the lead).
        click.ClickException: When the underlying HTTP / ssh call
            fails. We wrap the PeerError so the user sees the same
            error shape they get from ``sac peer post-turn``.
    """
    from .._network.peer import PeerError, post_turn_to_url
    from .lifecycle._dispatch import lookup_remote_peer

    found = lookup_remote_peer(name)
    if found is None:
        return False
    peer, row = found
    a2a_port = row.get("a2a_port")
    if not isinstance(a2a_port, int) or a2a_port <= 0:
        raise _RemoteA2APortMissingError(
            f"agent {name!r} is active on peer {peer!r} but state.db "
            f"records no a2a_port for it (a2a_port={a2a_port!r}). The "
            f"remote agent did not register an A2A port; cannot send. "
            f"Restart the agent on the peer with spec.a2a.port set, or "
            f"shell into the peer and run `sac agent send {name} ...` "
            f"directly."
        )
    url = f"ssh://{peer}:{a2a_port}/v1/turn"
    click.echo(f"# send {name}: POST {url}", err=True)
    try:
        reply = post_turn_to_url(url, prompt)
    except PeerError as exc:
        raise click.ClickException(f"remote send failed: {exc}") from exc
    click.echo(reply)
    return True


def _try_dispatch_local_send(name: str, prompt: str) -> bool:
    """POST a turn to a LOCAL agent via its loopback /v1/turn endpoint.

    Symmetric to :func:`_try_dispatch_remote_send`: when ``name`` has an
    active ``state.db.instances`` row on the *current* host with a bound
    ``a2a_port``, build ``http://127.0.0.1:<port>/v1/turn`` and POST one
    turn so the running SDK runner re-uses its in-process Claude session.

    This is the fix for the local mis-target diagnosed 2026-05-22: an
    apptainer agent's SDK session lives inside the container's
    ``~/.claude/projects/`` store, NOT on the host, so a host-side
    ``claude --resume <sid>`` cannot see it and exits 1 with "No
    conversation found". The HTTP path reaches the live in-container
    session instead.

    Returns:
        * ``True`` when the dispatch happened (reply printed to stdout).
        * ``False`` when there is no active local row, or the row exists
          but records no ``a2a_port`` (a non-A2A runtime) — the caller
          then falls through to the ``claude --resume`` host shellout,
          which only makes sense for a non-containerized host-side agent.

    Raises:
        click.ClickException: When the underlying HTTP call fails. We
            wrap the PeerError so the user sees the same error shape they
            get from ``sac peer post-turn``.
    """
    from .._network.peer import PeerError, post_turn_to_url
    from .._state.state_db import _resolve_host, list_active_instances

    current_host = _resolve_host(None)
    rows = list_active_instances()
    matching = [
        r
        for r in rows
        if r.get("name") == name and str(r.get("host") or "") == current_host
    ]
    if not matching:
        return False
    a2a_port = matching[0].get("a2a_port")
    if not isinstance(a2a_port, int) or a2a_port <= 0:
        # No bound A2A port: this is a non-A2A runtime. Let the caller
        # fall through to the host-side claude --resume path.
        return False
    url = f"http://127.0.0.1:{a2a_port}/v1/turn"
    click.echo(f"# send {name}: POST {url}", err=True)
    try:
        reply = post_turn_to_url(url, prompt)
    except PeerError as exc:
        raise click.ClickException(f"local send failed: {exc}") from exc
    click.echo(reply)
    return True


def _refuse_uncontained_send(name: str) -> NoReturn:
    """Refuse the turn rather than run it outside the container.

    Reached when every HTTP delivery path declined, which means no A2A
    port is recorded for ``name``. The code this replaces reacted to
    that by shelling out to ``claude --resume <sid> -p <prompt>`` in the
    agent's workdir — an entire Claude agent TURN on the bare host, with
    the host operator's ``~/.claude`` credentials, for an agent whose
    whole point is that it is contained.

    It could not have worked even setting containment aside: an
    apptainer agent's session lives in the CONTAINER's
    ``~/.claude/projects/`` store, so a host-side resume either finds
    nothing or resumes some unrelated host session — which is worse than
    an error, because it looks like it worked.

    THIS MESSAGE IS FOR A DEFINED AGENT ONLY. A name that was never an
    agent reaches the same dead end, and until 2026-08-20 it got this
    same text — which describes the other cause in vocabulary that
    presupposes the agent EXISTS ("the agent's live session", "(re)start
    the agent"). ``_refuse_unknown_agent`` handles that case; the caller
    decides which applies.
    """
    raise click.ClickException(
        f"agent {name!r}: no A2A port is recorded, so this turn has no "
        f"way to reach the agent's live session.\n"
        f"  There is deliberately NO host-side fallback: every sac agent "
        f"runs inside {CONTAINER_ENGINE}, and its Claude session lives in "
        f"the container's ~/.claude/projects/ store. A host-side "
        f"`claude --resume` would run OUTSIDE the container against a "
        f"session it cannot see.\n"
        f"  Fix: (re)start the agent so its A2A sidecar binds a port — "
        f"`sac agents start {name} -y` — then re-send. Verify with "
        f"`sac agents list {name} --json`: a2a_port must be non-null.\n"
        f"  State dir: {state_dir_for(name)}"
    )


def _is_known_agent(name: str) -> bool:
    """Whether ``name`` is an agent AT ALL — a spec on disk, or any row.

    DELIBERATELY BROADER than "running" and broader than "reachable". A
    stopped agent, an agent whose row is tombstoned, and one recorded
    with ``a2a_port=None`` are all KNOWN — they exist and the operator
    can act on them, so the port-oriented refusal is the right advice.
    Only a name with no spec in the agents/ cascade AND no ``instances``
    row it has ever had is unknown.

    The two halves are both needed and neither implies the other: an
    agent defined but never started has a spec and no row, and a row can
    outlive (or arrive without) a locally visible spec — a cross-host
    agent's row is written into this host's store by dispatch/sync while
    its spec lives on the peer. Checking only one half would call a real
    agent nonexistent, which is the same class of wrong answer this
    whole change exists to remove.
    """
    from ..config._resolve import enumerate_agent_names

    # `enumerate_agent_names` and NOT `_discover_defined_agents`: the
    # latter hardcodes its roots (project scope + ~/.scitex/.../agents)
    # and does not honour SCITEX_AGENT_CONTAINER_YAML_DIRS, so an agent
    # reachable through the operator-env cascade would be reported
    # NONEXISTENT — the precise wrong answer this function exists to
    # stop. It only stats `<dir>/<name>/spec.{yaml,yml}`, so the refusal
    # path still parses no YAML on its way to refusing.
    #
    # It can also RAISE (AmbiguousRegistryScope: both the project-local
    # and fleet registries exist and $SAC_AGENT_SCOPE is unset). Treat
    # that as KNOWN, not as unknown. "I could not determine whether this
    # agent exists" is the third value, and collapsing it into "it does
    # not exist" would make this function assert the very kind of
    # unestablished cause it was written to remove — and would turn a
    # clean refusal into an unrelated crash on any host with both
    # registries. Erring toward the port-oriented message keeps the
    # pre-existing behaviour whenever existence is undecidable.
    try:
        if name in enumerate_agent_names():
            return True
    except Exception:  # stx-allow: fallback (reason: see comment above)
        return True
    # Through the OWNING module, not through its table. This used to open a
    # raw connection and SELECT from ``instances`` directly, which reads the
    # same rows today and strands the moment that table moves backend — the
    # sqlite->PostgreSQL migration is doing exactly that, and the identical
    # pattern in ``_authheal/_specimen`` would have silently returned "no such
    # row" forever. ``last_known_instance`` already answers this question
    # (latest row for the name, active OR ended, ``None`` when never seen),
    # so nothing new had to be written — the accessor was there and this call
    # site was simply going around it.
    from .._state.state_db_instances import last_known_instance

    return last_known_instance(name) is not None


def _refuse_unknown_agent(name: str) -> NoReturn:
    """Refuse a send to a name that was never an agent.

    Delivery declines identically whether an agent is defined-but-
    unreachable or was never defined at all, so both land at the same
    dead end. Until 2026-08-20 both also got the SAME message —
    ``_refuse_uncontained_send``'s — which says "no A2A port is
    recorded" and prescribes ``sac agents start <name>``. Every word of
    that presupposes the agent exists.

    MEASURED: ``ci-watch`` dispatched to five names
    (``proj-scitex-{stats,str,types,dict,datetime}``) 351 times, each
    refused here, 0 successes. A peer read the text, took the named
    cause at face value, and reported the failures as instances of an
    unrelated port-registration defect then under active investigation.
    The state DB showed those five with ``definitions=0`` and
    ``instances=0`` EVER — not stopped, not tombstoned, never
    registered. The remedy was trusted precisely because it was
    specific, and it pointed at the wrong thing.

    A refusal must not name a cause it has not established. Where two
    states share one symptom, the message says which one was observed.
    """
    raise click.ClickException(
        f"agent {name!r}: NOT DEFINED — no spec.yaml for this name under "
        f"any agents/ tree (project-scope .scitex/agent-container/agents/ "
        f"or ~/.scitex/agent-container/agents/).\n"
        f"  This is NOT a missing-port problem and NOT a stopped agent: "
        f"there is nothing to (re)start, because this name has never been "
        f"an agent. Starting it is not the fix and will fail the same way.\n"
        f"  Fix: check the name for a typo (`sac agents list`), or define "
        f"the agent first (`sac agents create {name}`), then start it, "
        f"then re-send.\n"
        f"  If a SCHEDULED JOB is sending here, it is dispatching to a name "
        f"that does not exist — fix the job's target list rather than this "
        f"agent.\n"
        f"  (There is still deliberately NO host-side fallback: every sac "
        f"agent runs inside {CONTAINER_ENGINE}, so sac never runs the turn "
        f"on the bare host. That guarantee holds for both causes.)"
    )


@click.command(name="send")
@click.argument("name", shell_complete=agent_name_complete)
@click.argument("prompt", required=False)
@click.option(
    "--model",
    default=None,
    help="Override the model for this turn only (e.g. ``opus``, ``sonnet``).",
)
@click.option(
    "--max-turns",
    type=int,
    default=None,
    help="Cap autonomous turns within this send. Default: claude's own default.",
)
@click.option(
    "--key",
    default=None,
    help=(
        "Send a control key instead of a prompt (tmux-style, e.g. ``ESC``, "
        "``C-c``). Mutually exclusive with PROMPT."
    ),
)
# ``--no-stream`` and the trailing ``-- <forward>`` escape hatch were
# REMOVED with the host-side shellout: both existed only to shape a
# ``claude`` argv this command no longer builds. Keeping flags that
# reach nothing is the same doubt the container-engine choice was
# abolished for — an option a reader cannot tell does anything.
def send(
    name: str,
    prompt: str | None,
    model: str | None,
    max_turns: int | None,
    key: str | None,
) -> None:
    """Send a follow-up PROMPT (or control key) to an agent's live session.

    \b
    Examples:
      sac agent send coverage-runner "now bump the threshold to 95%"
      sac agent send coverage-runner --key ESC
      sac agent send coverage-runner --model opus --max-turns 3 "..."

    Delivery is always HTTP to the running (containerized) agent. If no
    A2A port is recorded the command refuses — it will not run the turn
    on the bare host.
    """
    if key and prompt:
        raise click.UsageError("--key is mutually exclusive with PROMPT.")
    if not key and not prompt:
        raise click.UsageError("Either PROMPT or --key is required.")

    # PR-3 — in-SIF auto-fallback. When inside an apptainer SIF and
    # sending a PROMPT (the ``--key`` SIGINT path needs local pid
    # access and is excluded), auto-proxy to ``POST /agents/<name>/send``
    # on the host listen. The host's existing lineage-scoped ACL gate
    # (already wired into node_message_send + the per-agent send
    # surface) enforces caller permission. Outcome JSON + exit code
    # follow the same Checkpoint 2 contract as the other in-SIF verbs.
    from .._lifecycle._in_sif_broker import is_in_sif

    if is_in_sif() and prompt and not key:
        _send_via_host_listen(
            name=name,
            prompt=prompt,
            model=model,
            max_turns=max_turns,
        )
        return  # noreturn — _send_via_host_listen sys.exits
    if key:
        # ESC / C-c → SIGINT to the runner pid. Other keys are reserved
        # for a future tty-bridge implementation.
        if key not in ("ESC", "C-c", "SIGINT"):
            raise click.UsageError(
                f"--key {key!r} not supported. Only ESC / C-c / SIGINT are "
                "wired (cancel current turn). Use a prompt otherwise."
            )
        import signal as _signal

        state_dir = state_dir_for(name)
        pid_file = state_dir / "pid"
        if not pid_file.is_file():
            raise click.ClickException(
                f"No pid file at {pid_file} — agent {name!r} not running."
            )
        try:
            pid = int(pid_file.read_text().strip())
            os.kill(pid, _signal.SIGINT)
        except (OSError, ValueError) as exc:
            raise click.ClickException(str(exc)) from exc
        click.echo(f"# interrupt {name}: SIGINT → pid={pid}", err=True)
        return

    # Cross-host: when the agent's active state.db.instances row lives
    # on a peer, POST one turn to the peer's /v1/turn endpoint over the
    # ssh control plane and short-circuit before the local resume path.
    # Architectural choice: prompt-style sends go through A2A (HTTP) so
    # the running runner re-uses its in-process Claude session, not via
    # a separate `claude --resume` shellout that would race the runner.
    if _try_dispatch_remote_send(name, prompt):
        return

    # Local A2A: when the agent is running on THIS host with a bound
    # a2a_port, POST one turn to its loopback /v1/turn so the live
    # in-container SDK session handles it. A host-side `claude --resume`
    # cannot see a containerized agent's session (it lives inside the
    # container's ~/.claude/projects/ store), so the HTTP path is the
    # only correct local delivery — and since apptainer is the only
    # container engine, it is the only local delivery there is.
    if _try_dispatch_local_send(name, prompt):
        return

    # Every delivery path declined. Refuse — do NOT run the turn on the
    # bare host. See _refuse_uncontained_send for what used to happen here.
    #
    # Two different states land here and they need different remedies, so
    # establish WHICH before naming a cause: an agent that exists but has
    # no reachable port, versus a name that was never an agent at all.
    # Telling the second to `sac agents start` is advice that cannot work.
    if not _is_known_agent(name):
        _refuse_unknown_agent(name)
    _refuse_uncontained_send(name)
