# spec-reference.md ↔ source audit

Verification pass of `docs/spec-reference.md` against
`src/scitex_agent_container/config/` (loader + parsers + validator).
Kept in `docs/` (not `docs/internal/`) because it's evidence the
operator can re-run when re-doing the audit; it doesn't ship in the
Sphinx build (`docs/sphinx/conf.py` doesn't include `*.audit.md`).

Legend: `✓` documented correctly · `✗` documented but mismatched/missing
in code · `!` implemented but undocumented · `?` design-only / unclear.

## Top-level

| Field | Documented | Implemented | Verdict | Source path |
| --- | --- | --- | --- | --- |
| `apiVersion` | `scitex-agent-container/v3` only; v1/v2 raise | `_VALID_API_VERSIONS = ("scitex-agent-container/v3",)`; rejected otherwise | ✓ | `config/_validation.py:48,140-144` |
| `kind` | `Agent | AgentProxy` | `_VALID_KINDS = {"Agent","AgentProxy"}` | ✓ | `config/_validation.py:55,147-149` |
| `metadata.name` | "no metadata.name" (dir-as-SSoT) | Explicitly rejected with hint | ✓ | `config/_validation.py:156-162` |
| `metadata.labels.*` | drives AgentCard | `parse_*` consumers / `a2a/_card.py` | ✓ | `a2a/_card.py`, `_loaders.py:213` |

## `spec` top-level

| Field | Documented | Implemented | Verdict | Source path |
| --- | --- | --- | --- | --- |
| `spec.runtime` | `apptainer` REQUIRED | Optional; empty defaults to `apptainer`; non-empty must equal `apptainer` | ✗ (REQUIRED → optional with default) | `config/_validation.py:201-207`, `_loaders.py:269` |
| `spec.workdir` | path, default `~/.scitex/agent-container/runtime/agents/<name>/` | Same default | ✓ | `config/_loaders.py:36,219-221` |
| `spec.dot_claude` | path merged into `<workdir>/.claude/` | Stored as string; default `""` (not auto-discover) | ✗ (no auto-discover of sibling) | `config/_loaders.py:302`, `_validation.py:93` |
| `spec.python-venv` | string \| list; `auto` probes `~/.venv-3.11`, `~/.venv` | Matches: `_resolve_python_venv` + `_resolve_venv` | ✓ | `config/_loaders.py:48,57-71,90-139` |
| `spec.env-file` | string \| list of dotenv paths | Parsed by `_parse_env_files`; **NOT in `_KNOWN_SPEC_KEYS`** | ✗ (validator rejects as unknown) | `config/_loaders.py:142-161`; missing from `_validation.py:64-99` |
| `spec.user` | container user override | Validated: `"" | "host" | "<uid>:<gid>"` | ✓ | `config/_validation.py:300-310` |
| `spec.multiplexer` | `tmux | screen`, default `tmux` | Read by loader; **NOT in `_KNOWN_SPEC_KEYS`** | ✗ (validator rejects as unknown) | `config/_loaders.py:295`; missing from `_validation.py:64-99` |
| `spec.host` / `spec.hosts` | mutually exclusive | Enforced | ✓ | `config/_validation.py:312-348` |
| `spec.startup_commands[]` | list of shell commands | Each item is a dict `{delay,command}` (not bare strings) | ✗ (shape understated) | `config/_parsers/_startup.py:14-23` |
| `spec.startup_prompts[]` | list of strings | Matches | ✓ | `config/_loaders.py:261-262` |
| `spec.screen.name` | (undocumented) | Loader reads `spec.screen.name`; key in `_KNOWN_SPEC_KEYS` as legacy | ! | `config/_loaders.py:224-225`, `_validation.py:70` |
| `spec.session` (top-level shortcut) | (undocumented) | Top-level `session:` overrides `claude.session` | ! | `config/_parsers/_claude.py:12-20`, `_validation.py:86` |

## `spec.apptainer`

| Field | Documented | Implemented | Verdict | Source path |
| --- | --- | --- | --- | --- |
| `image` | REQUIRED `.sif` path | Optional; empty default | ✗ (REQUIRED → optional with empty default) | `config/_parsers/_apptainer.py:107` |
| `overlay` | path | str default `""` | ✓ | `_apptainer.py:117` |
| `binds[]` | `host:container[:ro|rw]`; src expansion; dst must be absolute | Matches; also accepts legacy `{src,dst,mode}` dicts | ✓ (legacy dict form is undocumented `!`) | `_apptainer.py:46-103` |
| `env` | KV dict | Matches | ✓ | `_apptainer.py:34-37,109` |
| `nv` / `rocm` | bool, mutually exclusive | Parsed; **mutual exclusion NOT enforced in code** | ✗ (mutex claim) | `_apptainer.py:115-116` (no exclusion check) |
| `raw_args[]` | list of strings | Matches | ✓ | `_apptainer.py:104-105` |
| `relaxed` | bool (default `false`) | Not parsed in `ApptainerSpec` (`relaxed` isn't a field) | ✗ (DESIGN — not yet implemented in parser) | absent from `_apptainer.py` / `_types.py` |
| `fakeroot` | bool (default `false`) | Not parsed in `ApptainerSpec` | ✗ (DESIGN — not yet implemented in parser) | absent from `_apptainer.py` / `_types.py` |
| `container_workdir` | (undocumented) | Parsed; default `/work` | ! | `_apptainer.py:111` |
| `post` / `environment` / `def_file` | (undocumented) | Parsed for `apptainer build` extension | ! | `_apptainer.py:112-114` |

## `spec.claude`

| Field | Documented | Implemented | Verdict | Source path |
| --- | --- | --- | --- | --- |
| `model` | alias or full versioned form | Regex-validated against `_VALID_MODEL_RE` | ✓ | `_validation.py:35-46,227-251` |
| `session` | `continue | new-session | resume` + legacy aliases | Matches; `continue-or-new → continue`, `new → new-session` | ✓ | `_parsers/_claude.py:18-20` |
| `resume_id` | str | Matches | ✓ | `_parsers/_claude.py:39` |
| `continue_max_age_minutes` | int | Coerced to int; None if uncoerceable | ✓ | `_parsers/_claude.py:21-29` |
| `flags[]` | list of strings | Matches | ✓ | `_parsers/_claude.py:35` |
| `channels[]` | MCP push channels | Matches | ✓ | `_parsers/_claude.py:34` |
| `auto_accept` | bool | Default `True` (doc didn't specify default) | ✓ | `_parsers/_claude.py:40` |
| `raw_options` | dict splatted into `ClaudeAgentOptions` | Matches | ✓ | `_parsers/_claude.py:30-32,41` |

## `spec.health` / `spec.restart` / `spec.watchdog` / `spec.autonomous`

| Field | Documented | Implemented | Verdict | Source path |
| --- | --- | --- | --- | --- |
| `health.enabled` | bool | default `False` | ✓ | `_parsers/_health.py:11` |
| `health.interval` / `health.timeout` | seconds | int defaults 30/5 | ✓ | `_parsers/_health.py:12-13` |
| `health.method` | `sdk-alive` only | Parser default is `"multiplexer-alive"` but validator only accepts `"sdk-alive"` | ✗ (parser default contradicts validator) | `_parsers/_health.py:14`, `_validation.py:288-289`, `_types.py:57` |
| `restart.policy` | `never | on-failure | always` | Matches | ✓ | `_parsers/_restart.py:12`, `_validation.py:278-282` |
| `restart.max_retries` / `backoff.*` | int | Matches; defaults `3 / 30 / 300 / 2` | ✓ | `_parsers/_restart.py:13-16` |
| `watchdog.enabled` | "parsed for back-compat" | Matches | ✓ | `_parsers/_watchdog.py` |
| `autonomous.enabled` | bool | Matches | ✓ | `_parsers/_autonomous.py:19` |
| `autonomous.drive_until` | str default `DONE` | Matches | ✓ | `_parsers/_autonomous.py:20` |
| `autonomous.max_turns` | int | Default 50 (undocumented number) | ✓ | `_parsers/_autonomous.py:21` |
| `autonomous.kick_text` | str | Default `"Continue. Print DONE when finished."` | ✓ | `_parsers/_autonomous.py:23` |
| `autonomous.idle_kick_after_s` | (undocumented) | Default 120 | ! | `_parsers/_autonomous.py:22` |

## `spec.a2a` / `spec.listen`

| Field | Documented | Implemented | Verdict | Source path |
| --- | --- | --- | --- | --- |
| `a2a.port` | `auto` (default), int, or `null` (disable) | Matches exactly | ✓ | `_parsers/_a2a.py:23-40` |
| `a2a.host` | (undocumented) | Parsed; default `127.0.0.1` | ! | `_parsers/_a2a.py:24` |
| `listen.port` (single int) | "override for host-level sac listen port (default 7878)" | **Wrong shape**: `spec.listen` is parsed as a LIST of port-declarations (`ListenPort{port,proto,path,name,owner}`), not a single `{port: N}` dict. The host-level `sac listen` port is set in `~/.scitex/agent-container/config.yaml`, not in agent spec. | ✗ (semantics) | `_parsers/_listen.py:8-44`, `_types.py:ListenPort` |

## Skills

| Field | Documented | Implemented | Verdict | Source path |
| --- | --- | --- | --- | --- |
| `spec.skills` | "removed in v3"; use `metadata.labels.skills` CSV + `dot_claude/skills/` | `_V3_REMOVED_FIELDS["skills"]` rejects it, BUT `parse_skills` still exists and is wired in `_loaders.py:287` (reads `raw.get("skills", {})`). It is never reachable because the validator rejects first. | ✓ doc, ! dead-code path | `_validation.py:113-116`; `_parsers/_skills.py`, `_loaders.py:287` |

## `spec.mcp_servers` / `spec.telegram` / `spec.hooks` / `spec.extensions`

| Field | Documented | Implemented | Verdict | Source path |
| --- | --- | --- | --- | --- |
| `mcp_servers` | dict-of-dicts merged into `.mcp.json`; supports `${metadata.*}` interpolation | `interpolate_mcp_servers` walks env + args | ✓ | `_parsers/_mcp.py` |
| `telegram.enabled` | bool | `TelegramSpec` has no `enabled` field; uses `auto_connect` (default True) | ✗ (field name) | `_parsers/_telegram.py:10-17`, `_types.py:TelegramSpec` |
| `telegram.chat_id` | doc'd | **Not in parser**; parser has `bot_token_env`, `allowed_users`, `auto_connect`, `greeting` | ✗ (field name) | `_parsers/_telegram.py:10-17` |
| `hooks.{pre_start,post_start,pre_stop}` | shell commands lists | Matches; also `post_stop` exists in `HOOK_KEYS` (`_helpers.py`); `mkdir -p workdir/.claude` auto-prepended | ✓ (doc omits `post_stop`) | `_parsers/_hooks.py`, `_loaders.py:251-255` |
| `extensions` | opaque pass-through dict | Matches | ✓ | `_parsers/_extensions.py` |

## `spec.proxy` (kind: AgentProxy)

| Field | Documented | Implemented | Verdict | Source path |
| --- | --- | --- | --- | --- |
| `proxy.upstream` | REQUIRED http(s) URL | Required, must start with `http://` or `https://` | ✓ | `_parsers/_proxy.py:48-58` |
| `proxy.trust` | `untrusted | local-mesh | trusted`, default `untrusted` | Matches | ✓ | `_parsers/_proxy.py:60-65` |
| `proxy.redact` | list[str], default `[]` | Matches | ✓ | `_parsers/_proxy.py:67-72` |
| `proxy.timeout_s` | float > 0, default 30.0 | Matches | ✓ | `_parsers/_proxy.py:74-82` |
| AgentProxy ↔ Agent coupling | proxy required when AgentProxy; claude/startup_* rejected; proxy rejected when Agent | Matches exactly | ✓ | `_validation.py:378-408` |

## Other implemented-but-undocumented blocks (`!`)

| Block | Effect | Source |
| --- | --- | --- |
| `spec.context_management` | trigger %/strategy/intervals for context auto-management | `_parsers/_context_management.py`, `_KNOWN_SPEC_KEYS` |
| `spec.startup` (vs `startup_commands`) | opt-in block with `ready_patterns`, `ready_idle_ticks`, `ready_poll_interval_seconds`, `ready_timeout_seconds`, `on_timeout`, `commands` | `_parsers/_startup.py:26-86` |
| `spec.container` | legacy container block (`runtime`, `image`, `volumes`, `network`, `mount_host_claude`) | `_parsers/_container.py`, `_validation.py:253-274` |
| `spec.scheduling` | rejected with hint (replaced by host/hosts) | `_validation.py:411-416` |
| `spec.dockerfile` | type-checked only (dropped 2026-05-13) | `_validation.py:221-225` |

## Summary counters

* `✓` (correct): 30
* `✗` (mismatched / wrong): 11
  * `spec.runtime` REQUIRED → optional with default
  * `spec.dot_claude` no auto-discover
  * `spec.env-file` missing from `_KNOWN_SPEC_KEYS`
  * `spec.multiplexer` missing from `_KNOWN_SPEC_KEYS`
  * `spec.startup_commands[]` shape (dict items not strings)
  * `spec.apptainer.image` REQUIRED → optional
  * `spec.apptainer.nv/rocm` mutex not enforced
  * `spec.apptainer.relaxed` / `fakeroot` not in parser
  * `spec.health.method` parser default vs validator
  * `spec.listen.port` semantics (shape: list, not single override)
  * `spec.telegram.enabled` / `.chat_id` (wrong field names)
* `!` (undocumented but real): 9
  * `screen.name`, top-level `session`, `apptainer.container_workdir`,
    `apptainer.post`/`environment`/`def_file`, `apptainer.binds` legacy
    dict form, `autonomous.idle_kick_after_s`, `a2a.host`,
    `hooks.post_stop`, `spec.context_management`, `spec.startup`,
    `spec.container`, `spec.scheduling` rejection, `spec.dockerfile`,
    dead `parse_skills`.

## Notes / open questions

* `multiplexer` and `env-file` clearly *should* be in `_KNOWN_SPEC_KEYS`
  (the loader reads them, the docs document them, examples use them).
  This looks like a parser-side bug, not a doc bug, but the brief is
  docs-only — flagged in the doc with `(VERIFY: validator rejects)` so
  the next code touch picks it up.
* `parse_skills` and `parse_container` are still wired into `load_v3`
  even though their inputs are explicitly rejected by the validator —
  effectively dead code on the v3 happy path. Worth a follow-up
  cleanup (out of scope here).
