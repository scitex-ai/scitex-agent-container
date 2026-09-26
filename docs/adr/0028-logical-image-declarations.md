# ADR-0028: Logical SAC image declarations, immutable incarnation evidence

Status: Accepted (2026-09-12)

## Observation

On compute-03, `scitex-hub` failed before tmux creation because its source
spec named a timestamped SIF built on another host. The referenced file had
never been distributed to compute-03, so `resolve_sif` returned `None`. Changing
only that declaration to an artifact present on compute-03 allowed the normal
SAC start path to succeed. This establishes image-reference portability—not
Hermes or tmux—as the cause of that start failure.

## Decision

SAC-owned images are declared by one of three logical names:

- `sac-base`
- `sac-scitex`
- `sac-proxy`

At launch, the name resolves through the corresponding host-local live link
under `~/.scitex/agent-container/containers/`. Build and distribution remain
responsible for installing an immutable artifact and atomically switching that
link. A missing or broken link refuses launch.

Source specs may not name either a timestamped SAC artifact or the SAC live-link
filesystem path. There is no compatibility alias. Absolute paths remain valid
for images not managed by SAC.

Migration is explicit, audited, and dry-run by default:

```bash
sac agents migrate-images --root ~/.dotfiles/src/.scitex/agent-container/agents
sac agents migrate-images --root ~/.dotfiles/src/.scitex/agent-container/agents --apply
sac agents migrate-images --root ~/.dotfiles/src/.scitex/agent-container/agents --check
```

The report names every source path, old reference, logical replacement, and
before/after SHA-256. `--apply` replaces each file atomically. `--check` writes
nothing and exits 1 while any old managed declaration remains. Custom images,
including an unrelated `/tmp/.../sac-base.sif`, are not classified as managed.

The incarnation birth certificate records the resolved immutable path and the
SHA-256 of the bytes used by that launch under
`launch_artifacts.apptainer_image`. Thus Git stores portable intent, while the
PostgreSQL incarnation row stores exact runtime evidence.

## Consequences

A spec copied between hosts predicts the same image family without assuming
identical storage paths or artifact timestamps. Reproduction uses the recorded
digest, and a host that has not received the selected image fails before
creating an agent process.
