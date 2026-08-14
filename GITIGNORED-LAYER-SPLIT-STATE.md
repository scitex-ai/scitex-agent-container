# WIP: `sac image build` four-layer split — resume state

**Worktree:** `/home/ywatanabe/proj/scitex-agent-container/.worktrees/image-layer-split`
**Branch:** `feat/image-build-four-layers` (off `develop`)
**Repo note:** `~/proj/sac` is a SYMLINK to `~/proj/scitex-agent-container`. Same repo.

## Operator request (verbatim intent)

1. `sac agents start scitex-agent-container -y` printed two warnings; "could you fix the problem?"
   - `sac-drift: spec source ... could not be drift-checked (not a git repo ...)`
   - `WARN: gdu not found; falling back to du+fd for .claude audit`
2. "sac image build should have four options: 1. system-deps 2. python-pkgs 3. base 4. scitex"
3. Operator repeatedly sent bare "**2**" (~10x). Clarified via AskUserQuestion →
   **"Layer 2 = python-pkgs"**. That layer is the operator's focus.

## Design (decided)

Split the old 898-line `apptainer-base.def` into a 4-layer chain:

| # | layer | def file | Bootstrap / From | contents (old base.def sections) |
|---|-------|----------|------------------|----------------------------------|
| 1 | `system-deps` | `apptainer-system-deps.def` | `docker` / `ubuntu@sha256:5616...` | 1, 1a, 1b, 1c, 2, 2a, 3, 3a, 4, 4a, 4c, 5, **7 (rtk)** |
| 2 | `python-pkgs` | `apptainer-python-pkgs.def` | `localimage` / `./sac-system-deps.sif` | 6, 6a, 6b, 6c + claude symlink repoint. Has the `%files` sac-src copy. |
| 3 | `base` | `apptainer-base.def` (now thin) | `localimage` / `./sac-python-pkgs.sif` | 8 (versions manifest) + env/labels/help |
| 4 | `scitex` | `apptainer-scitex.def` (unchanged) | `localimage` / `./sac-base.sif` | scitex[all] |

`proxy` (`apptainer-proxy.def`) is KEPT as a buildable non-stack sidecar
(bootstraps from the registry, not part of the chain). Operator listed only
four, but deleting proxy would remove a working capability — flag it, don't drop it.

Key apptainer fact driving the design: `%environment`, `%labels`, `%help`
OVERWRITE the parent's `/.singularity.d/env/90-environment.sh` on a
localimage build — so every layer declares its **full** PATH, not a delta.
(`apptainer-scitex.def` currently declares only a delta — pre-existing wart,
not touched.)

## DONE so far

- [x] worktree + branch created
- [x] `apptainer-system-deps.def` written (new file, ~330 lines)
- [x] `apptainer-python-pkgs.def` assembled = head + `sed -n '332,839p' <old base.def>` + tail
      (599 lines; byte-exact slice so the 200-line embedded staleness-gate heredoc survived)
- [x] `apptainer-base.def` rewritten thin (~105 lines)

- [x] Fixed stale cross-ref in `apptainer-python-pkgs.def` (`section 3a` now
      names apptainer-system-deps.def)
- [x] `cli_pkg/_image_layer_chain.py` — NEW module owning the topology:
      `LAYER_DEFS`, `STACK_ORDER`, `BOOTSTRAP_PARENT`, `BootstrapSifMissing`,
      `resolve_bootstrap_sif` (now a real 4-link chain, parent-aware error text).
      Created because `_image_source_build.py` was ALREADY 528 lines (over the
      512 cap) and the line-limit hook blocked the edit. `GITIGNORED/REFACTORING.md`
      documents the split — **DELETE IT when the refactor is verified** (it
      suspends the line-limit hook while present).
- [x] `_image_source_build.py` — removed both symbols, re-exports them from
      `_image_layer_chain` (kept in its `__all__`). Now 472+9 lines, under cap.
- [x] `image_group.py` — `_LAYERS`/`_STACK_ORDER` now alias the chain module;
      added `_image_layer_chain` to the `from . import (...)` block; rewrote the
      `image_build` docstring with the four-layer stack + per-layer examples.

## TODO (not yet done)

- [ ] **RUN THE TESTS.** pytest inside this worktree REQUIRES PYTHONPATH or the
      `enforce_pytest_worktree_source.sh` hook blocks it (editable install points
      at the MAIN checkout, so a green run would test the wrong code):
      ```
      PYTHONPATH=/home/ywatanabe/proj/scitex-agent-container/.worktrees/image-layer-split/src \
        /home/ywatanabe/proj/scitex-agent-container/.venv/bin/python -m pytest \
        tests/scitex_agent_container/cli_pkg/test_image_group.py \
        tests/scitex_agent_container/cli_pkg/test__image_source_build.py \
        tests/integration/test_apptainer_base_def_playwright_libs.py \
        tests/integration/test_apptainer_base_def_cross_host_tools.py \
        tests/integration/test_apptainer_base_def_scitex_todo.py \
        tests/integration/test_apptainer_def_openai_agents.py -q
      ```
