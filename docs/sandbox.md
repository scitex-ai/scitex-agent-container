# Sealed runs (`spec.sandbox`)

A spec with a `sandbox:` block launches every run **sealed**:

| concern | what SAC does |
|---|---|
| egress | the container gets `--net --network none`; the inner command runs under `netgw` (in-container, private user+net namespace) whose only way out is `127.0.0.1:3128` → unix socket → a host-side CONNECT proxy that tunnels **only** to `egress.allow`. Everything else is refused (403 / TCP RST / NXDOMAIN) and logged. No iptables, netns or sudo on the host. |
| control plane | a sealed agent gets no builtin `sac` MCP server / `server:sac` channel (agent spawn, host exec and store tools are host authority; a sealed spec declaring them is refused). On EVERY sealed TUI pane (Claude Code, Codex, Hermes, OpenCode) the agent tree — its `startup_commands`, the harness and every tool child — starts through `_sandbox/_hide`: a nested user+mount+PID namespace in which `/state/<name>`, the launching SAC environment, the image's SAC (venvs, console scripts, source tree) and the run's proxy/ingress socket directory are covered by empty read-only mounts, `/proc` shows only the agent's own processes (outer processes' `environ`, fds and `root` are unreachable), and the agent holds no capability to undo either. The launcher refuses to start anything (exit 125) when a mask step fails or a `sac` stays runnable. No `sac` is on the agent's `PATH`. See "How each pane is masked" below. |
| env | the container environment is `env.allow` + the keys of `spec.apptainer.env` + **one** `env.model_key` + SAC's declared launch wiring (`_sandbox/_env_policy.SAC_WIRING`). Fleet-default env and binds (`~/.scitex/cards`, host `/tmp`, quota cache …), host `~/.claude` commands/skills and skills of packages installed next to the launching SAC never reach the agent. apptainer itself is started with a minimal process environment, never `os.environ`. |
| per-run reset | each start creates `<run.root>/<agent>/<run_id>/`: a directory overlay (the writable rootfs), a fresh `$HOME`, `/tmp` + `/var/tmp` (`--workdir`) and `/uvwork`. `run.size` caps the WHOLE run dir: the supervisor measures it every 5 s and terminates the container on overflow (`exit.reason` says so). After the run, `run.after: remove` deletes every layer except `out/`; `archive` keeps them. |
| outcome | `<run_dir>/out/outcome.json` (written atomically at every transition), `exit_code`, `egress_blocked.jsonl`, `egress_allowed.jsonl`, `egress_gateway.jsonl`. |

```yaml
spec:
  sandbox:
    egress:
      allow: [opencode.ai:443, pypi.org:443, files.pythonhosted.org:443]
    env:
      allow: [SCITEX_STORE_DSN]          # names only; values come from spec.apptainer.env / the launch env
      model_key: OPENCODE_GO_API_KEY_03  # the ONE model key; a Hermes failover pool must list only it
    run:
      root: /scratch/$USER/sac-runs      # durable disk; /tmp, /var/tmp, /dev/shm, /run are refused
      size: 4G                           # cap on the whole run dir (overlay + $HOME + /tmp + /uvwork)
      after: remove                      # remove | archive
```

Every key is required once the block is present; unknown keys are refused.
`spec.apptainer.overlay`, `relaxed: true` and `raw_args` network/overlay/workdir
flags are refused for a sealed agent (the sandbox owns them).

## Outcome record (`outcome.json`)

`status` (`prepared` → `running` → `exited` → `finalized`), `run_id`,
`prepared_at` / `started_at` / `ended_at` / `finalized_at`, `exit`
(`{"code": N}`, `{"code": null, "signal": S}`, or `{"code": null, "reason": …}`
when the supervisor was killed before it observed the container exit),
`image` (path + sha256), `sac_version`, `policy`, `env` (passed / dropped
NAMES, never values), `egress` (summary + log paths), `token_usage` (Hermes
`state.db` totals when available), `files.home_written` and
`files.overlay_written` (files in the overlay's `upper/`), `size` (limit + peak),
`tool_calls` and `files.read` (from the harness's own transcript — Hermes `state.db` messages: every tool call with its arguments and result size, the paths passed to file-reading tools, and the first assistant-message time; `source` names it; no syscall trace), `layers`.

## Host-side RPC still works

Loopback listeners inside the sealed namespace (e.g. Hermes' TUI gateway) are
**mirrored**: `netgw` reports each LISTEN port, the host proxy opens the same
port on the host's `127.0.0.1` and relays each connection back in through the
`ingress.sock` unix socket. The mirror is inbound-only; it cannot carry egress.

## Limits

* Supported on the TUI runtimes (`runtime: tui`: Hermes, Claude, Codex,
  OpenCode panes). The headless SDK runner has no `begin_run` hook and its
  launch refuses with "no prepared run".
* The image needs `python3`, `unshare` (util-linux) and `/dev/net/tun` support.
* `run.size` is enforced by polling (5 s), so a burst can overshoot briefly
  before the container is terminated. (A FUSE ext3 overlay image would be a
  hard cap but stacks a third FUSE layer and stalled Hermes for minutes on a
  loaded host.)
* The proxy/ingress socket directory is covered inside the mask, so the
  agent tree cannot write to the proxy log socket directly; it can neither
  delete logged lines nor reach a non-allowlisted host.
* The harness holds its model key (the ONE `env.model_key`) in its own
  environment, and the agent's tool children run as the same user in the
  same PID namespace, so the agent can read that key. Nothing else secret
  is reachable: `/state/<name>` and the outer processes are masked.
* A covered SAC package directory still resolves as an empty, unreadable
  namespace package on a `PYTHONPATH` that names it; no SAC module or
  attribute is importable from it. Mount points stay visible by NAME in
  `/proc/self/mountinfo`; their content does not.

## How each pane is masked

The launcher is the run's read-only copy of `_sandbox/_hide.py`
(`/run/sac-sandbox/bin/hide.py`), run BY PATH under `python3 -I` on the
image's interpreter, inside the egress gateway (`_sandbox/_mask`).

* **Claude Code, Codex** (the harness is the pane's inner command): the
  inner command IS the mask — `python3 -I /run/sac-sandbox/bin/hide.py
  --hide … -- /bin/bash -lc "<startup_commands>; exec <harness>"`. The mask
  exists before the first startup command runs; startup exports still reach
  the harness. Settings (`$HOME/.claude/settings.json`), MCP config
  (`$HOME/.mcp.json`), `CLAUDE_CONFIG_DIR` / `CODEX_HOME` (under `/tmp`) and
  the model key are outside every hidden path and reach the harness.
* **Hermes, OpenCode** (an in-container SAC owner keeps `/state/<name>`):
  the owner runs from the launching SAC installation (bound at its own
  paths; validated at launch) and starts the harness processes (Hermes'
  gateway + TUI; `opencode serve` + `opencode attach`) under the mask
  itself. Their `startup_commands` run FIRST in their own masked shell —
  filesystem effects persist, exported variables do not reach the harness —
  and the owner starts only if that shell succeeded.
* SAC code that runs in-container (the owners) starts as `python -I -c
  <bootstrap> <SAC root> <module>`, never `python -m`: a sealed pane's
  working directory and `PYTHONPATH` are agent-influenced and must not
  shadow SAC or the standard library while the control plane is visible.
