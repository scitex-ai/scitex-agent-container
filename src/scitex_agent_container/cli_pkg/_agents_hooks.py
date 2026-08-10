"""``sac agents hooks`` — what does THIS container actually enforce?

One command, two jobs, deliberately the same code path:

* **read-only report** — an operator or an agent asks "which Claude Code hooks
  are armed here, and does this agent meet the floor its spec declares?" and
  gets the answer measured where it is consumed;
* **the boot gate** — ``runtimes._apptainer_inner_argv`` runs this exact
  command inside the container before ``exec``ing the agent runner, for specs
  that declare a floor. A non-zero exit aborts the launch.

They are the same command on purpose. A gate whose verdict cannot be reproduced
by a human running one command is a gate nobody can debug; and a report that
does not share the gate's code is a report that can disagree with it.

Output is the cross-package standard health shape (``--json``), the same one
``scitex-cards health`` emits, so the fleet has one shape to read rather than
two. Exit status:

* ``0`` — floor satisfied, floor undeclared, or floor UNKNOWN (see below)
* ``1`` — floor declared and a required hook is DEFINITELY not armed
* ``0`` with an ERROR-level bypass banner — same, but ``--allow-missing-hooks``

UNKNOWN exits 0 by design, and says so loudly. Refusing on "I could not read
the hooks directory" would ground an agent on an unreadable mount, exactly as
``_drift._local`` declines to refuse on NOT_A_REPO / UNREACHABLE. It is still
not a pass: ``required_hooks_present`` stays ``null`` in the JSON and the
summary names it under ``unknown:``.
"""

from __future__ import annotations

import json as json_mod
import os
import sys

import click

from .._claude_hooks import MissingRequiredHooks, check_required_hooks
from .._claude_hooks._gate import ALLOW_ENV, ALLOW_FLAG
from .._claude_hooks._report import hooks_health, render_hooks_text
from ._helpers import agent_name_complete

CONTEXT_SETTINGS = {"help_option_names": ["-h", "--help"]}


def resolve_self_name(explicit: "str | None") -> "str | None":
    """The agent to report on: the argument, else THIS agent's own name.

    In-container ``$SAC_NAME`` is injected by the launcher, so the boot gate
    and a bare interactive ``sac agents hooks`` both name the right agent
    without the caller repeating it.
    """
    if explicit:
        return explicit
    return (os.environ.get("SAC_NAME") or "").strip() or None


def load_spec_config(name: "str | None"):
    """Load ``name``'s AgentConfig, or ``None`` when it does not resolve here.

    Best-effort by design. An unresolvable spec is not an error for this
    command: it means the floor is unknown, which is one of the three answers
    the report is built to give. Raising instead would turn "I could not read
    the spec" into "this agent is broken".
    """
    if not name:
        return None
    # stx-allow: fallback (reason: an unresolvable/unparseable spec is a
    # REPORTED state — `required_hooks_declared` reads UNKNOWN — not a crash;
    # in-container the host registry may legitimately be unreachable.)
    try:
        from ..config import load_config, resolve_config

        return load_config(resolve_config(name))
    except Exception:  # stx-allow: fallback (reason: see above)
        return None


def _true_or_unset(ctx, param, value):  # noqa: ARG001 - click callback signature
    """``--allow-missing-hooks`` yields ``True`` when passed, ``None`` when not.

    NOT ``False``. This is the exact seam PR #949 got wrong for
    ``--strict-drift``: a click flag's natural default IS ``False``, and the
    resolver reads an explicit ``False`` as "the caller demanded leniency", so
    wiring the flag straight through would have had the flag NOBODY PASSED
    silently disable the gate on every start. ``None`` means "no instruction",
    which is what an absent flag actually means.
    """
    return True if value else None


@click.command("hooks", context_settings=CONTEXT_SETTINGS)
@click.argument("name", required=False, shell_complete=agent_name_complete)
@click.option(
    "--allow-missing-hooks",
    "allow_missing",
    is_flag=True,
    default=False,
    callback=_true_or_unset,
    help=f"Exit 0 even though a declared required hook is missing ({ALLOW_ENV}=1). "
    "The bypass is still logged at ERROR naming every missing hook.",
)
@click.option("--json", "as_json", is_flag=True, default=False, help="Output as JSON.")
@click.pass_context
def agents_hooks(
    ctx: click.Context,
    name: "str | None",
    allow_missing: "bool | None",
    as_json: bool,
) -> None:
    """Report the Claude Code hooks armed in THIS container, and the declared floor.

    NAME defaults to $SAC_NAME (this agent). Measured from
    ``$HOME/.claude/hooks`` — the EFFECTIVE set, because inside the container
    the two stacked home mounts are already resolved by the kernel. Run on the
    bare host it measures the operator's own hooks instead, and says so:
    ``measurement_site`` reports UNKNOWN rather than a confident verdict about
    the wrong directory.

    \b
    Examples:
      $ sac agents hooks                 # this agent, human-readable
      $ sac agents hooks --json          # the standard health report
      $ sac agents hooks grant           # named agent (only truthful in-container)
    """
    from ._helpers import _json_flag

    resolved = resolve_self_name(name)
    config = load_spec_config(resolved)
    report = hooks_health(config, agent_name=resolved)

    if _json_flag(ctx, as_json):
        click.echo(json_mod.dumps(report, indent=2))
    else:
        click.echo(render_hooks_text(report))

    try:
        check_required_hooks(report, allow_missing=allow_missing)
    except MissingRequiredHooks as exc:
        # The banner already went to the logger at ERROR; echo it to stderr too
        # so a bare `sac agents hooks` (no logging config) still shows WHY, and
        # so the container's boot step captures it in the pane stderr that
        # `_start_failure_diag` reads back.
        click.echo(str(exc), err=True)
        click.echo(
            f"(override with {ALLOW_FLAG}, or {ALLOW_ENV}=1 in the spec's env)",
            err=True,
        )
        sys.exit(1)


def register(agent_group) -> None:
    agent_group.add_command(agents_hooks)


__all__ = ["agents_hooks", "load_spec_config", "register", "resolve_self_name"]
