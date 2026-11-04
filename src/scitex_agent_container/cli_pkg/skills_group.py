"""`sac dev skills` — list / get / install agent-facing skills bundled with this package.

List/get read the leaf bundle directly. Installation delegates to the shared
Dev installer and its hashed projection registry; SAC owns Hermes exposure.
"""

from __future__ import annotations

from pathlib import Path

import click

from ._helpers import HelpRecursiveGroup

PKG = "scitex-agent-container"


def _skills_root() -> Path:
    """Resolve the bundled `_skills/scitex-agent-container/` directory."""
    import scitex_agent_container

    pkg_dir = Path(scitex_agent_container.__file__).parent
    return pkg_dir / "_skills" / PKG


def _list_skill_files(root: Path) -> list[Path]:
    """All `.md` files under `_skills/scitex-agent-container/` (recursive)."""
    if not root.is_dir():
        return []
    return sorted(p for p in root.rglob("*.md") if p.is_file())


@click.group(name="skills", cls=HelpRecursiveGroup)
def skills_group() -> None:
    """Agent-facing skills bundled with scitex-agent-container.

    \b
    Examples:
      $ sac dev skills list
      $ sac dev skills get 01_installation
      $ sac dev skills install                      # → ~/.scitex/dev/skills/scitex-agent-container/ + Hermes exposure
      $ sac dev skills install --no-hermes-symlink  # store only, skip Hermes exposure
      $ sac dev skills install --claude-symlink     # (legacy) also expose to ~/.claude/skills/scitex/
    """


@skills_group.command(name="list")
@click.option("--json", "as_json", is_flag=True, help="Emit machine-readable JSON.")
def skills_list(as_json: bool) -> None:
    """List skill files bundled with this package.

    \b
    Example:
      $ sac dev skills list
      $ sac dev skills list --json
    """
    root = _skills_root()
    files = _list_skill_files(root)
    if as_json:
        import json as _json

        click.echo(
            _json.dumps(
                [{"name": p.stem, "path": str(p)} for p in files],
                indent=2,
            )
        )
        return
    if not files:
        click.echo(f"no skills found at {root}", err=True)
        raise SystemExit(1)
    for p in files:
        rel = p.relative_to(root)
        click.echo(f"{p.stem:36s}  {rel}")


@skills_group.command(name="get")
@click.argument("name")
@click.option("--json", "as_json", is_flag=True, help="Emit machine-readable JSON.")
def skills_get(name: str, as_json: bool) -> None:
    """Print the contents of a skill file by NAME (e.g. `01_installation`).

    \b
    Example:
      $ sac dev skills get 01_installation
      $ sac dev skills get 02_quick-start --json
    """
    root = _skills_root()
    target_stem = name[:-3] if name.endswith(".md") else name
    match = next((p for p in _list_skill_files(root) if p.stem == target_stem), None)
    if match is None:
        click.echo(f"skill not found: {name}", err=True)
        available = ", ".join(p.stem for p in _list_skill_files(root)[:8])
        click.echo(f"available: {available}…", err=True)
        raise SystemExit(1)
    if as_json:
        import json as _json

        click.echo(
            _json.dumps(
                {
                    "name": match.stem,
                    "path": str(match),
                    "content": match.read_text(encoding="utf-8"),
                },
                indent=2,
            )
        )
        return
    click.echo(match.read_text(encoding="utf-8"))


@skills_group.command(name="install")
@click.option(
    "--dest",
    type=click.Path(),
    default=None,
    help="Destination dir (default: ~/.scitex/dev/skills/scitex-agent-container/).",
)
@click.option(
    "--no-link",
    "no_link",
    is_flag=True,
    help="Copy files instead of symlinking. Default is symlink.",
)
@click.option(
    "--claude-symlink",
    is_flag=True,
    help="(Legacy opt-in) also expose at ~/.claude/skills/scitex/ for Claude Code consumers.",
)
@click.option(
    "--no-hermes-symlink",
    "no_hermes_symlink",
    is_flag=True,
    help="Skip the default Hermes exposure at ~/.hermes/skills/scitex-agent-container/.",
)
@click.option("--json", "as_json", is_flag=True, help="Emit the shared projection manifest result.")
@click.option("--dry-run", is_flag=True, help="Preview without copying/linking.")
@click.option("--yes", "-y", is_flag=True, help="Skip confirmation prompt.")
def skills_install(
    dest: str | None,
    no_link: bool,
    claude_symlink: bool,
    no_hermes_symlink: bool,
    dry_run: bool,
    as_json: bool,
    yes: bool,
) -> None:
    """Install this package's skills into a target directory.

    \b
    The shared Dev installer validates ownership and hashes, preserves other
    installed packages, and resolves the canonical store via $SCITEX_DIR.
    Default mode links the installed SAC bundle; --no-link forces a copy.
    Unmanaged or dirty destinations are refused. Then expose
    the installed package to Hermes (the first-option harness) at
    ~/.hermes/skills/scitex-agent-container/.

    Use --no-hermes-symlink to skip the Hermes exposure, and
    --claude-symlink (legacy opt-in) to also expose at
    ~/.claude/skills/scitex/ for Claude Code's skill loader.

    \b
    Example:
      $ sac dev skills install
      $ sac dev skills install --no-hermes-symlink
      $ sac dev skills install --claude-symlink     # legacy Claude Code exposure
      $ sac dev skills install --no-link --dest /tmp/sac-skills
    """
    del yes  # compatibility flag; shared installer validates before writing
    from ._skill_install import install_skills

    install_skills(
        dest=dest, no_link=no_link, claude_symlink=claude_symlink,
        no_hermes_symlink=no_hermes_symlink, dry_run=dry_run, as_json=as_json,
    )
