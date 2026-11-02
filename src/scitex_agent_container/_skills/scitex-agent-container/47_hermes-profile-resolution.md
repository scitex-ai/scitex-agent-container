---
description: |
  [TOPIC] Hermes profile resolution in SAC
  [DETAILS] Trace spec/to_home profiles into Apptainer, diagnose goal_judge rotation, distinguish project context from runtime settings, and discover hosts and skills through scitex-dev.
tags: [scitex-agent-container-hermes-profile-resolution]
---

# Hermes profile resolution in SAC

For unexpected settings, missing skills, or judge errors, resolve the live
host, spec, workdir, image, HOME, HERMES_HOME and binds before repairs.
Authority checkouts and agent worktrees can differ.

## Resolution and source evidence

Hermes resolves its home through its installed profile/context and
`HERMES_HOME` logic, then reads `config.yaml` there; the ordinary POSIX fallback
is `~/.hermes/config.yaml`. Project
`<root>/.hermes/config.yaml` is not automatically merged. Verify these rules
in the installed image's `hermes_constants.get_hermes_home` and
`hermes_cli.config.get_config_path`, rather than a different host installation.

| Surface | Role | Source to inspect |
|---|---|---|
| Agent spec and engine | Authored route, model, accounts, workdir | `config/_hermes_config.py`, `runtimes/_hermes_failover.py` |
| Shared and agent `to_home/.hermes/` | Deployment inputs; explicit symlinks are dereference-copied | `runtimes/_to_home.py`, `_symlink_resolve.py` |
| `<state_dir>/home/.hermes/` | Generated runtime profile | `runtimes/_hermes_profile.py` |
| `<overlay>/upper/<container-home>/.hermes/` | Generated profile when a directory overlay supplies HOME | `runtimes/_to_home_overlay.py` |
| `/home/agent/.hermes/` inside Apptainer | Active profile in the standard SAC layout | `_apptainer_build_argv.py`, `_apptainer_inner_argv_tui.py` |
| Host operator `~/.hermes/` | Separate profile; not automatically the container's configuration | Actual symlinks, spec inputs, binds, and process environment |

Source filenames above are relative to `src/scitex_agent_container/` (runtime
filenames without a directory remain under `runtimes/`). SAC deploys `to_home`
and then writes its generated Hermes configuration and credential pools to
the runtime home and applicable overlay home. Editing a generated file alone
does not update the authored source and may be overwritten at next launch.

The current launch compiler declares `HERMES_HOME=/home/agent/.hermes` in
`config/_harness_callables.py:_hermes_env_and_binds`. A custom `--home` does not
automatically change this declaration. Verify the effective process environment
and finalized binds before treating a custom HOME as the active Hermes profile.

Directory overlays and runtime homes are alternate container-home backings,
not layers of config precedence. Inspect the actual bind winner. In particular,
do not widen overlay argument parsing to move a live agent to a different HOME:
the resolver documents why some raw-argument spellings retain runtime backing.

This executable example exercises SAC's real directory-overlay resolver using
temporary files, without starting an agent:

```python
# sac-doc-check
from pathlib import Path
from tempfile import TemporaryDirectory

from scitex_agent_container.config import AgentConfig
from scitex_agent_container.config._types import ApptainerSpec
from scitex_agent_container.runtimes._to_home_overlay import (
    resolve_container_home,
    resolve_overlay_upper_home,
)

with TemporaryDirectory() as directory:
    overlay = Path(directory) / "overlay"
    overlay.mkdir()
    config = AgentConfig(name="profile-example")
    config.apptainer = ApptainerSpec(
        raw_args=["--home", "/home/agent", "--overlay", str(overlay)]
    )
    assert resolve_container_home(config) == "/home/agent"
    assert resolve_overlay_upper_home(config) == overlay / "upper/home/agent"
```

## Investigation checklist

- [ ] SAC source: generated route, key selection, failover pools, profile writes.
- [ ] Host operator `<operator-home>/.hermes/`: resolve the actual symlink and
  dotfiles target; do not assume the same checkout or username across hosts.
- [ ] `<registry-root>/agents/_shared/to_home/.hermes/` and each agent's
  `to_home/.hermes/`: verify baseline resolution, explicit symlinks and overrides.
