"""JSON options for job status, admitted by the owning Dev command metadata."""

from __future__ import annotations

import click

from ._dev_jobs_capability import leaf_command


def add_status_json_option(command: click.Command) -> None:
    """Expose JSON on status only; lifecycle mutations retain their grammar."""
    if command.name == "status":
        command.params.append(
            click.Option(
                ["--json", "as_json"],
                is_flag=True,
                help="Forward structured status output from scitex-dev.",
            )
        )


def json_arguments(
    path: tuple[str, ...], verb: str, *, leaf: click.Command | None = None
) -> list[str]:
    """Forward JSON only when the installed owner declares that option.

    An older backend must fail explicitly rather than return text under an
    advertised JSON interface. This does not generate or reinterpret status.
    """
    target = leaf if leaf is not None else leaf_command(path, verb)
    if target is None or not any(
        "--json" in getattr(parameter, "opts", ()) for parameter in target.params
    ):
        raise click.ClickException(
            f"installed scitex-dev ecosystem {' '.join(path)} {verb} "
            "does not declare --json; upgrade scitex-dev for structured status."
        )
    return ["--json"]


__all__ = ["add_status_json_option", "json_arguments"]
