"""Console bootstrap that protects image builds from ``PYTHONPATH`` shadowing.

This package is deliberately separate from :mod:`scitex_agent_container`.
The console script can therefore import this installed bootstrap before a
stale checkout on ``PYTHONPATH`` selects the SAC package that performs the
build.  All non-build commands pass straight through.
"""

from __future__ import annotations

import importlib.util
import json
import site
import sys
import sysconfig
from collections.abc import Callable
from importlib import metadata
from pathlib import Path
from urllib.parse import unquote, urlparse

import click

_DIST_NAME = "scitex-agent-container"


class ImageBuildSourceMismatch(RuntimeError):
    """An image build would execute SAC from a non-authoritative checkout."""


def _is_image_build(argv: list[str]) -> bool:
    """Return whether argv selects the current or retired image-build verb."""
    index = 0
    while index < len(argv):
        token = argv[index]
        if token == "--on":
            index += 2
            continue
        if token.startswith("-"):
            index += 1
            continue
        return token == "build-image" or (
            token == "image" and index + 1 < len(argv) and argv[index + 1] == "build"
        )
    return False


def _environment_metadata_paths() -> tuple[Path, ...]:
    """Return interpreter-owned metadata roots, excluding ``PYTHONPATH``."""
    candidates = {
        Path(value).resolve()
        for key, value in sysconfig.get_paths().items()
        if key in {"purelib", "platlib"} and value
    }
    candidates.update(Path(value).resolve() for value in site.getsitepackages())
    if site.ENABLE_USER_SITE:
        candidates.add(Path(site.getusersitepackages()).resolve())
    return tuple(sorted(candidates))


def _editable_authorities(paths: tuple[Path, ...]) -> set[Path]:
    """Read editable SAC roots only from interpreter-owned dist-info."""
    roots: set[Path] = set()
    for path in paths:
        for distribution in metadata.distributions(path=[str(path)]):
            name = (distribution.metadata.get("Name") or "").lower().replace("_", "-")
            if name != _DIST_NAME:
                continue
            raw = distribution.read_text("direct_url.json")
            if not raw:
                continue
            try:
                direct_url = json.loads(raw)
                parsed = urlparse(str(direct_url.get("url", "")))
                editable = bool(direct_url.get("dir_info", {}).get("editable"))
            except (AttributeError, TypeError, ValueError) as exc:
                raise ImageBuildSourceMismatch(
                    f"installed {_DIST_NAME} has malformed direct_url.json: {exc}"
                ) from exc
            if editable and parsed.scheme == "file":
                repo = Path(unquote(parsed.path)).resolve()
                roots.add((repo / "src" / "scitex_agent_container").resolve())
    return roots


def assert_image_build_source_authority(
    argv: list[str] | None = None,
    *,
    metadata_paths: tuple[Path, ...] | None = None,
    runtime_root: Path | None = None,
) -> None:
    """Fail before importing SAC when an editable authority is shadowed."""
    args = list(sys.argv[1:] if argv is None else argv)
    if not _is_image_build(args):
        return

    paths = _environment_metadata_paths() if metadata_paths is None else metadata_paths
    authorities = _editable_authorities(paths)
    if not authorities:
        return
    if len(authorities) != 1:
        rendered = "\n".join(f"  - {root}" for root in sorted(authorities))
        raise ImageBuildSourceMismatch(
            "refusing `sac image build`: the active interpreter contains "
            f"multiple editable SAC authorities:\n{rendered}"
        )

    expected = next(iter(authorities))
    if runtime_root is None:
        spec = importlib.util.find_spec("scitex_agent_container")
        origin = None if spec is None else spec.origin
        if not origin:
            raise ImageBuildSourceMismatch(
                "refusing `sac image build`: scitex_agent_container has no "
                "filesystem import origin"
            )
        observed = Path(origin).resolve().parent
    else:
        observed = runtime_root.resolve()
    if observed == expected:
        return

    raise ImageBuildSourceMismatch(
        "refusing `sac image build` before filesystem or image mutation: "
        "PYTHONPATH selected SAC source different from the editable install.\n"
        f"  runtime-loaded package root: {observed}\n"
        f"  editable direct_url authority: {expected}\n"
        "Unset PYTHONPATH or select the canonical checkout, then retry, for example:\n"
        "  unset PYTHONPATH\n"
        f"  PYTHONPATH={expected.parent} uv run sac image build ..."
    )


class _SacBootstrapGroup(click.Group):
    """Auditable facade whose execution uses SAC's console wrapper."""

    _delegate: Callable[[], object]

    def main(
        self,
        args: list[str] | None = None,
        prog_name: str | None = None,
        **extra: object,
    ) -> object:
        """Delegate execution while retaining SAC's pre-Click processing."""
        delegated_args = list(sys.argv[1:] if args is None else args)
        if args is None:
            return self._delegate()
        original_argv = sys.argv
        sys.argv = [prog_name or original_argv[0], *delegated_args]
        try:
            return self._delegate()
        finally:
            sys.argv = original_argv


def _load_cli() -> click.Group:
    """Validate first, then construct an auditable facade over the real CLI."""
    try:
        assert_image_build_source_authority()
    except ImageBuildSourceMismatch as exc:
        click.echo(f"error: {exc}", err=True)
        raise SystemExit(1) from exc

    from scitex_agent_container.cli import cli_entry_point as delegate
    from scitex_agent_container.cli import main as command

    facade = _SacBootstrapGroup(
        name=command.name,
        commands=command.commands,
        params=command.params,
        callback=command.callback,
        context_settings=command.context_settings,
        help=command.help,
        epilog=command.epilog,
        short_help=command.short_help,
        options_metavar=command.options_metavar,
        add_help_option=command.add_help_option,
        no_args_is_help=command.no_args_is_help,
        invoke_without_command=command.invoke_without_command,
        subcommand_metavar=command.subcommand_metavar,
        chain=command.chain,
    )
    facade._delegate = delegate
    return facade


def __getattr__(name: str) -> object:
    """Resolve the console object without importing SAC before the guard."""
    if name == "cli_entry_point":
        return _load_cli()
    raise AttributeError(name)


__all__ = [
    "ImageBuildSourceMismatch",
    "assert_image_build_source_authority",
    "cli_entry_point",
]
