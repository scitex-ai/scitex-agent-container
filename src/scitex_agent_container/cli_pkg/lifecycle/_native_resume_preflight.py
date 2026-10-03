"""Validate a selected native thread in its own retained Codex home."""

from __future__ import annotations

from pathlib import Path
from uuid import UUID

import click

from ...runtimes._codex_activity import CodexActivityError
from ...runtimes._codex_activity_binding import _cli_root_header
from ._resume_preflight import ResumePreflightError


def preflight_native_resume_id(
    config,
    resume_id: str,
    *,
    is_remote: bool = False,
    native_home: Path | None = None,
) -> str | None:
    """Refuse a missing, ambiguous, child or malformed native resume before stop.

    Only session identity headers are read. Claude projects and other agents'
    homes cannot supply a native thread. The exact selected home resolver is
    shared with the native launch, including its explicit home override.
    """
    if is_remote:
        click.echo(
            f"[--resume] native thread {resume_id!r} must be verified on "
            f"the owning host for {config.name!r}.",
            err=True,
        )
        return None
    try:
        if str(UUID(resume_id)) != resume_id:
            raise ValueError("noncanonical UUID")
    except (ValueError, TypeError, AttributeError) as exc:
        raise ResumePreflightError(
            "native --resume requires an exact thread UUID"
        ) from exc
    if native_home is None:
        from ...runtimes._apptainer_codex_env import resolve_codex_home
        from ...runtimes.tui_session import state_dir_for_config

        native_home = resolve_codex_home(state_dir_for_config(config))
    root = native_home.resolve()
    candidates = []
    for directory in (root / "sessions", root / "archived_sessions"):
        for path in directory.rglob(f"rollout-*-{resume_id}.jsonl"):
            if path.is_symlink() or not path.resolve().is_relative_to(root):
                raise ResumePreflightError(
                    "native resume source escapes its selected home"
                )
            candidates.append(path)
            if len(candidates) > 1:
                raise ResumePreflightError(
                    "native resume has multiple retained source files"
                )
    if not candidates:
        raise ResumePreflightError(
            f"agent {config.name!r}: exact native thread {resume_id} is absent "
            "from its selected Codex home; recover its retained checkpoint before launch"
        )
    try:
        header = _cli_root_header(candidates[0])
    except (OSError, CodexActivityError) as exc:
        raise ResumePreflightError(
            "native resume identity header is unavailable or malformed"
        ) from exc
    if header is None or header[0] != resume_id:
        raise ResumePreflightError(
            "native resume source is not the exact CLI root thread"
        )
    return resume_id
