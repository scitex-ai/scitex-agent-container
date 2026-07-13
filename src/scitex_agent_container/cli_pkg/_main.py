"""Click entry point for scitex-agent-container.

Subcommand modules are NOT imported at module top-level — every entry
on the user-facing surface is registered through ``LazyGroup`` so a
``--help`` or ``<TAB>`` only pays for the click + LazyGroup imports
(~150 ms). Modules load on demand when their command actually runs.
See ``_lazy_group.py`` for the mechanism and ``21_cli-startup-budget.md``
for the rationale.
"""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _pkg_version_lookup

import click

from ._lazy_group import LazyGroup


def _pkg_version(lookup=_pkg_version_lookup) -> str:
    """Return the installed package version, or 'dev' off-tree.

    ``lookup`` is the ``importlib.metadata.version``-shaped callable
    used to read the dist version; it's a parameter (not a hard import
    reference) so tests can pass a real callable that raises
    ``PackageNotFoundError`` to exercise the off-tree fallback without
    monkey-patching module state.
    """
    try:
        return lookup("scitex-agent-container")
    except PackageNotFoundError:
        return "dev"


def _print_version(ctx: click.Context, param, value) -> None:
    """``--version``: report the identity of the LOADED code, then exit.

    This replaces ``click.version_option``, which prints only the declared
    version string — a number that reads IDENTICALLY on a machine where a
    fix shipped and one where it did not, because a fix that does not bump
    the version does not move it. It therefore cannot answer the one
    question it is ever asked: is my fix actually deployed?

    The line keeps click's ``<prog>, version <X.Y.Z>`` prefix, so anything
    parsing the third whitespace field still works, and appends the commit
    and the path the module was really imported from.

    Cost: ~0.5 ms over the ``importlib.metadata`` lookup click already did
    — no subprocess (``git rev-parse`` forks cost ~89 ms) and no tree walk
    (hashing the tree costs ~35 ms). Those live in ``sac provenance``.
    """
    if not value or ctx.resilient_parsing:
        return
    from .._provenance import format_terse, identity

    click.echo(format_terse(identity()))
    ctx.exit()


# ---------------------------------------------------------------------------
# Help categories — clean noun-group surface
# ---------------------------------------------------------------------------
COMMAND_CATEGORIES = [
    ("Agent", ["agents", "subagent"]),
    ("Lifecycle (multiplexer)", ["auto-accept"]),
    ("Account", ["accounts"]),
    (
        "Network & Peer",
        ["host", "peer", "a2a", "fleet", "listen"],
    ),
    ("Registry & Events", ["db", "registry", "event"]),
    ("Build & Install", ["image", "installation"]),
    ("Diagnostics", ["doctor", "ports", "provenance"]),
    ("Remote testing", ["pytest"]),
    ("Introspection", ["mcp", "list-python-apis", "skills", "versions"]),
    ("Developer", ["dev"]),
]


_PKG = "scitex_agent_container.cli_pkg"


