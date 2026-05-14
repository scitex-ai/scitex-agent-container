# Changelog

All notable changes to `scitex-agent-container` (sac) are recorded here.
Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/);
versioning follows [SemVer](https://semver.org/).

## [Unreleased]

### Added
- **F-CS15** — `sac mcp` server. New `_mcp/` package + `cli_pkg/mcp_cmds.py`
  Click group exposing `start`, `doctor`, `list-tools`, `install` against a
  FastMCP server with 36 `sac_*` tools mirroring the CLI surface (agent /
  db / host / image / template / account / skills / introspection). Install
  with `pip install scitex-agent-container[mcp]`.
- **F-CS3** phase 2 — runner-side autonomous loop: SDK runner consumes
  `spec.autonomous` and drives turns until `drive_until` matches or
  `max_turns` is reached. New `_runners/claude_session._autonomous_loop`
  coroutine + `--autonomous-*` CLI args; container.py forwards the spec.
- **OAuth credentials synthesis** — `provision_anthropic_auth` writes a
  minimal `~/.claude/.credentials.json` when given a `sk-ant-oat-*`
  access token via `SAC_ANTHROPIC_API_KEY`, so Pro/Max plans work in
  containers without the host file.

### Changed
- **F-CS17** stage 3a–3d — CLI/TUI runtime, SLURM runtime, action/context
  pane abstractions, and the legacy Dockerfile all removed. `sac` is now
  container-only: docker / podman / apptainer engines, the SDK-persistent
  base image, and the standalone Python runner under `_runners/`.
- **Auth handoff renamed** to `SAC_ANTHROPIC_API_KEY`. The previous
  `SCITEX_AGENT_CONTAINER_CI_ANTHROPIC_API_KEY[_OAUTH]` envs are gone.
  `provision_anthropic_auth` only honours `ANTHROPIC_API_KEY` when set
  explicitly by the operator; otherwise it bridges (api-key form) or
  synthesises a credentials file (oat- form) from `SAC_ANTHROPIC_API_KEY`.
- **Container daemon argv** — drops `--rm`; lifecycle.stop now does an
  explicit `docker rm -f` after stop. Adds `--env HOME=/tmp` so the
  bundled `claude` binary has a writable home even when the host UID
  has no `/etc/passwd` entry inside the image. Forwards
  `SAC_ANTHROPIC_API_KEY` (and `ANTHROPIC_API_KEY` when explicit).
- **F-CS16** — schema flatten (`spec.image`, `spec.dockerfile` top-level),
  auto-build at start, validator hard-errors on legacy runtimes.

### Removed
- F-CS17: `runtimes/slurm{,_tenant,sbatch_spartan}.py`, the entire CLI/TUI
  stack (`context_manager`, `actions/{compact,nonce_probe}`, `auto/response`,
  `cli_pkg/action_cmds`), the legacy `containers/Dockerfile`, and all
  associated tests (~7,500 LOC).

## [0.13.0] — pre-rewrite snapshot

Reference tag for the state immediately before F-CS17. See `git log v0.13.0`
for the prior history.