- [ ] Contract tests read `apptainer-base.def` and assert content that has MOVED.
      Cleanest fix: add a shared helper returning the CONCATENATION of the three
      stack defs (the assertion is about the `:base` IMAGE, which is now the
      composition), then repoint these:
      - `tests/integration/test_apptainer_base_def_playwright_libs.py` (→ system-deps)
      - `tests/integration/test_apptainer_base_def_cross_host_tools.py` (→ system-deps)
      - `tests/integration/test_apptainer_base_def_scitex_todo.py` (→ python-pkgs)
      - `tests/integration/test_apptainer_def_openai_agents.py` (→ python-pkgs)
      Also check: `tests/scitex_agent_container/cli_pkg/test_image_group.py:326`,
      `test__image_source_build.py` (lines 89/141/608/1003),
      `tests/integration/image_build_hooks/test_deny_raw_apptainer_build.py`.
- [ ] Docs: `docs/images.md`, `docs/sphinx/images.md`, `docs/directories.md`,
      `src/scitex_agent_container/_skills/scitex-agent-container/24_image-build.md`,
      `containers/spartan-sif-bake.sh`, `_drift/versions.py`, `_image_repro_build.py`.
- [ ] CHANGELOG entry.

## TEST PROGRESS

- Run 1 (before any test fix): **51 failed / 118 passed**
- Run 2 (after contract-test repointing): **13 failed / 155 passed** — all 13 in
  `test_image_group.py`, ALL one root cause: those tests build the DEFAULT layer
  (`base`), which now requires `sac-python-pkgs.sif`, so the CLI fails loud
  before reaching the builder.
- Fix applied (commit pending at time of writing): added a `staged_python_pkgs_sif`
  fixture to `test_image_group.py` that stages the prerequisite (timestamped SIF +
  inner boot symlink) under `ig._CONTAINERS_DIR`, and wired it into the 12 tests
  that build the default layer. Also renamed
  `test_build_base_passes_none_bootstrap_sif` →
  `test_build_system_deps_passes_none_bootstrap_sif` and pointed it at
  `system-deps` (the property moved with the bottom of the stack, it did not vanish).
- Run 3: **168 passed, 0 failed in 4.08s** — GREEN across all six affected files
  (test_image_group, test__image_source_build, playwright_libs, cross_host_tools,
  scitex_todo, openai_agents). Verified with
  `PYTHONPATH=<worktree>/src` so the worktree's code was actually exercised.

- Run 4 (after the docstring trim that took image_group.py 517 -> 510):
  **168 passed in 3.82s**. Still green.

### The full-dir timeout — DIAGNOSED, and NOT ours

`pytest <worktree>/tests/integration/ --timeout=20` stalls at **67%**, on
`test_listen_startup_sync_no_bind_block.py` (emits `E`, then hangs). Everything
image-related passes at 31%. That file is in the `listen` SSE/bind subsystem;
NOTHING in this branch's diff touches `_listen`. Treat the full-dir timeout as
an unrelated pre-existing hang, not a regression from the layer split.
(Isolation run of that single file was still in flight at last write — confirm
it hangs standalone before filing.)

DONE since: docs/images.md, docs/sphinx/images.md, CHANGELOG.md all updated to
the four-layer chain. GITIGNORED/REFACTORING.md deleted (limit re-armed,
image_group.py 510, _image_source_build.py 481).

STILL TODO: `_skills/scitex-agent-container/24_image-build.md` (lines 4, 55-56,
104 still say two layers), `docs/directories.md`, `containers/spartan-sif-bake.sh`,
`_drift/versions.py`, `_image_repro_build.py`.

DO NOT touch `test_build_scitex_errors_loud_when_base_sif_missing` — it needs
`sac-base.sif` ABSENT. The fixture only stages `sac-python-pkgs.sif`, so it is safe.

## AUDIO / PHONE — **CORRECTION: IT IS POSSIBLE.** (3rd /speak-and-call)

My first two conclusions ("cannot speak or call") were WRONG in an important
way. I checked for an INSTALLED `scitex` CLI and stopped there. The real
situation:

- `~/proj/scitex-notification` (v0.2.9) EXISTS as a source checkout, just not
  installed into the sac venv.
- Its `_backends/_telegram.py` AND `_backends/_twilio.py` both use **stdlib
  urllib only — no SDK dependency**. (`twilio` pkg is NOT importable, and that
  does NOT matter.)
- Its only hard deps are `click` + `scitex_dev`, BOTH already importable in
  `/home/ywatanabe/proj/scitex-agent-container/.venv`.
- Twilio + Telegram credentials ARE set in the environment:
  `SCITEX_NOTIFY_TWILIO_{TO,TOKEN,FROM}`, `SCITEX_NOTIFICATION_TWILIO_{SID,FROM}`,
  `SCITEX_NOTIFICATION_TELEGRAM_{TOKEN,BOT_TOKEN,CHAT_ID}`.
  (Do NOT echo their values — a hook redacts secrets; use
  `~/.claude/hooks/lib/env-check VARNAME` to confirm set-ness.)