class _MainGroup(LazyGroup):
    command_categories = COMMAND_CATEGORIES

    # User-facing top-level commands. Resolved on demand.
    LAZY_COMMANDS = {
        # Noun groups
        "agents": f"{_PKG}.agent_group:agent_group",
        "subagent": f"{_PKG}.subagent_group:subagent_group",
        "db": f"{_PKG}.db_group:db_group",
        "dev": f"{_PKG}.dev_group:dev_group",
        "host": f"{_PKG}.host_group:host_group",
        "registry": f"{_PKG}.registry_group:registry_group",
        "event": f"{_PKG}.event_group:event_group",
        "image": f"{_PKG}.image_group:image_group",
        "skills": f"{_PKG}.skills_group:skills_group",
        # `accounts` is a top-level noun — the credential store is fleet-wide,
        # not agent-scoped. Reuses the underlying `account` click group;
        # `accounts` is just the plural surface name.
        "accounts": f"{_PKG}.account_group:account",
        "a2a": f"{_PKG}.a2a_group:a2a",
        "mcp": f"{_PKG}.mcp_group:mcp",
        "peer": f"{_PKG}.peer_group:peer_group",
        "fleet": f"{_PKG}.fleet_group:fleet_group",
        "listen": f"{_PKG}.listen_cmds:listen",
        "doctor": f"{_PKG}.doctor_cmds:doctor",
        # Read-only port-hygiene inventory: listen 7878 + a2a claims +
        # the scitex/sac port-assignment reference map.
        "ports": f"{_PKG}.ports_cmds:ports",
        # Which code is ACTUALLY loaded — the heavy half of `--version`
        # (tree hash, duplicate/fossil .dist-info, shadowed imports).
        "provenance": f"{_PKG}.provenance_cmds:provenance",
        # Spartan pytest runner (operator directive 2026-06-13). Phase 1
        # surface: ``sac pytest spartan run <repo>@<branch>``. The lazy
        # mapping resolves to the ``pytest_group`` click group exported
        # by the ``spartan_pytest`` package; ``spartan run`` lives
        # underneath.
        "pytest": f"{_PKG}.spartan_pytest:pytest_group",
        # Top-level standalone
        "list-python-apis": f"{_PKG}.info_cmds:list_python_apis",
        "installation": f"{_PKG}.installation_group:install_group",
        # scitex-* version introspection across sac's base + overlay layers
        # (drift-report aggregation surface for scitex-dev ecosystem
        # check-versions — the 2 layers only sac can see).
        "versions": f"{_PKG}.versions_cmds:versions",
    }

    # Tracks whether scitex_dev._cli._completion has been attached.
    # ``attach_shell_completion`` adds two top-level commands but pulls
    # in the linter graph (~490 ms). We defer the import until the
    # ``install-shell-completion`` or ``print-shell-completion`` name
    # is actually resolved — the audit-cli §1a check, the user
    # invoking the command, and tab completion all go through
    # ``_resolve_lazy`` via the dict installed by ``LazyGroup``, so
    # one hook covers every entry-point.
    _completion_attached = False
    _COMPLETION_NAMES = ("install-shell-completion", "print-shell-completion")

    def _attach_completion(self) -> None:
        if self._completion_attached:
            return
        self._completion_attached = True
        try:
            from scitex_dev._cli._completion import attach_shell_completion
        except Exception:  # stx-allow: fallback (reason: scitex-dev[cli-audit] is optional; broaden beyond ImportError so a misbuilt transitive dep can't break CLI startup)
            # scitex-dev[cli-audit] not installed (or its import chain
            # raised something other than ImportError); completion
            # commands stay missing — non-fatal for the CLI itself.
            return
        attach_shell_completion(self, prog_name="scitex-agent-container")
        # The upstream helper writes an ``eval "$(_NAME_COMPLETE=...)"``
        # line in ~/.bashrc for ONE binary. Two problems for sac:
        #   1. We ship TWO binaries (``scitex-agent-container`` and ``sac``);
        #      Click's completion is keyed on argv[0], so each name needs
        #      its own registration.
        #   2. The eval line invokes the binary on every shell start
        #      (~0.4 s per binary; 9 scitex eval lines = ~3.6 s of source
        #      ~/.bashrc latency).
        # Replace the upstream behaviour with a cache-file install: write
        # the static completion script once to
        # ``~/.local/share/bash-completion/scitex/<binary>`` and let
        # ~/.bashrc just ``source`` that file (microseconds).
        self._install_shell_completion_cache_based()

    def _install_shell_completion_cache_based(self) -> None:
        """Replace install-shell-completion with a cache-file install.

        Writes generated completion scripts (one per binary) to
        ``~/.local/share/bash-completion/scitex/<binary>`` and appends
        ``source`` lines to ~/.bashrc. The source op is O(microseconds);
        the eval-the-binary op was O(0.4 s).
        """
        import os
        import subprocess
        from pathlib import Path

        cmd = self.commands.get("install-shell-completion")
        if cmd is None:
            return

        BINARIES = (
            ("scitex-agent-container", "_SCITEX_AGENT_CONTAINER_COMPLETE"),
            ("sac", "_SAC_COMPLETE"),
        )
        SOURCE_MAP = {"bash": "bash_source", "zsh": "zsh_source"}

        def install_cached(*args, **kwargs):
            # Re-resolve Path.home() each call (not at attach time) so
            # $HOME changes between invocations are honoured (matters for
            # tests under tmp_path, and for users who run with a custom
            # HOME via env-prefix).
            # Primary: sac-owned, under runtime/ per local-state-directories spec §4b.
            SAC_CACHE_DIR = (
                Path.home() / ".scitex" / "agent-container" / "runtime" / "completion"
            )
            # Secondary: XDG bash-completion dir (where third-party tooling
            # auto-discovers); kept as a symlink to the sac-owned file so
            # both paths point at the same content.
            XDG_CACHE_DIR = (
                Path.home() / ".local" / "share" / "bash-completion" / "scitex"
            )
            shell = kwargs.get("shell", "bash")
            dry_run = kwargs.get("dry_run", False)
            if shell not in SOURCE_MAP:
                click.echo(
                    f"error: cache install supports bash/zsh; got {shell!r}", err=True
                )
                return
            rc_path = Path.home() / (".bashrc" if shell == "bash" else ".zshrc")

            for binary, env_var in BINARIES:
                cache_path = SAC_CACHE_DIR / binary
                xdg_link = XDG_CACHE_DIR / binary
                source_line = f"[ -f {cache_path} ] && source {cache_path}"
                marker = f"# sac-completion: {binary}"

                if dry_run:
                    click.echo(f"Would write {cache_path} ({binary} completions)")
                    click.echo(f"Would symlink {xdg_link} -> {cache_path}")
                    click.echo(f"Would append to {rc_path}: {source_line}  {marker}")
                    continue

                # Generate static script via the binary itself.
                env = os.environ.copy()
                env[env_var] = SOURCE_MAP[shell]
                result = subprocess.run(
                    [binary], capture_output=True, text=True, env=env
                )
                if result.returncode != 0 or not result.stdout.strip():
                    click.echo(
                        f"warn: failed to generate completion for {binary}",
                        err=True,
                    )
                    continue
                SAC_CACHE_DIR.mkdir(parents=True, exist_ok=True)
                cache_path.write_text(result.stdout)

                # XDG symlink for auto-discovery (idempotent).
                XDG_CACHE_DIR.mkdir(parents=True, exist_ok=True)
                if xdg_link.is_symlink() or xdg_link.exists():
                    xdg_link.unlink()
                xdg_link.symlink_to(cache_path)

                # Add the source line in rc if not already present.
                if rc_path.is_file() and marker in rc_path.read_text():
                    continue
                with rc_path.open("a") as fh:
                    fh.write(f"\n{source_line}  {marker}\n")
                click.echo(f"Tab completion installed: {cache_path}")
                click.echo(f"  XDG symlink: {xdg_link}")

            if not dry_run:
                click.echo(f"Run: source {rc_path}")

        cmd.callback = install_cached

    def list_commands(self, ctx):
        names = super().list_commands(ctx)
        return sorted(set(names) | set(self._COMPLETION_NAMES))

    def _resolve_lazy(self, name):
        if name in self._COMPLETION_NAMES:
            self._attach_completion()
            # ``attach_shell_completion`` populates ``self.commands``
            # via ``add_command``; pull the result back out so the
            # caller (LazyCommandsDict / get_command) sees it.
            return dict.get(self.commands, name)
        return super()._resolve_lazy(name)

    # Mirror of each top-level command's ``short_help``. Populated by
    # hand because reading it via ``cmd.get_short_help_str()`` would
    # force the import we're trying to avoid. Re-run the snippet in
    # ``21_cli-startup-budget.md`` to refresh after editing any
    # subcommand's docstring/short_help.
    LAZY_SHORT_HELPS = {
        "agents": "Agent lifecycle, status, introspection, and snapshots.",
        "accounts": "Manage stored Claude Code accounts for credential rotation.",
        "db": "Inspect and maintain the sac state database (state.db).",
        "dev": "Developer / maintainer plumbing (CI secrets, etc.).",
        "host": "Local host identity and peer routing for sac.",
        "registry": "Registry maintenance — folded into ``sac db`` (F-CS11).",
        "event": "Event log operations: ingest hook events into the per-agent ring buffer.",
        "image": "Container image operations: build the runtime base image.",
        "skills": "Agent-facing skills bundled with scitex-agent-container.",
        "auto-accept": "Auto-accept TUI handler for Claude Code permission prompts.",
        "a2a": "A2A protocol — generic agent-to-agent surface (no fleet deps).",
        "mcp": "MCP (Model Context Protocol) server commands.",
        "peer": "Outbound A2A calls into other agents' POST /v1/turn endpoint.",
        "list-python-apis": "List all public Python APIs of scitex-agent-container.",
        "installation": "Bootstrap and install helpers for a new fleet host.",
        "fleet": "Peer-aware multi-agent orchestration across hosts.",
        "doctor": "Diagnose agent-spec source drift (local, or --fleet across hosts).",
        "provenance": "Prove which code is actually loaded (commit, origin, fossil installs).",
        "listen": "Boot the sac listen HTTP/JSON control-plane server.",
        "ports": "List the ports sac/scitex uses, with live status.",
        "pytest": "Run pytest on remote pools (Spartan SLURM, ...).",
        "install-shell-completion": "Wire up `<TAB>` completion in the user's shell rc.",
        "print-shell-completion": "Print the shell-completion eval line (no install).",
    }

    # Renamed-command redirects (F-CS13 / scitex CLI convention §5):
    # legacy click-name → (module:symbol, new-path). Hidden from --help
    # and tab-completion; resolving the legacy name still works (prints
    # a redirect to stderr and exits with code 2). Soft warnings let
    # stale scripts persist indefinitely; hard errors force the fix.
    LAZY_RENAMED = {
        # Lifecycle
        "start": (f"{_PKG}.lifecycle:start", "sac agents start"),
        "stop": (f"{_PKG}.lifecycle:stop", "sac agents stop"),
        "restart": (f"{_PKG}.lifecycle:restart", "sac agents restart"),
        "validate": (f"{_PKG}.build_cmds:validate", "sac agents validate"),
        "check": (f"{_PKG}.build_cmds:check", "sac agents check"),
        # Status / introspection
        "show-status": (f"{_PKG}.status_cmds:status", "sac agents status"),
        "check-health": (f"{_PKG}.status_cmds:health", "sac agents health"),
        "find": (f"{_PKG}.info_cmds:find", "sac agents find"),
        "recall": (f"{_PKG}.recall_cmds:recall", "sac agents recall"),
        # Quota — accounts is a top-level noun (the credential store is
        # fleet-wide, not agent-scoped).
        "watch-quota": (
            f"{_PKG}.account_group:quota_watch",
            "sac accounts watch-quota",
        ),
        # Hook events
        "ingest-hook-event": (f"{_PKG}.hook_cmds:hook_event", "sac event ingest"),
        # Registry — ``registry clean`` is now ``db clean`` (F-CS11 phase 5);
        # send the top-level alias straight there to avoid double-redirect.
        "clean-registry": (f"{_PKG}.lifecycle:cleanup", "sac db clean"),
        "reconcile-singletons": (
            f"{_PKG}.priority_cmds:singleton_reconcile",
            "sac registry reconcile",
        ),
        # Build / image
        "build-image": (f"{_PKG}.build_cmds:build", "sac image build"),
        # Network — ``probe-network`` and ``network probe`` both fold
        # into ``host probe-hub`` (same reachability concern).
        "probe-network": (f"{_PKG}.probe_cmds:probe_network", "sac host probe-hub"),
        # Install
        "install-post-merge-cron": (
            f"{_PKG}.installation_group:install_post_merge_cron",
            "sac installation setup-cron",
        ),
    }


