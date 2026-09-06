<!-- Two axes: the harness (which agent PROGRAM) and the engine (which MODEL
ENDPOINT). Written when the two were split so that any agent can be switched to
a local Qwen in one line, and so that Qwen can be the fleet default. -->

# Harness and engine — the two axes

**`harness:` names the PROGRAM that runs the loop. `engine:` names the MODEL
ENDPOINT that answers it.** They are independent; either can be flipped
without touching the other.

| Axis | Key | Legal values | Where its default lives |
|------|-----|--------------|-------------------------|
| HARNESS — which agent program | `spec.harness` | `anthropic` (alias `claude-code`), `codex`, `openai` (alias `openai-agents`) | required in every spec |
| LAUNCH MODE — how it starts | `spec.runtime` | `tui`, `headless` (aliases `claude-agent-sdk`, `apptainer`) | `tui` |
| ENGINE — which endpoint answers | `spec.engine` | any key in `spec.engines:` **or** the fleet engine library | the library's `engine:` line |

#### Case 1 — a Claude-Code agent following the fleet default

```yaml
spec:
  runtime: tui
  harness: anthropic
  # no `engine:` line -> follows the fleet default
  claude:
    model: ''        # explicit-empty: the ENGINE carries the model
    provider: null   # explicit-null:  the ENGINE carries the endpoint
    account: scitex-01-scitex-ai
```

#### Case 2 — a Codex agent on Qwen

```yaml
spec:
  runtime: tui
  harness: codex        # <- HARNESS. Independent.
  engine: qwen38-27b    # <- ENGINE.  Independent. A PIN.
  claude:
    model: ''
    provider: null
    account: ''
```

#### Case 3 — the one-line switch

```diff
 spec:
   runtime: tui
   harness: anthropic
+  engine: qwen38-27b
   claude:
```

Flipping the HARNESS instead is a separate, also-one-line edit — which is the
proof the two axes are orthogonal:

```diff
-  harness: anthropic
+  harness: codex
```

#### The fleet engine library — `$SCITEX_DIR/agent-container/engines.yaml`

Source of truth: `~/.dotfiles/src/.scitex/agent-container/engines.yaml`, deployed
like every other spec. `$SCITEX_DIR/agent-container/` is a **synced copy**, never
hand-edited. `$SAC_ENGINES_FILE` overrides the path for one process (ops/test
only — never a spec surface).

```yaml
apiVersion: scitex-agent-container/v3
kind: EngineLibrary

engine: claude-opus            # THE FLEET DEFAULT. One line.

engines:
  claude-opus:
    model: opus[1m]
    provider: anthropic
  qwen38-27b:
    model: qwen38-27b
    provider:
      base_url: http://100.64.0.1:18772     # the GATEWAY (it load-balances the replicas)
      auth_token_env: SCITEX_GENAI_GATEWAY_API_KEY
    reasoning_effort: low
    max_context_tokens: 1048576
```

**Moving the whole fleet onto Qwen is one line** — change `engine: claude-opus`
to `engine: qwen38-27b`, commit, deploy. Every agent that has not pinned itself
starts on Qwen at its next start. Reverting is the same one line. A missing
library is legal and silent; an unreadable or self-contradictory one is a load
error naming the path, never an empty default.

An entry states **no `harness:`**, deliberately: an engine that states none
states *no opinion* and inherits the spec's. That is what lets ONE `qwen38-27b`
entry serve a Claude-Code agent and a Codex agent unchanged.

#### Precedence — five steps, first hit wins, nothing falls back

1. `--engine <key>` on the command line (start time only).
2. `spec.engine: <key>` — the spec pins itself, immune to any fleet edit.
3. A legacy backend declaration in `spec.claude` (`model` and/or `provider`) —
   **also a pin**. COMPAT ONLY; deleted when the migration ends.
4. `engine:` in the fleet engine library — the fleet default.
5. Nothing declared anywhere — the harness's own built-in backend.

Step 3 sits **above** step 4 on purpose: every spec deployed today carries a
legacy declaration, so writing the library the first time changes nothing for
anybody. The fleet default becomes real one agent at a time, as the migration
sweep clears each legacy pin.

`sac agents explain <agent>` prints the resolved engine, **which of these steps
chose it**, and the library's path + mtime + content hash — the only defence
against a synced library that has diverged between hosts.

#### Harness × engine — what refuses, and when

Refused at **start time**, naming the combination and the remedy (these used to
fail deep in argv construction, after the agent had already been stopped):

| Harness | Engine | Verdict |
|---------|--------|---------|
| `anthropic` | provider-bearing | honourable |
| `anthropic` | no provider (OAuth) | honourable |
| `codex` | provider-bearing | honourable |
| `codex` | no provider | **NOT honourable** — codex has no OAuth path to fall back on |
| `codex` | provider literally named `codex` | **NOT honourable** — the two-axis name clash |
| `openai` | anything | **NOT honourable** — no lifecycle adapter (card `sac-v4-layering-refactor-harness-runtime-inference-20260813`) |
| undetermined | anything | `could-not-tell` — starts with a LOUD warning; never rendered as "fine" |

#### `wire` is NOT a field

An engine declares one endpoint; the harness descriptor renders it into whatever
that program's process form is (`ANTHROPIC_BASE_URL`, `-c model_providers…`,
`OPENAI_BASE_URL`). There is deliberately no `wire:` / `protocol:` key. **The
written condition under which one becomes justified:** a single harness that can
speak two protocols to the *same* endpoint, where the choice is not derivable
from the harness. Until that exists, adding the field would be a menu with one
dish — and stating the condition here is what stops someone adding it later
without knowing it was a decision.

