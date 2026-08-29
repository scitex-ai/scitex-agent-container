# image_build_hooks

Claude Code PreToolUse hooks protecting **sac's own image build**.

| file | role |
|---|---|
| `deny_raw_apptainer_build.sh` | refuses a hand-run `apptainer build` of a sac image |
| `settings.local.json.fragment.json` | manual `PreToolUse`/`Bash` wiring (legacy path) |

## Who declares, who applies

sac **declares** this rule and ships the script. sac does **not** install it.
scitex-dev discovers the declaration through the `scitex_dev.hooks`
entry-point group and applies it centrally:

```
pyproject.toml
  [project.entry-points."scitex_dev.hooks"]
  scitex-agent-container = "scitex_agent_container._claude_hooks_plugin:provide_hooks"
                                                    |
                                                    v
scitex_agent_container/_claude_hooks_plugin.py   -> HookRule(id="sac.no-raw-apptainer-build", ...)
                                                    |
                                                    v
scitex-dev  discover_hooks()                     -> installs $HOME/.claude/hooks/pre-tool-use/
```

This is the same leaf-declares / keystone-applies split as `scitex_dev.jobs`,
`scitex_dev.system_deps` and `scitex_dev.gate.checks`. The leaf owns the rule
because it is sac's images being protected; scitex-dev hardcodes nothing about
apptainer. `cli_pkg/image_group.py` already names the same doctrine in its own
comment: *"each package owns its own surface; the aggregator never hard-codes
package names"*.

The `scitex_dev.hooks` import in the provider is **lazy**, so the entry point
ships and stays inert on a scitex-dev that predates the contract rather than
breaking import-time metadata — same idiom and same reason as
`_jobs/_jobs_plugin.py`.

## Why the rule exists

sac's image build is **not** `apptainer build` plus arguments.

`sac image build` stages a build-context directory holding the `.def`
alongside a `scitex-agent-container-src/` copy of the installed package, and
the `.def` resolves its `%files` sources against that staging dir.
`apptainer-base.def` documents the contract itself:

> Bundle the package's OWN source tree so the in-SIF sac is the source tree
> that shipped this .def — never a `git+...@main` snapshot of whatever
> happened to be on a branch at build time. […] apptainer resolves the
> relative source path below against the build CWD, which the CLI sets to
> that staging dir.

A hand-run build skips the staging. It does **not** fail loudly — it produces
a SIF whose in-image sac is whatever happened to be lying around, and the
mismatch surfaces weeks later as a version that makes no sense.

The build is additionally becoming **staged** (base → scitex → …), where a
hand-run build also bypasses parent-chain resolution and staleness checking,
silently layering a child on a stale or missing parent.

### Not a duplicate of `heavy_job_hooks`

`enforce_heavy_job_demotion.sh` judges only whether a heavy command was
`nice`'d. A fully demoted
`nice -n 19 ionice -c 2 -n 7 apptainer build … apptainer-base.def`
sails through it today. This hook closes exactly that gap, and the self-test
pins the case.

## The discriminator, and why

`apptainer build` against an unrelated image is legitimate and must keep
working. **A guard that blocks unrelated builds gets disabled, and then the
real rule is gone with it.** So the hook refuses only what it can prove is
ours, keyed on the recipe's **content** — not its filename, not the output
SIF's name:

**Primary — the label key `org.scitex.layer`.** Every sac recipe declares it
under `%labels` (`base`, `scitex`, `proxy` today). Content-keyed, so it
survives the `.def` renames in flight, survives the recipe directory moving,
and still catches a `.def` copied to `/tmp`. It is also the *same* notion of
"ours" the built artifact carries — `apptainer inspect --labels x.sif` reports
`org.scitex.layer: base` — so guard and artifact agree.

The matcher is `^[ \t]*org\.scitex\.layer\b` and stops there. It matches the
**key**, never the value set: a matcher spelled
`org\.scitex\.layer (base|scitex|proxy)` would silently stop catching new
stages once `system-deps` / `python-pkgs` appear, while still passing its own
tests — a guard whose trigger condition is narrower than its stated rule. The
self-test uses a recipe labelled `a-stage-that-does-not-exist-yet` to pin
this.

**Fallback — sac's own directories.** When the recipe cannot be read (a build
driven over ssh on another host, or a path not yet created), the command text
is checked for `scitex_agent_container/containers` or
`.scitex/agent-container/containers`.

**Honest limit:** a build whose recipe is unreadable *and* which names none of
sac's directories passes through. That is the safe direction to be wrong.

### Wrappers the detector must see through

Two real-world spellings defeat a naive check, both taken from
`containers/spartan-sif-bake.sh`:

```sh
:122  APPTAINER="$(command -v apptainer)"          # argv[0] is /usr/bin/apptainer
:275  bash -c "... exec \"$APPTAINER\" build --force \"$PARTIAL_SIF\" \"$CTX/apptainer-$LAYER.def\""
```

argv[0] is an **absolute path**, and the build appears only **inside a quoted
argument** of `bash -c` under `srun`. The detector therefore `basename()`s
argv[0] and re-splits any token that itself mentions a builder. Both forms are
pinned in the self-test — someone reaching for a workaround produces exactly
these.

## Deliberately allowed

- `sac image build …` — the sanctioned path
- `containers/spartan-sif-bake.sh` — the sanctioned remote bake; it does its
  own `$CTX` staging, so it earns the exemption on merit
- read-only verbs: `apptainer inspect --labels|--deffile`, `exec`, `run`
- any build carrying no sac signal

## Overrides

```
SAC_ALLOW_RAW_IMAGE_BUILD=1          # env
# hook-bypass: raw-apptainer-build   # inline marker
```

The hook also **fails open**: if `python3` is unavailable or the decision
engine errors, the command is allowed. A broken guard must not wedge the
agent's Bash tool.

## Verifying it

```bash
bash deny_raw_apptainer_build.sh --self-test
```

20 cases — nine refusals, nine allowances, two bypasses. The pytest suite in
`tests/integration/image_build_hooks/` drives the same script by
repo-relative path against real PreToolUse JSON, so an asset move is caught at
collection time rather than at agent-boot time.