@click.group(
    cls=_MainGroup,
    invoke_without_command=True,
    context_settings={"help_option_names": ["-h", "--help"]},
    help=(
        f"sac (v{_pkg_version()}) — SciTeX Agent Container: declarative "
        f"agent management.\n\n"
        "\b\n"
        "Each agent lives in its own directory with a ``spec.yaml``:\n"
        "  ~/.scitex/agent-container/agents/<name>/spec.yaml\n"
        "Subcommands accept either the bare ``<name>`` (looked up under the\n"
        "search root) or an explicit path to the YAML file.\n\n"
        "\b\n"
        "Search order when only a name is given:\n"
        "  1. ./.scitex/agent-container/agents/<name>/spec.yaml  (project-local)\n"
        "  2. ~/.scitex/agent-container/agents/<name>/spec.yaml  (user-wide)\n"
        "  3. ``$SCITEX_AGENT_CONTAINER_YAML_DIRS`` (colon-separated extra dirs)\n\n"
        "\b\n"
        "Example:\n"
        "  $ sac --version\n"
        "  $ sac agents status\n"
        "  $ sac agents start orchestrator                                      # by name\n"
        "  $ sac agents start ~/.scitex/agent-container/agents/orchestrator/spec.yaml   # by path\n"
    ),
)
@click.option(
    "-V",
    "--version",
    is_flag=True,
    expose_value=False,
    is_eager=True,
    callback=_print_version,
    help="Show the version, the commit it was built from, and where it loaded from.",
)
@click.option(
    "--help-recursive",
    is_flag=True,
    default=False,
    help="Show help for all commands recursively.",
)
@click.option(
    "--json",
    "as_json",
    is_flag=True,
    default=False,
    help="Output as structured JSON (propagates to subcommands).",
)
@click.pass_context
def main(ctx: click.Context, help_recursive: bool, as_json: bool) -> None:
    """SciTeX Agent Container -- Declarative agent management.

    \b
    Each agent lives in its own directory with a ``spec.yaml``:
      ~/.scitex/agent-container/agents/<name>/spec.yaml
    Subcommands accept either the bare ``<name>`` or an explicit path.

    \b
    Search order when only a name is given:
      1. ./.scitex/agent-container/agents/<name>/spec.yaml  (project-local)
      2. ~/.scitex/agent-container/agents/<name>/spec.yaml  (user-wide)
      3. ``$SCITEX_AGENT_CONTAINER_YAML_DIRS`` (colon-separated extra dirs)

    \b
    Example:
      $ sac agents start orchestrator                                      # by name
      $ sac agents start ~/.scitex/agent-container/agents/orchestrator/spec.yaml   # by path
    """
    ctx.ensure_object(dict)
    if as_json:
        ctx.obj["json"] = True
    if help_recursive:
        click.echo(ctx.command.get_help_recursive(ctx))  # type: ignore[attr-defined]
        ctx.exit(0)
    elif ctx.invoked_subcommand is None:
        click.echo(ctx.get_help())