=> RUN IT FROM SOURCE, no install needed:
```
PYTHONPATH=/home/ywatanabe/proj/scitex-notification/src \
  /home/ywatanabe/proj/scitex-agent-container/.venv/bin/python -m scitex_notification <verb> ...
```
Operator phone: 080-4022-3567. Skill says PHONE_CALL_THRESHOLD=7 and
"Do never hesitate to make a phone call to me! This is our strategy."
ALWAYS send SMS together with a call.

LESSON (for GITIGNORED/tasks/lessons.md): "command not found" is not the same
as "capability absent". Check the source checkouts under ~/proj/ before
declaring a capability impossible — the fleet keeps packages there uninstalled.

### VERIFIED RESULTS (2026-08-14)

- `call` verb: **WORKS.** "Call initiated successfully" — operator's phone rang.
- `send-sms`: **FAILS**, and NOT for the reason the CLI claims.
  All four `SCITEX_NOTIFICATION_TWILIO_{SID,TOKEN,FROM,TO}` are set correctly
  (SID 34ch, TOKEN 32ch, FROM +16562554597, TO +818040223567).
  Raw HTTP POST to the Twilio Messages API returns:
      HTTP 400 {"code":21408,"message":"Permission to send an SMS has not been
      enabled for the region indicated by the 'To' number: +81804022XXXX"}
  => Twilio account lacks SMS **geo-permission for Japan**. Fix is in the
  Twilio console (Messaging -> Geo Permissions -> enable Japan). NEEDS THE
  OPERATOR'S LOGIN; an agent cannot do it.
  Until then the skill's "ALWAYS send SMS together with phone call" rule
  CANNOT be satisfied on this account. Voice works, SMS does not.

### BUG TO FILE against scitex-notification (v0.2.9)

`send-sms` prints "Failed to send SMS / Check SCITEX_NOTIFICATION_TWILIO_* env
vars are set correctly." and SWALLOWS the actual Twilio response body. The env
vars were fine; the real cause was geo-permissions. That misleading message
cost two tool calls chasing the wrong thing, and it is a fail-loud violation
(the operator's `02_quality_07_crash-early-crash-loud.md` /
`no-fallbacks` rules). Fix: surface the HTTPError body + Twilio error code.

## (superseded) earlier note — NOT POSSIBLE FROM THIS SESSION

`/speak-and-call` was requested. Verified on scitex-compute-04:
- no `scitex` CLI (only scitex-agent-container / -container / -dev / -ssh in .venv)
- no `espeak-ng`, no `aplay`/`paplay`/`ffplay` (`pico`/`piconv` are Perl/Pine, NOT pico2wave)
- no `mcp__scitex__audio_speak` / `notification_call` / `notification_sms` in this
  session's tool set
Only channel available is the harness `PushNotification` tool. Do NOT claim to have
spoken or called — that is exactly the false-positive the operator's
`02_quality_01_no-false-positives.md` forbids.

## The two warnings

### 2. sac-drift NOT_A_REPO — ROOT CAUSE FOUND (2026-08-14)

`~/.scitex/agent-container/agents/scitex-agent-container/spec.yaml` is a REAL FILE
in a plain directory. `git -C <that dir> rev-parse --show-toplevel` →
`fatal: not a git repository`. Other agents' specs are symlinked into a checkout
(`_drift/_local.py::spec_source_repo` resolves symlinks precisely so a
`~/.scitex/...` symlink is followed into `~/.dotfiles/...`).

So drift is genuinely UNKNOWABLE for this agent — the warning is CORRECT, not a bug.
Fix is a provisioning choice, needs operator input:
  (a) move the spec into a git checkout and symlink it (matches every other agent), or
  (b) accept NOT_A_REPO for locally-authored specs and stop warning for them.

### (original notes below)

1. **gdu** — `_workdir/_audit.py:629` warns when `shutil.which("gdu")` is None.
   `apptainer-base.def` (now `apptainer-system-deps.def` §4c) DOES bake dundee/gdu
   v5.31.0 into the SIF. The warning fired because `sac agents start` ran the
   audit **on the HOST** (`scitex-compute-04`), where gdu is not installed —
   confirmed: `which gdu` → exit 1 on this host. So the message's own advice
   ("the SIF's apptainer-base.def is expected to bake gdu in") is MISLEADING on
   a host-side run. Fix candidates: (a) install gdu on the host, (b) make the
   warning name the host-vs-SIF distinction. Decide with operator.
2. **sac-drift NOT_A_REPO** — `_drift/_local.py:318` `drift_warning_lines()`.
   Spec source dir is not inside a git working tree. Need to find what path is
   being used as the spec source for agent `scitex-agent-container` and why it
   is not in a repo.

## House rules that bit / apply

- Bash must be backgrounded or `timeout 7`-bounded (force_background_bash.sh hook).
- `grep -r` blocked; use `/home/ywatanabe/.local/bin/rg` by ABSOLUTE path
  (`rg` is at `~/.local/bin/rg`, NOT `/usr/bin/rg`).
- Tracked-file edits only inside a linked worktree under `.worktrees/`.
- No `Co-Authored-By` trailer on commits.
