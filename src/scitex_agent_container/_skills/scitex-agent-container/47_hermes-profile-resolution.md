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

Hermes reads provider/judge configuration from `$HERMES_HOME/config.yaml`;
without a profile override its default is `~/.hermes/config.yaml`. Project
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
- [ ] Host operator `~/.hermes/`: resolve symlinks, including a dotfiles target.
- [ ] Shared and agent `to_home/.hermes/`: inspect explicit sources and overrides.
- [ ] `<state_dir>/home/.hermes/`: inspect generated profile and credentials.
- [ ] Overlay `upper/<container-home>/.hermes/`: identify the actual HOME backing.
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

## Restart diagnostics

MCP helper imports must not configure another stderr handler: lifecycle records
use scitex-logging's configured level and format once. The regression lives in
`tests/scitex_agent_container/_logging/test__streams.py`.

The supported-provider secret catalog is optional. Absent default slots and
intentionally empty full-pool slots are debug details, not evidence that the
selected agent lacks credentials. Explicitly requested missing names and an
untrusted pool still warn; required main/judge access is checked by preflight.
Conflicting literal definitions across files warn by name and source path.
Unevaluated shell expressions only produce debug evidence: an alias and a
secret decryption may yield the same value. Never print credential values to
resolve this distinction. These classifications are covered by the envrc and
operator-secret regression tests.

## CCT readiness

Main/judge access does not prove Telegram ingress. Inspect the effective CCT
MCP asset path and external-poller flag; explicit spec entries override shared
MCP settings. Use qualified published assets: development code may start an
extra poller despite that flag. CCT owns its MCP and poller behavior.

SAC's `_preflight_cct` rejects dangling links in `CCT_AGENT_STATE_DIR` before
health or spawn; ordinary missing directories remain valid. Restore the
configured state target without erasing state. Verify host PID ownership and
continued liveness. Never signal another namespace's PID from a shared pidfile.

## Judge failures and verification

Read `logs/agent.log` as well as `errors.log`: goal evaluation logs API failures
at INFO. Correlate the agent session, route, HTTP status, and consecutive failures.
A DeepSeek example in an error banner may be generic guidance, not the live route.

For named custom providers, compare the durable provider slug and legacy
`custom:<slug>` pool identities. Check whether auxiliary cache fingerprints and
recovery select the same populated pool as client construction. Reproduce pool
rotation in a copied temporary Hermes home before modifying live state.

An OpenCode Go 403 saying an active subscription is required is a credential or
entitlement rejection, not proof of network unreachability. A cached judge key
may fail while the main client continues using another key. Do not switch the
judge model or erase cooldowns to conceal the failure.

After a source fix, validate rotation and error recovery, deploy the tested code
to the relevant images/profiles, and verify a real judge verdict. A successful
restart alone is not judge verification. Resume the paused goal through Hermes'
goal command only after recovery; preserve the existing conversation and goal.

## Keeping this skill current

Update this leaf when its cited runtime functions change. Run its marked Python
example through `tests/develop/test_documented_examples.py`, which uses scitex-dev
to extract examples from README, docs, and skills. Confirm daily CI collects the test. Use the scitex-dev
skill registry to check discoverability and projection drift. Audits and rendered docs do not prove runtime claims.