def cli_entry_point() -> None:
    """Console-script entry. Honours the global ``--on <peer>`` flag.

    Click's group parser normally consumes ``--on`` during ``main``'s
    own arg parsing, but the flag has to be honoured BEFORE the
    subcommand is dispatched: ``sac --on spartan agent list`` must
    run ``sac agents list`` on spartan, not locally. Pre-process
    ``sys.argv`` here, dispatch via host_group.dispatch_remote when
    the flag is present, and fall through to plain ``main()``
    otherwise.
    """
    import sys

    # Cheap pre-scan: don't import the heavy ``host_group`` module
    # unless ``--on`` is actually present on the command line. Importing
    # ``host_group`` (state.host_config + subprocess + the full click
    # group surface) is ~80 ms of import time and is wasted on every
    # plain ``sac --help`` / tab-completion / subcommand invocation.
    argv = sys.argv[1:]
    if "--on" not in argv and not any(a.startswith("--on=") for a in argv):
        main()
        return

    from .host_group import dispatch_remote, split_on_flag

    # stx-allow: fallback (reason: a malformed --on value should still
    # surface a useful error rather than crash the entry point)
    try:
        peer, rest = split_on_flag(argv)
    except click.UsageError as exc:
        click.echo(f"error: {exc.format_message()}", err=True)
        sys.exit(2)
    if peer is not None:
        sys.exit(dispatch_remote(peer, rest))
    main()


if __name__ == "__main__":
    cli_entry_point()
