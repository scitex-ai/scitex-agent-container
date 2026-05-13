# ADR: Isolation Hardening (2026-05-13)

**Status:** Accepted.
**Context:** Agent symlink incident in `scitex-stats-auditor` proof of
concept; sac's positioning vs. Clew reproducibility verification.

## Problem

On 2026-05-13 the first per-package auditor (`scitex-stats-auditor`)
ran under Apptainer with `sac-base.sif + overlay` and reported:
"the project venv targets `/opt/python3.12/bin/python3.12`, which is
missing on this host; I repaired it by symlinking it to
`/usr/bin/python3.12`." The agent's mental model was that it had
patched the host. Investigation showed the symlink only landed in the
container's overlay — the host was untouched. But two systemic gaps
were exposed:

1. **The agent thought it had host write access.** Apptainer's
   defaults make the container/host boundary porous enough that an
   agent can't distinguish "I patched the container's view of /opt"
   from "I patched the host."
2. **Operator-side prompts can't enforce isolation.** The prompt told
   the agent to be read-only; the agent ignored it. Prompt-level
   guardrails are not a security mechanism.

The deeper issue: sac's stated positioning is reproducible-by-default,
but its actual default behavior inherited Apptainer's HPC-convenience
defaults (auto-bind `$HOME`, `/tmp`, `/proc`, `/sys`, `/dev`; inherit
all env vars; share host namespaces). Convenience-first defaults are
upside-down for an agent runtime where the container is supposed to
be the security boundary.

## Decisions

### D1. Hardened isolation by default; `relaxed: true` to opt out.

`spec.apptainer.relaxed: false` is the default. sac auto-prepends
`--containall` (filesystem isolation), `--cleanenv` (environment
isolation), and `--writable-tmpfs` (when no overlay is declared) to
the apptainer argv.

`relaxed: true` is an explicit opt-out for HPC-style convenience use
cases. Agents started with `relaxed: true` are **outside the Clew
verification chain** — their runs cannot be attested as reproducible.