- [ ] `<state_dir>/home/.hermes/`: inspect generated profile and credentials.
- [ ] `<overlay-root>/upper/<container-home>/.hermes/` (including its `upper/home/`
  subtree): identify the actual backing, not a stale alternate tree.
- [ ] Host `<agent-worktree>/.hermes/`: distinguish skills from runtime settings.
- [ ] Inside Apptainer: verify HOME, HERMES_HOME, cwd and visible config path.
- [ ] Inside Apptainer: inspect `/home/agent/.hermes/` or the explicit override.
- [ ] Inside Apptainer: inspect trusted `<project-root>/.hermes/skills/`.
- [ ] Across fleet: discover hosts through `scitex_dev.hosts`, report unreachable
  hosts, and repeat against each running instance's actual image/profile.

Compare sanitized provider/model/endpoint and account labels across `config.yaml`,
`.env`, and `auth.json`. Do not print tokens or dump whole credential stores.
Matching provider names does not prove the main and auxiliary clients use the
same credential or observe a pool rotation.

## Project context and skill discovery

Hermes recognizes the nearest Git root, including `.git` worktree files.
Project `.hermes/skills/` and `.agents/skills/` require project trust through
`skills.trusted_project_dirs`; inspect the installed `agent/skill_utils.py`.
The installed resolver may use session working directory before terminal/process
cwd; record the effective workdir rather than guessing from an attached shell.
Project instruction files such as `.hermes.md`, `HERMES.md`, and `AGENTS.md`
are handled separately by `agent/prompt_builder.py`.

Package `_skills/<package>/` trees are authoritative. Harness directories are
projections: keep content in the package and use `scitex-dev` discovery and
projection auditing rather than maintaining parallel host/container copies.
Installed packages can expose skills through the `scitex_dev.skills` entry
point. Respect `scitex-dev` enable/disable settings and SAC `exclude_skills`;
verify exposure inside the active Hermes home. Availability is different from
loading every full skill into the system prompt.

At launch, `runtimes/_hermes_skills.py` discovers installed packages through
scitex-dev, applies those opt-outs, copies their authoritative skill trees into
each generated profile's `scitex-skills/`, and audits the projection manifest.
`_hermes_profile.py` then registers that relative directory in Hermes'
`skills.external_dirs`. This makes installed skills available inside Apptainer
without requiring the host package source path to be mounted into the image.

Load the owning package's leaf on demand:

```bash
scitex-dev dev skills get --help
scitex-dev dev skills list --help
scitex-dev dev skills get scitex-dev 24_host-registry
scitex-dev dev skills get scitex-dev 36_skill-registry
sac dev skills get 47_hermes-profile-resolution
```

For fleet routing, read scitex-dev's `24_host-registry` skill; for projection
authority and drift checks, read its `36_skill-registry` skill. Retrieve them through its skill dispatcher. `scitex_dev.hosts.list_hosts` and `resolve` own host records; SAC combines
registry SSH routes with configured peers, with explicit SAC peers winning.
Do not copy host inventories.

Configured native `hooks` are retained when SAC regenerates model and credential
settings (`runtimes/_hermes_hook_config.py`). Hook discovery/installation belongs
to scitex-dev and leaf packages; this pass-through neither selects a policy nor
grants Hermes command consent. Install trusted bindings through the native writer,
and verify the effective profile after the next generation.

## Recovery and adjacent diagnostics

For credential, subscription, quota or judge failures, follow
[49_subscription-errors.md](49_subscription-errors.md) and
[48_model-availability.md](48_model-availability.md). Preserve the standing goal;
a restart alone does not prove judge recovery. A banner's provider example is
not necessarily the running route.

CCT ingress is a separate rail: inspect effective MCP assets and external-poller
configuration. `_preflight_cct` refuses dangling CCT state links; restore the
configured target without deleting state or signaling another namespace's PID.
For restart-log formatting and optional/missing pool slot classifications, inspect
`tests/scitex_agent_container/_logging/test__streams.py`, the envrc/operator-secret
regressions, and [40_troubleshooting.md](40_troubleshooting.md).

## Keeping this skill current

Update this leaf when its cited runtime functions change. Run its marked Python
example through `tests/develop/test_documented_examples.py`, which uses scitex-dev
to extract examples from README, docs, and skills. Confirm daily CI collects the test. Use the scitex-dev
skill registry to check discoverability and projection drift. Audits and rendered docs do not prove runtime claims.
