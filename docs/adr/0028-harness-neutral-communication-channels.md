# ADR-0028: Communication channels belong to the agent, not the harness

Status: Accepted

Date: 2026-09-13

## Observed problem

The canonical v3 surface placed `channels` under each
`spec.available_harnesses.<name>` entry. The loader then copied the selected
entry into `ClaudeSpec.channels`. Hermes consumed that Claude-named carrier,
while the Codex and OpenAI turn drivers explicitly described the value as a
Claude-only adapter and ignored it. Switching harnesses could therefore change
communication behavior even when the agent identity, SAC inbox, Cards inbox,
and A2A endpoint were unchanged.

## Decision

The authored declaration is `spec.comms.channels`.

```yaml
spec:
  comms:
    channels:
      - server:sac
      - server:scitex-cards
    outbound:
      siblings: allow
      parent: allow
    inbound:
      siblings: allow
      parent: allow
    a2a:
      listen: true
```

`available_harnesses` contains only harness behavior. A `channels` field in a
harness entry is rejected with a relocation error. The loader projects the
neutral list into the existing internal runner carrier until that internal
type is renamed; this projection is not an authored compatibility surface.

SAC, Cards, and CCT messages retain their source-specific adapters, but all
inbound delivery terminates at the selected harness's turn endpoint and uses
the shared exchange ledger for accepted, final, and failed states. `steer` is
the interactive delivery behavior; queueing and interruption are not inferred
from the transport name.

## Migration boundary

Direct legacy `spec.claude.channels` remains readable until deployed specs are
migrated. New canonical specs using `available_harnesses` must declare
`spec.comms.channels`, including an explicit empty list when no inbound channel
is wanted. After the fleet migration is verified on every host, the legacy
reader and `ClaudeSpec.channels` carrier can be removed together.

## Consequences

- One channel list applies when switching among Claude Code, Hermes, and Codex.
- AgentCard synthesis and A2A executor construction prefer the neutral block.
- Twin derivation removes a Telegram channel from the neutral list, preserving
  the one-token/one-poller rule.
- This schema move does not by itself prove every adapter's live delivery.
  Harness-specific integration tests and live exchange receipts remain the
  deployment gate.
