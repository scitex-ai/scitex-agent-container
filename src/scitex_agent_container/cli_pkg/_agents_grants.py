"""``sac agents grants`` — infer and check explicit ``spec.grants`` blocks.

Migration path for the deny-by-default flip (docs/grants.md):

    sac agents grants infer NAME          # print the block that reproduces
                                          # NAME's current (legacy) authority
    (paste it under spec: in spec.yaml)
    sac agents grants check NAME          # prove it: legacy vs spec+block,
                                          # argv / env names / home — 0 diff

Both render into a THROWAWAY directory (``--render-dir``, default a fresh
temp dir) — never the agent's real state dir, overlay or ``~/.scitex``, and
nothing is launched. Secret VALUES are never printed: argv entries are
redacted the way ``write_redacted_argv`` does, env is reported by NAME, and
secret-shaped home files by key names only.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import click


def _resolve(name_or_path: str) -> Path:
    from ..config import resolve_config

    try:
        return Path(resolve_config(name_or_path)).resolve()
    except Exception as exc:  # stx-allow: fallback (re-raised as click.ClickException, printed to stderr)
        raise click.ClickException(str(exc)) from exc


def _raw(path: Path) -> dict:
    import yaml

    doc = yaml.safe_load(path.read_text())
    if not isinstance(doc, dict):
        raise click.ClickException(f"{path} is not a YAML mapping")
    return doc


def _render_root(render_dir: str | None):
    import tempfile
    from contextlib import contextmanager

    @contextmanager
    def _root():
        if render_dir:
            yield Path(render_dir)
            return
        tmp = tempfile.mkdtemp(prefix="sac-grants-")
        try:
            yield Path(tmp)
        finally:
            import shutil

            shutil.rmtree(tmp, ignore_errors=True)

    return _root()


def _block_yaml(block: dict) -> str:
    import yaml

    return yaml.safe_dump({"grants": block}, sort_keys=False, default_flow_style=None)


_RENDER_DIR = click.option(
    "--render-dir",
    type=click.Path(file_okay=False),
    default=None,
    help="Throwaway directory to render into (wiped per render). Default: a temp dir.",
)


@click.group("grants")
def grants_group() -> None:
    """Infer / check explicit spec.grants blocks (deny-by-default migration)."""


@grants_group.command("infer")
@click.argument("name_or_path")
@_RENDER_DIR
def infer_cmd(name_or_path: str, render_dir: str | None) -> None:
    """Print the grants: block that reproduces NAME's current authority.

    Example:
      $ sac agents grants infer scitex-dev
    """
    from .._grants._infer import infer

    path = _resolve(name_or_path)
    with _render_root(render_dir) as root:
        block, legacy, _volatile = infer(_raw(path), path, root=root)
    click.echo(f"# inferred for {path}")
    if legacy.error:
        click.echo(
            "# WARNING: the legacy render itself is refused, so authorities "
            f"past the refusal are not seen: {legacy.error}"
        )
    click.echo(_block_yaml(block), nl=False)


@grants_group.command("check")
@click.argument("names_or_paths", nargs=-1, required=True)
@_RENDER_DIR
@click.option("--json", "as_json", is_flag=True, help="One JSON object per agent.")
def check_cmd(
    names_or_paths: tuple[str, ...], render_dir: str | None, as_json: bool
) -> None:
    """Prove the inferred block reproduces each agent's legacy launch surface.

    Renders (a) the spec without spec.grants and (b) the spec plus the
    inferred block, and diffs argv (redacted), env names and the home file
    list. Exit 1 when any agent shows a diff.

    Example:
      $ sac agents grants check scitex-dev grant
    """
    from .._grants._infer import check

    failed = 0
    for item in names_or_paths:
        path = _resolve(item)
        with _render_root(render_dir) as root:
            result = check(_raw(path), path, root=root / path.parent.name)
        failed += 0 if result.passed else 1
        if as_json:
            click.echo(
                json.dumps(
                    {
                        "spec": str(path),
                        "grants": result.block,
                        "passed": result.passed,
                        "conclusive": result.conclusive,
                        "diff": result.diff,
                        "volatile_lines": result.volatile,
                        "legacy_error": result.legacy_error,
                    }
                )
            )
            continue
        if result.diff:
            verdict = f"FAIL ({len(result.diff)} diff)"
        elif not result.conclusive:
            verdict = "INCONCLUSIVE (0 diff, but the legacy render is refused)"
        else:
            verdict = "PASS (0 diff)"

        click.echo(f"{path.parent.name}: {verdict}")
        click.echo(_block_yaml(result.block), nl=False)
        if result.legacy_error:
            click.echo(f"  note: legacy render refused: {result.legacy_error}")
        for line in result.diff:
            click.echo(f"  {line}")
    sys.exit(1 if failed else 0)


__all__ = ["grants_group"]
