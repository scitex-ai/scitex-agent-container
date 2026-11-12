"""Discoverable namespace for configured runtime hook protocols."""

import click

from .never_stop_when_task_remains_cmds import never_stop_when_task_remains


@click.group(name="hooks")
def hooks() -> None:
    """Handle configured harness events; these can update hook state.

    Example (owned harness event on stdin):
      $ sac dev hooks decide-stop --agent example-agent < stop-event.json
    """


# A second protocol entry point, not a redirect: old deployed hooks must
# retain their exit-zero JSON semantics until their settings are migrated.
hooks.add_command(
    click.Command(
        "decide-stop",
        callback=never_stop_when_task_remains.callback,
        params=list(never_stop_when_task_remains.params),
        help=never_stop_when_task_remains.help,
    )
)
