"""Propagate a ``--on <peer> agents start`` into the lead-side registry.

The global ``--on <peer>`` flag (handled in ``_main.cli_entry_point`` ->
``host_group.dispatch_remote``) runs ``sac <argv>`` verbatim on ``peer``
over ssh and returns the remote exit code. That is correct for read-only
verbs (``agents list``, ``host probe``), but for ``agents start`` it left
a GAP: the started instance was recorded ONLY in the REMOTE peer's local
``state.db`` and never propagated back to the dispatching (lead) host's
cross-host ``instances`` table.

The incident this closes (issue #192, 2026-05-24): clew was restarted via
``sac --on spartan-bm001 agents start`` after a reservation moved it to a
new node. The new bm001 instance was invisible to the lead — its
cross-host registry still resolved clew against the OLD lapsed reservation
node with ``remote=False`` (the silent-local default), an unbreakable
wrong state.

The fix mirrors the spec-host-driven path in
``cli_pkg/lifecycle/_dispatch.py::_dispatch_remote_start``: when the ``--on``
target is an ``agents start``, run it remotely with ``--json
--no-redispatch`` appended, parse the JSON the remote start emits, and
write a lead-side ``record_instance_start`` row capturing the ACTUAL
resolved runtime state — the override host (``--on <peer>``), the bound
port the remote allocator claimed, ``remote=True``, and the lineage edge.

The registry is thus populated from actually-used data (the override host
the operator typed), and spec-vs-actual divergence is captured rather than
silently lost.
"""

from __future__ import annotations

import json

import click

__all__ = [
    "is_agents_start_argv",
    "parse_started_agent_name",
    "propagate_remote_start",
]


# Verb tokens that name the start command. ``sac agents start`` is the
# canonical form; the legacy ``agent`` (singular) alias is also accepted
# so an operator who typed the older spelling still propagates.
_AGENTS_NOUNS = ("agents", "agent")
_START_VERB = "start"

# Read-only flags that take a value we must skip when scanning for the
# positional agent name. Mirrors the value-taking options on the
# ``agents start`` click command.
_VALUE_FLAGS = {
    "--resume",
    "--session",
    "--params-file",
    "--params-out",
}


def is_agents_start_argv(argv: list[str]) -> bool:
    """Return True iff ``argv`` is an ``agents start`` invocation.

    Matches ``["agents", "start", ...]`` or the legacy singular
    ``["agent", "start", ...]``. Anything else (``agents list``,
    ``host probe``, ``db query`` ...) returns False so the caller keeps
    the verbatim pass-through behaviour for non-start verbs.
    """
    return len(argv) >= 2 and argv[0] in _AGENTS_NOUNS and argv[1] == _START_VERB


def parse_started_agent_name(argv: list[str]) -> str | None:
    """Return the first positional agent name in an ``agents start`` argv.

    Skips the ``agents start`` tokens and any leading value-taking flags
    (``--resume <id>``, ``--session <mode>``, ...) plus their values, and
    plain flags (``--force``, ``--json``). The first bare token that is
    not a flag (and not a flag value) is the agent name/path.

    Returns None when no positional is present (e.g. a malformed
    ``agents start --force`` with no target) — the caller then skips
    propagation and falls back to the plain pass-through, so a missing
    name never crashes the dispatch.
    """
    rest = argv[2:]  # drop "agents"/"agent" + "start"
    i = 0
    while i < len(rest):
        tok = rest[i]
        if tok.startswith("--") and "=" in tok:
            # ``--resume=<id>`` style — consumes no separate value token.
            i += 1
            continue
        if tok in _VALUE_FLAGS:
            i += 2  # skip the flag AND its value
            continue
        if tok.startswith("-"):
            i += 1  # plain flag (no value)
            continue
        return tok  # first bare positional = agent name/path
    return None


def _name_from_target(target: str) -> str:
    """Derive the agent name from a start TARGET (name or yaml path).

    ``sac agents start`` accepts either a bare name or a yaml path; the
    instances row is keyed by the agent NAME. A path's name is its parent
    directory stem (``<name>/<name>.yaml``) or the file stem; a bare name
    is returned unchanged.
    """
    from pathlib import Path

    if "/" not in target and not target.endswith((".yaml", ".yml")):
        return target
    p = Path(target)
    # ``<name>/<name>.yaml`` → parent stem; bare ``<name>.yaml`` → file stem.
    if p.suffix in (".yaml", ".yml"):
        return p.parent.name or p.stem
    return p.name


def _spawned_by() -> str:
    """Launching identity for the lineage edge (Rule B/D).

    A parent AGENT shelling out carries ``SAC_NAME`` in its env; a bare
    lead / operator dispatch records ``"cli"``.
    """
    from .._env import getenv

    return getenv("NAME") or "cli"


