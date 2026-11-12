"""Argument-only creation preview; no template, account or destination reads."""

from __future__ import annotations

import json
from pathlib import Path

import click

from ._new_dir_template import DirTemplateError, parse_set_pairs


def _show_creation_preview(
    name: str,
    *,
    template_name: str,
    project: str | None,
    agent_id: str | None,
    set_pairs: tuple[str, ...],
    base_dir: Path | None,
) -> None:
    """Render supplied arguments, redacting arbitrary placeholder values."""
    try:
        keys = sorted(parse_set_pairs(set_pairs))
    except DirTemplateError:
        # The parser's ordinary error includes the raw pair, possibly a secret.
        raise click.UsageError(
            "--set requires a non-empty KEY=VALUE; preview does not show values."
        ) from None
    spec = (
        str(base_dir / name / "spec.yaml")
        if base_dir is not None
        else f"<primary-agents-root>/{name}/spec.yaml"
    )
    click.echo(
        json.dumps(
            {
                "action": "create-agent",
                "dry_run": True,
                "scope": "arguments_only",
                "name": name,
                "template": template_name,
                "project": project,
                "agent_id": agent_id if agent_id is not None else name,
                "base_dir": str(base_dir) if base_dir is not None else None,
                "spec_path": spec,
                "set_keys": keys,
                "unverified": [
                    "default agents root",
                    "template availability and required placeholders",
                    "destination state and overwrite refusal",
                    "credential discovery and managed-home adoption",
                    "runtime readiness",
                ],
            },
            indent=2,
        )
    )
