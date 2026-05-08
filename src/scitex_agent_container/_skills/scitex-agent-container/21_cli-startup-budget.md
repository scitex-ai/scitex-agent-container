---
description: |
  [TOPIC] scitex-agent-container — CLI startup-time budget
  [DETAILS] Why ``sac --help`` and tab-completion stay under 500 ms: the LazyGroup pattern in cli_pkg/_main.py, the LAZY_SHORT_HELPS cache, deferred shell-completion attach, and the pytest gate that prevents regressions.
tags: [scitex-agent-container-cli-startup-budget]
---

# CLI startup budget — 500 ms ceiling

`sac` is a daily-driver command. Every Tab press in a user shell
re-runs the entry point, so a 2 s import graph is felt as visible
lag. We hold a 500 ms ceiling on `sac --help` cold-start (Python
boot + click + entry-point group + `--help` rendering, no
subcommand work). A pytest test enforces it; this doc explains the
mechanism so the rule is grokkable, not just a magic number.

## Why this matters

Tab completion and `--help` only need command **names** and
**short_help strings**. They never invoke a subcommand. Importing the
implementation module of every subcommand at entry-point load time is
pure waste in the common path.

Measured before LazyGroup: `time scitex-agent-container --help` ≈
**2.5 s** (most of it `cli_pkg/_main` eagerly importing 30+
subcommand modules, each pulling in rich/runtimes/a2a/_lifecycle/…).

After LazyGroup: **~400 ms** (Python startup ~100 ms, click +
LazyGroup ~50 ms, `_helpers` + rich ~150 ms, `--help` rendering
~30 ms, headroom for slow disks).

## How LazyGroup works

`cli_pkg/_lazy_group.py` defines `LazyGroup`: a `click.Group`
subclass that holds two registries instead of importing subcommands
at module top.

* `LAZY_COMMANDS = {"agent": "scitex_agent_container.cli_pkg.agent_group:agent_group", …}`
  — visible top-level commands. The module is only imported when
  `get_command(name)` is actually called (invocation or `--help <name>`).
* `LAZY_RENAMED = {"start": ("…lifecycle_cmds:start", "sac agent start"), …}`
  — legacy F-CS13 redirects. Same lazy mechanism; the renamed wrapper
  is built on demand and cached.
* `LAZY_SHORT_HELPS = {"agent": "Agent lifecycle, …", …}` — mirror of
  each command's short_help, populated by hand. Without it,
  `format_commands` would call `get_command(name).get_short_help_str()`
  for every row, importing every module just to render `--help`.

`format_commands` is overridden to read from `LAZY_SHORT_HELPS`
directly. Eager / unmapped names fall back to per-command lookup.

`scitex_dev._cli._completion.attach_shell_completion` is also
deferred: it adds two top-level commands (`install-shell-completion`,
`print-shell-completion`) but pulls in the full linter graph
(~490 ms). `_MainGroup.get_command` triggers `_attach_completion()`
only when one of those names is actually invoked — `--help` and tab
completion list the names from the cache and skip the import.

## Adding a new top-level command

1. Add the entry to `LAZY_COMMANDS` in `cli_pkg/_main.py`.
2. Add its `short_help` to `LAZY_SHORT_HELPS` in the same class.
3. If it's a renamed legacy alias, use `LAZY_RENAMED` instead and
   give it a hidden short_help (or omit — renamed entries are hidden
   from `--help` and tab completion).
4. Run `pytest tests/integration/test_cli_startup_budget.py` to
   confirm the budget is still green.

To refresh `LAZY_SHORT_HELPS` after editing a subcommand's docstring,
re-run the snippet at the top of `cli_pkg/_main.py` (or just inspect
`cmd.get_short_help_str()` from a Python REPL).

## Why not …

* **Auto-discovery via importlib.metadata entry-points.** Same
  one-import-per-row cost, just spelled differently.
* **Click's `MultiCommand.list_commands` returning a generator.** The
  formatter still calls `get_command` on each name; the win is on
  not-importing-everything-immediately, not on enumeration.
* **Top-level `from .x import y` with `lazy_loader`.** Adds a
  dependency for a single-file refactor.

## Test enforcement

`tests/integration/test_cli_startup_budget.py` measures cold-start
time of `scitex-agent-container --help` and asserts it's below
**500 ms**. The test forks a clean Python process so it doesn't
benefit from the parent's already-warmed `sys.modules`. Set
`SAC_STARTUP_BUDGET_S` to override the threshold in CI environments
that need slack (default: 0.5).
