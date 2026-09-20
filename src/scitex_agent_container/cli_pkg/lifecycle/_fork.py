"""Create a lead-owned child with an independent runtime and conversation."""

import json
import os

import click


@click.command("fork")
@click.argument("parent")
@click.option("--name", required=True, help="New child agent name.")
@click.option("--task", required=True, help="Bounded assignment for the child.")
@click.option("--fresh", is_flag=True, help="Copy the spec without conversation history.")
@click.option("--no-start", is_flag=True, help="Prepare and register the child without launching it.")
@click.option("--json", "as_json", is_flag=True, help="Return a machine-readable result.")
def fork(parent, name, task, fresh, no_start, as_json):
    """Fork PARENT into a task-scoped child; the parent continues running.

    Hermes conversation history is copied consistently into a private store.
    Use --fresh for a clean session with any harness. The child has its own
    overlay, bot-free communication, author identity, port, and parent lineage.
    Files in the project workdir remain shared; use separate git worktrees for
    concurrent code changes.
    """
    from ..._lifecycle._in_sif_broker import is_in_sif

    if is_in_sif():
        from ..._lifecycle._host_exec_client import request_host_exec

        argv = ["sac", "agents", "fork", parent, "--name", name, "--task", task]
        argv += [flag for flag, enabled in (("--fresh", fresh), ("--no-start", no_start), ("--json", as_json)) if enabled]
        result = request_host_exec(argv, timeout_s=300)
        click.echo(result.get("stdout", ""), nl=False)
        if result.get("stderr"):
            click.echo(result["stderr"], err=True, nl=False)
        raise click.exceptions.Exit(int(result.get("returncode", 1)))

    try:
        from ._attach import _classify_agent_host
        from ..._listen._acl import check_lineage_acl, check_spawn
        from ..._lifecycle._fork import prepare_fork
        from ..._state.state_store_nodes import record_lineage

        kind, host = _classify_agent_host(parent)
        if kind != "local":
            raise ValueError(f"run this fork on parent host {host!r}")
        caller = os.environ.get("SAC_NAME") or None
        decision, reason = check_lineage_acl(caller=caller, target=parent)
        if decision != "allow":
            raise ValueError(str(reason))
        decision, reason = check_spawn(caller=parent)
        if decision != "allow":
            raise ValueError(str(reason))
        path = prepare_fork(parent, name, task, fresh=fresh)
        record_lineage(child=name, parent=parent)
        result = {"name": name, "parent": parent, "spec": str(path),
                  "inherited_context": not fresh, "started": False}
        if not no_start:
            from ..._lifecycle._spawn_client import request_spawn
            from ..._listen._config import listen_base_url

            launch = request_spawn(name, caller=parent, base_url=listen_base_url(), assume_yes=True)
            result["launch"] = launch
            result["started"] = launch.get("returncode") == 0
            result["pending"] = launch.get("status") == "accepted"
            if not result["started"] and not result["pending"]:
                raise ValueError(f"fork prepared at {path}, but launch did not report success: {launch}")
        message = f"Forked {parent} → {name}: {path}"
        if result.get("pending"):
            message += f"\nLaunch accepted; still starting. Poll {result['launch']['poll']} before retrying."
        click.echo(json.dumps(result) if as_json else message)
    except Exception as exc:
        if as_json:
            click.echo(json.dumps({"name": name, "error": str(exc)}))
            raise click.exceptions.Exit(1) from exc
        raise click.ClickException(str(exc)) from exc