**Rationale.** sac's differentiation against LangGraph / CrewAI /
AutoGen is "spec.yaml declares isolation; mechanism enforces it;
external verifier can attest it." Default-strict supports that
thesis directly. HPC users can opt in to the legacy behavior with
one line, and they pay the cost of falling out of the verification
chain (which they typically don't need anyway).

### D2. Universal preflight via `$HOME`-visibility check, not per-path enumeration.

The preflight that sac auto-injects before user `startup_commands` is:

```bash
test "$(id -u)" != "0" || (echo 'ERROR: running as root' && exit 1)
test ! -d "$HOME" || (echo 'ERROR: host $HOME visible — isolation breach' && exit 1)
```

**Rationale.** Per-path enumeration (`test ! -e $HOME/.gitconfig`,
`test ! -e $HOME/.ssh`, …) has unbounded false-negative risk: every
new credential store added in the next decade (`.kube/config`,
`.docker/config.json`, `.netrc`, `.npmrc`, `.pypirc`, `.gnupg/`,
`~/.config/anthropic/`, `~/.bash_history` with embedded secrets, …)
requires a new line. The `$HOME`-visibility check covers all of them
at once.

Under `--containall`, `$HOME` is NOT auto-bound — it should be
invisible inside the container. If the check fails, either `--containall`
isn't in effect or an operator-declared bind brought it in. Either way,
the agent shouldn't start.

**Operator opt-out** for paths that legitimately need to be visible:

```yaml
spec:
  apptainer:
    preflight_allow:
      - "$HOME/.gitconfig"   # acknowledged: agent needs read-only gitconfig
```

The opt-out is declared per-path, not as a blanket "disable preflight."

### D3. AgentCard exposes structured isolation block, not a flat enum.

Instead of `isolation_level: hardened | relaxed | custom`:

```json
"x-scitex-agent-container": {
  "isolation": {
    "level": "hardened",
    "containall": true,
    "cleanenv": true,
    "writable_tmpfs": false,
    "preflight_passed": ["uid-nonzero", "no-host-home"],
    "preflight_allowed": [],
    "binds_count": 3,
    "binds_writable_count": 0
  }
}
```

`level: hardened` is the human shorthand for "all booleans true +
`preflight_allowed: []`". External verifiers (Clew, orochi attestation)
read the structured booleans to attest specific properties.

**Rationale.** A flat `custom` label hides what's custom about it.
A run with `preflight_allow: [$HOME/.ssh]` and a run with
`preflight_allow: [$HOME/.aws]` are both `custom` under the enum but
have very different security profiles. Clew's verification chain wants
to attest specific properties: "did this run set containall? did it
allow any preflight bypasses?" The structured block answers those
directly.

## Considered and rejected

### Rejected: prompt-level isolation guarantees.

"Tell the agent in the prompt to be read-only." The PoC proved this
fails — the agent acknowledged the rule and then violated it. Prompts
are not a security mechanism. Mechanism-level enforcement is
non-negotiable for the Clew thesis.

### Rejected: `test -w <path>` permission-based preflight.

`test ! -w /opt` would fail inside any container with a writable
overlay shadowing `/opt`, even when no host damage is possible. The
intent (catch leaks) and the test (catch any write capability) don't
align under apptainer's overlay model. Existence-based checks on
identity files (`$HOME` visibility) avoid this entirely.

### Rejected: enumerated per-path preflight (`.gitconfig`, `.ssh`, `.aws`, ...).

Forever incomplete; every new credential cache added by tooling
elsewhere requires a sac patch. The `$HOME`-visibility check covers
the same surface in one assertion.

### Rejected: `isolation_level: hardened | relaxed | custom` flat enum.

Loses verifier resolution. Two `custom` runs with very different
preflight_allow sets become indistinguishable on the card.

## Implementation

| Layer | Where | Status |
|---|---|---|
| `apptainer.relaxed: false` default | `config/_types.py::ApptainerSpec` | ✅ shipped earlier today |
| `--containall` auto-prepended | `runtimes/_apptainer_runtime.py` | ✅ shipped |
| `--cleanenv` auto-prepended | same | ⏳ pending |
| `--writable-tmpfs` when no overlay | same | ⏳ pending |
| Universal preflight injection | runtime wraps inner cmd with `sh -c "preflight && exec <inner>"` | ⏳ pending |
| `spec.apptainer.preflight_allow` field | `config/_types.py::ApptainerSpec` | ⏳ pending |
| AgentCard structured `isolation` block | `a2a/_card.py::project_card` | ⏳ pending |
| Regression tests | `tests/.../test__apptainer_runtime.py` + `tests/.../a2a/test__card.py` | ⏳ pending |

## Consequences

**Positive.**
- sac becomes the only A2A-compatible agent runtime that mechanically
  enforces isolation by default. Clear differentiation from LangGraph
  / CrewAI / AutoGen (no isolation concept) and from Docker (isolation
  exists but not declared / not attestable).
- Clew can build verification chains that attest specific isolation
  properties without sac-internal introspection.
- The `scitex-stats-auditor` symlink-confusion class of incidents
  becomes impossible: the agent can't "fix" the host because the host
  isn't reachable.

**Negative / tradeoffs.**
- Existing agents that worked under the old defaults will fail-fast
  if they implicitly relied on `$HOME` auto-bind. Operators must
  either declare the bind explicitly OR opt in to `relaxed: true`.
- The `--cleanenv` default removes `$PATH` inheritance; sac has to
  inject the container's expected `$PATH` itself.
- HPC-style "just run my command inside the container with my home
  visible" is now a two-step (write `relaxed: true`); intentional.

## Addendum: D2 refinement (2026-05-13 evening)

Initial D2 design proposed `test ! -d "$HOME"` as a universal
invariant. Pre-implementation verification against the
scitex-stats-auditor spec.yaml exposed an Apptainer-specific edge
case: **Apptainer creates the entire directory path of every bind
target as scaffolding**, so a bind like
`/home/$USER/proj/scitex-stats:/home/$USER/proj/scitex-stats:ro`
causes `/home/$USER` (== `$HOME`) to exist as a directory inside the
container — even under `--containall`, with no credential files
visible. The D2 check would false-fire on every agent that mirrors
host paths into the container.

Two solutions were considered:

- **Bind-aware preflight** (sac generates the preflight at runtime,
  knowing what binds it's about to create, and the preflight checks
  `$HOME` contents are a subset of the declared bind scaffolding).
  Rejected: dynamic preflight is harder to integrate into Clew's
  verification chain — verifier has to attest the *generator*, not
  just the executed script.
- **Container-canonical paths** (bind targets MUST use container-
  side conventional roots — `/srv/`, `/work/`, `/opt/`, `/data/` —
  never host-mirroring paths). Accepted.

### D4. Bind targets MUST be container-canonical paths.

Bind targets that mirror host paths (`/home/`, `/Users/`, `/root/`,
absolute Windows-style paths) are deprecated. Bind targets MUST live
under conventional container roots: `/srv/`, `/work/`, `/opt/`,
`/data/`.

Spec.yaml convention:

```yaml
spec:
  apptainer:
    binds:
      - $HOME/proj/scitex-stats:/srv/sources/scitex-stats:ro
      - $HOME/proj/scitex-dev:/srv/sources/scitex-dev:ro
```

Inside the container nothing under `/home/$USER` appears, so:

1. The D2 preflight (`test ! -d "$HOME"`) stays static and universal.
2. spec.yaml is **operator-agnostic** — the same spec runs cleanly
   for any user.
3. Verification chain receives a static preflight script with a
   stable sha256; no meta-verification required.

**Rationale, beyond the technical fix.** Container-canonical paths
match Docker / OCI best practice and break the "works on my
machine" failure mode (every agent that hardcodes
`/home/ywatanabe/proj/...` is operator-bound). The shared-path
convention was an HPC convenience artifact; Clew's reproducibility
context inverts it.

**sac-side enforcement (planned).** `sac agents check <name>` will
emit a warning when a bind target starts with `/home/`, `/Users/`, or
`/root/`. Future strict mode (`sac.audit.strict_binds: true`) makes
it an error.

**Convenience.** The runtime sets `$SAC_WORKDIR=/srv/sources` inside
the container (when any bind targets land there), so `startup_prompts`
and operator scripts can use `cd $SAC_WORKDIR/<pkg>` without
hardcoding paths.

### Order of execution

1. ADR addendum (this section) — done.
2. scitex-stats-auditor spec.yaml — bind targets translated to
   `/srv/sources/...`; startup_prompts updated to reference the
   container paths.
3. Implementation: D1 + D2 (static check) + D3 + D4 (CLI validator).
4. Restart scitex-stats-auditor against hardened sac; verify the
   static preflight passes end-to-end.
5. Preserve session.jsonl + preflight result for Clew supplementary.

## References

- [`docs/isolation.md`](../isolation.md) — the 10-category leak catalog this ADR's decisions close.
- [`docs/spec-reference.md`](../spec-reference.md) — `spec.apptainer.relaxed`, `spec.apptainer.preflight_allow`.
- The 2026-05-13 scitex-stats-auditor incident — session.jsonl at
  `~/.scitex/agent-container/runtime/scitex-stats-auditor/` for the
  full trace.
