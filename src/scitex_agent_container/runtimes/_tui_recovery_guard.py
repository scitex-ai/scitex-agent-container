"""Refuse replacement when completing a partially bootstrapped live TUI."""

from pathlib import Path


def refuse_existing_helper(path: Path, service: str) -> None:
    """Any retained claim is unknown until its owner reviews it; never signal it."""
    try:
        path.lstat()
    except FileNotFoundError:
        return
    raise RuntimeError(f"bootstrap completion refuses existing {service} claim")