def propagate_remote_start(
    peer: str,
    argv: list[str],
    *,
    runner=None,
    ssh_argv0: str = "sac",
) -> int:
    """Run ``sac agents start ...`` on ``peer`` and record a lead-side row.

    Re-invokes the remote ``agents start`` with ``--json --no-redispatch``
    appended (idempotent — appending again is harmless if already
    present), parses the JSON the remote start emits, and writes a
    lead-side ``record_instance_start`` row with the ACTUAL override host
    (``peer``), the remote-resolved ``bound_port``, ``remote=True``, and
    the lineage edge.

    The recorded host is the override host the operator typed via
    ``--on``, captured EXACTLY rather than re-derived from the spec — the
    crux of the #192 fix.

    Args:
        peer: The ``--on`` target peer (must already be a known peer; the
            caller validates membership before calling).
        argv: The ``agents start ...`` argv (sans ``--on <peer>``).
        runner: subprocess-style callable seam
            ``runner(ssh_argv) -> CompletedProcess``. Defaults to a real
            ssh round-trip via ``build_ssh_argv`` + ``subprocess.run``.
        ssh_argv0: Remote program name (``sac`` by convention).

    Returns:
        The remote exit code. On a non-zero remote start, no row is
        written (there is no live instance to record) and the remote rc
        is returned so the operator sees the failure.

    Raises:
        RuntimeError: When the remote start succeeded (rc 0) but its
            stdout is not parseable JSON — a LOUD failure rather than a
            silently-unrecorded start, because a started-but-unrecorded
            agent is exactly the invisible state #192 was about.
    """
    name = parse_started_agent_name(argv)
    if name is None:
        # No positional target — nothing to record. Fall back to the
        # plain verbatim pass-through so the remote still runs (and emits
        # its own usage error if the argv is malformed).
        from .host_group import dispatch_remote

        return dispatch_remote(peer, argv, ssh_argv0=ssh_argv0)

    # Build the remote argv with --json --no-redispatch so we can parse
    # the started state and the remote doesn't try to re-dispatch.
    remote_argv = list(argv)
    if "--json" not in remote_argv:
        remote_argv.append("--json")
    if "--no-redispatch" not in remote_argv:
        remote_argv.append("--no-redispatch")

    if runner is None:
        runner = _default_ssh_runner
    result = runner(peer, [ssh_argv0, *remote_argv])
    if result.returncode != 0:
        # Remote start failed — no live instance to record. Surface the
        # remote rc; the operator sees stdout/stderr from the inherited
        # streams (real runner) or from the result object.
        return result.returncode

    stdout = getattr(result, "stdout", None) or ""
    try:
        peer_state = json.loads(stdout)
    except (json.JSONDecodeError, TypeError) as exc:
        raise RuntimeError(
            f"`sac --on {peer} agents start {name}` succeeded but returned "
            f"non-JSON stdout; cannot record the lead-side instances row "
            f"(the started agent would be invisible to the lead — the "
            f"exact #192 failure). Re-run with --json to inspect.\n"
            f"stdout (first 500 chars):\n{stdout[:500]}\n"
            f"json error: {exc}"
        ) from exc

    # A skipped / dry-run start has no live instance to record.
    status = peer_state.get("status")
    if status not in ("started",):
        return 0

    agent_name = _name_from_target(name)
    bound = peer_state.get("a2a_port")
    from .._state.state_db import record_instance_start

    record_instance_start(
        name=agent_name,
        host=peer,
        a2a_port=bound,
        bound_port=bound,
        remote=True,
        spawned_by=_spawned_by(),
        workdir=peer_state.get("host_workdir"),
    )
    click.echo(
        f"[--on] {agent_name!r} started on {peer!r} "
        f"(a2a_port={bound!s}, started_at={peer_state.get('started_at')!s}); "
        f"recorded lead-side instances row (remote=True, host={peer!r})."
    )
    return 0


def _default_ssh_runner(peer: str, full_argv: list[str]):
    """Real ssh round-trip, capturing stdout so the JSON can be parsed.

    Returns a ``subprocess.CompletedProcess`` (text mode). The remote
    ``sac agents start --json`` writes its report to stdout; stderr is
    inherited so the operator still sees progress / errors live.
    """
    import subprocess

    from .._state.host_config import build_ssh_argv
    from .._state.host_config import load as _load

    cfg = _load()
    ssh_argv = build_ssh_argv(peer, full_argv, cfg.peers)
    return subprocess.run(ssh_argv, capture_output=True, text=True, check=False)


