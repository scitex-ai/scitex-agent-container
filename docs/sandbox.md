# Sealed runs (`spec.sandbox`)

A spec with a `sandbox:` block launches every run **sealed**:

| concern | what SAC does |
|---|---|
| egress | the container gets `--net --network none`; the inner command runs under `netgw` (in-container, private user+net namespace) whose only way out is `127.0.0.1:3128` → unix socket → a host-side CONNECT proxy that tunnels **only** to `egress.allow`. Everything else is refused (403 / TCP RST / NXDOMAIN) and logged. No iptables, netns or sudo on the host. |
| control plane | a sealed agent gets no builtin `sac` MCP server / `server:sac` channel (agent spawn, host exec and store tools are host authority). For Hermes, SAC's in-container owner keeps `/state/<name>` and the launching SAC installation but starts the agent tree (gateway, TUI, every tool child) through `_sandbox/_hide`: a nested user+mount+PID namespace in which `/state/<name>`, the launching SAC environment and the image's SAC are covered by empty read-only mounts, `/proc` shows only the agent's own processes, and the agent holds no capability to undo either. The gateway's API key reaches it over a pipe, never its exec-time environment. No `sac` is on the agent's `PATH`. |
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
* A same-uid agent can reach the proxy socket and so could append forged log
  lines; it can neither delete logged lines nor reach a non-allowlisted host.
* The control-plane mask covers Hermes (its owner process is the boundary).
  The Claude, Codex and OpenCode TUI panes run the harness directly as the
  container's inner command, so their agent can still list `/state/<name>`
  (secret env files there are rewritten to allowed names by the seal).
  `startup_commands` run before the owner and are not masked (they are
  spec-authored, not agent-authored). Mount points stay visible by NAME in
  `/proc/self/mountinfo`; their content does not.
