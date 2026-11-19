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
`kind` / `command` / `output` for a plain-command run (below),
`tool_calls` and `files.read` (from the harness's own transcript — Hermes `state.db` messages: every tool call with its arguments and result size, the paths passed to file-reading tools, and the first assistant-message time; `source` names it; no syscall trace), `layers`.

## Host-side RPC still works

Loopback listeners inside the sealed namespace (e.g. Hermes' TUI gateway) are
**mirrored**: `netgw` reports each LISTEN port, the host proxy opens the same
port on the host's `127.0.0.1` and relays each connection back in through the
`ingress.sock` unix socket. The mirror is inbound-only; it cannot carry egress.

## Plain commands (`sac sandbox exec`)

The same seal for ONE command that is not an agent — no harness, no model
key, no spec. For callers that must run untrusted code in isolation (e.g.
scitex-agentic-journal running a submitted bundle's `clew bundle rerun`):

```bash
sac sandbox exec --image /path/to/image.sif \
    --run-root /scratch/$USER/sac-exec-runs --timeout 600 \
    --bind "$PWD/bundle:/bundle" --pwd /bundle \
    --egress-allow pypi.org:443 --egress-allow files.pythonhosted.org:443 \
    --env-allow MY_TOKEN \
    -- clew bundle rerun .
```

| flag | meaning |
|---|---|
| `--image SIF` | the image (a file; its sha256 is recorded). Needs `python3` + `unshare` like any sealed run. |
| `--run-root DIR` | durable dir; the run is `<DIR>/<name>/<run_id>/`. `/tmp`, `/var/tmp`, `/dev/shm`, `/run` are refused. |
| `--timeout S` | required wall-clock limit; on expiry the container gets SIGTERM (SIGKILL 10 s later), sac exits **124** and `exit.timed_out: true`. |
| `--egress-allow H:P` | repeatable; **default: none** — no egress at all (every CONNECT is refused and logged, DNS is NXDOMAIN). |
| `--bind SRC:DST[:ro\|rw]` | repeatable; **read-only unless `:rw`**. SRC must exist. DST may not cover `/`, `/proc`, `/sys`, `/dev`, `/home/agent` or `/run/sac-sandbox`. |
| `--env-allow NAME` | repeatable; the variable's value is taken from the CALLER's env. A value holding `$`, a backtick, `"`, `'`, `\` or a line break is refused (exit 2) — apptainer evaluates env values as shell inside the container and would rewrite it (measured: `'$HOME'` arrives expanded); pass such data in a `--bind` file. |
| `--size-mb N` | cap on the run's writable layers (default 4096). |
| `--after archive\|remove` | keep the layers (default) or delete all but `out/`. |
| `--name N` | run group under the run root (default `exec`). |
| `--pwd DIR` | working directory inside the container (default `/home/agent`). |
| `--dry-run` | print the plan (the exact `apptainer exec` argv and policy) as JSON; run nothing. |

**What the command sees:** the image, the `--bind` mounts, a fresh
`$HOME=/home/agent`, `/tmp` + `/var/tmp`, a writable rootfs layer and
`/uvwork` (all per run, on the run root), and `/run/sac-sandbox` (the
stdlib gateway launchers + the proxy socket). Nothing of SAC is mounted (no
venv, no state dir, no fleet binds); `--containall` keeps the host `$HOME`,
`/tmp` and processes out. **Environment:** the `--env-allow` names plus
`LANG=C.UTF-8` and the caller's `TERM`, passed through a 0600 env file
under `secrets/` that is deleted once the gateway is up (values never
appear in an argv); `PATH` and other image-defined variables come from the
image (`%environment`), `HOME` is `/home/agent`, and the gateway sets
`HTTPS_PROXY`/`HTTP_PROXY`. Every other caller variable is dropped and its
NAME recorded. apptainer itself starts with a minimal process env.

**Output:** the command's stdout/stderr are **tee'd** — streamed to sac's
stdout/stderr as they arrive AND written to `<run_dir>/out/stdout.log` /
`stderr.log` (kept under both `--after` modes). sac's own lines go to stderr
only, prefixed `[sac-sandbox]`; the last one names the outcome path. **Exit
status:** the command's own status; 124 on `--timeout`; 96 when the run
could not be set up; 2 for a usage error. SIGINT/SIGTERM/SIGHUP to sac are
forwarded to the container and recorded as `exit.stopped_by: "caller"` +
`exit.stop_signal`.

The run is the agent machinery without the agent: `_run.prepare_run_dir`
(the fresh run dir and `outcome.json`), `_argv.run_flags` + `netgw_wrap`
(the network seal), `_supervisor.supervise` in the foreground (env seal,
egress proxy, size watchdog, deadline, tee) and `_run.finalize_run` (files
written, `--after`). Its record has `kind: "command"`, `command` (argv,
binds, pwd, timeout), `output` (log paths) and `env.requested` / `passed` /
`missing` / `dropped`; it has no `token_usage` / `tool_calls` (no harness).

## Limits

* Agents: supported on the TUI runtimes (`runtime: tui`: Hermes, Claude,
  Codex, OpenCode panes). The headless SDK runner has no `begin_run` hook and
  its launch refuses with "no prepared run". A plain command needs no
  runtime: use `sac sandbox exec` (there is no `runtime: command` spec form).
* The image needs `python3`, `unshare` (util-linux) and `/dev/net/tun` support.
* `sac sandbox exec` lists files written to `$HOME` and the rootfs overlay;
  writes into an `:rw` bind are not listed (they land on the host path).
  The gateway launchers under `/run/sac-sandbox/bin` (stdlib copies of
  `netgw.py` / `hide.py`) are visible to the command; nothing else of SAC is.
* Only a SIF FILE is accepted as `--image` (a sandbox directory has no
  single sha256 to record).
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
