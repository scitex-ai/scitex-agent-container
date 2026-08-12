"""Inline spec templates for ``sac agents create`` (minimal / full).

Extracted from ``_create.py`` (per-file line cap). Both templates are
``str.format`` strings with ``{name}`` / ``{host}`` / ``{home}``
placeholders — literal YAML braces are escaped as ``{{}}``.

EVERY field is written explicitly (red-start ruling 2026-07-21): a spec
that omits a field is a load ERROR whose hint lists the whole missing
set with paste-ready defaults, so anything this module scaffolds must
already carry the complete field set — the operator edits values, never
hunts for missing keys.
"""

from __future__ import annotations

_MINIMAL_TEMPLATE = """\
# THIS IS A DESIGN DOCUMENT — the contract for an agent not yet started.
# The state of a RUNNING agent lives in the database, never in this file.
#
# {name} — fresh v3 spec scaffolded by ``sac agents create``.
# EVERY field is written explicitly (red-start ruling 2026-07-21: an
# omitted field is a load ERROR whose hint lists the whole missing set
# with paste-ready defaults); the values are those defaults except the
# handful curated here. See ``examples/agents/full-agent/spec.yaml``.

apiVersion: scitex-agent-container/v3
kind: Agent

spec:
  runtime: apptainer
  harness: anthropic
  # Placement: the RESOLVED hostname of the machine this agent runs on
  # (filled with the creating host at render time; `host: local` is
  # banned). Edit to a `sac host list` peer name to pin it elsewhere,
  # or use `hosts:` for one instance per host.
  host: {host}
  workdir: ~/proj/{name}
  python-venv: ""
  user: ""
  to_home: ./to_home
  startup_commands: []
  startup_prompts: []
  listen: []
  extensions: {{}}
  mcp_servers: {{}}

  container:
    runtime: none
    image: scitex-agent-container:latest
    volumes: []
    network: host
    mount_host_claude: false

  apptainer:
    image: ~/.scitex/agent-container/containers/sac-base.sif
    binds: []
    env: {{}}
    raw_args: []
    post: ""
    environment: {{}}
    def_file: ""
    nv: false
    rocm: false
    overlay: ""
    overlay_size: ""
    overlay_create_if_missing: true
    tmpfs_size: 2G
    relaxed: false
    fakeroot: false
    jail: false
    nested_build: false

  claude:
    model: haiku
    flags:
      - --dangerously-skip-permissions
    channels: []
    raw_options: {{}}
    # null = role-derived (continue for coordinator roles, fresh otherwise)
    session: null
    continue_max_age_minutes: null
    resume_id: ""
    auto_accept: true
    account: ""
    credentials_file: ""
    # The ROTATION POOL, filled at create time with every account in the store.
    # The selector rotates over this list by remaining headroom. ONE entry is
    # perfectly valid — it is the right answer for a single-account setup, and
    # rotation not happening with one account is arithmetic, not a fault. What
    # matters is HEADROOM, not length: an agent pinned to an account at 7d 100%
    # authenticates fine and then dies on every model call.
    credentials_files: {credentials_files}
    provider: null

  health:
    enabled: true
    interval: 60
    timeout: 5
    method: sdk-alive

  watchdog:
    enabled: false
    interval: 1.5
    responses:
      y_n: "1"
      y_y_n: "2"
      waiting: /speak-and-call

  restart:
    policy: on-failure
    max_retries: 3
    prune_on_stop: false
    backoff:
      initial: 30
      max: 300
      multiplier: 2

  autonomous:
    enabled: false
    drive_until: DONE
    max_turns: 50
    idle_kick_after_s: 120
    kick_text: Continue. Print DONE when finished.

  hooks:
    pre_start: []
    post_start: []
    pre_stop: []
    post_stop: []
    on_compact: []
    on_restart: []
    on_diff: []

  context_management:
    trigger_at_percent: 70.0
    strategy: noop
    warn_before_n_checks: 0
    check_interval_seconds: 300
    state_file: ~/.scitex/agent-container/state/<agent>.json

  a2a:
    host: 127.0.0.1
    port: auto

  comms:
    outbound:
      siblings: allow
      parent: allow
    inbound:
      siblings: allow
      parent: allow
    a2a:
      listen: true

  lineage:
    group: ""
    may_spawn: true

# EOF
"""


_FULL_TEMPLATE = """\
# THIS IS A DESIGN DOCUMENT — the contract for an agent not yet started.
# The state of a RUNNING agent lives in the database, never in this file.
#
# {name} — fresh v3 DEVELOPER spec scaffolded by ``sac agents create --template full``.
#
# This is the PROVEN developer shape the fleet's live dev agents use
# (card sac-agents-new-template-stale; operator 2026-06-25: "very
# general, just developer like existing ones") — a ready-to-run
# project-maintainer agent, NOT a bare skeleton. It ships:
#   * runtime: tui                interactive in-apptainer Claude TUI
#   * relaxed + directory overlay persistent per-agent $HOME / installs
#   * full host reach at the canonical path (so ~/proj/... paths match)
#   * fleet push channels         server:sac + server:scitex-todo + telegrammer
#   * SCITEX_TODO_AGENT_ID        todo-store writes attribute to THIS agent
#   * editable install of the agent's own repo (live dev loop)
#   * a generic "Start or continue." kick + metadata.labels + opus model
# EVERY field is written explicitly (red-start ruling 2026-07-21); the
# non-curated ones sit at their defaults. Edit the labels + the startup
# prompt for the agent's real mission.

apiVersion: scitex-agent-container/v3
kind: Agent

metadata:
  labels:
    role: project-maintainer
    team: scitex
    project: {name}
    purpose: {name}-maintainer
    description: |
      {name} developer agent — maintains the {name} repo. Replace this
      description and the startup prompt below with the agent's real mission.
    capabilities: develop, test, review, release
    cardinality: singleton

spec:
  runtime: tui
  harness: anthropic
  # RESOLVED placement (creating host at render time; `local` is banned).
  host: {host}

  # The in-container --pwd. The repo is bound at this SAME absolute path
  # below, so host and container agree (editable install, git, tooling).
  workdir: {home}/proj/{name}

  # to_home/ sibling — mirrored into the container $HOME (overlay upper
  # home under relaxed mode). Put CLAUDE.md / .mcp.json / .claude/ there.
  to_home: ./to_home

  python-venv: auto
  user: ""
  listen: []
  extensions: {{}}
  mcp_servers: {{}}

  container:
    runtime: none
    image: scitex-agent-container:latest
    volumes: []
    network: host
    mount_host_claude: false

  apptainer:
    # sac-base.sif = the minimal layer; the agent editable-installs its
    # own stack into the overlay below. Swap to sac-scitex.sif to start
    # from the full pre-baked scitex stack instead.
    image: ~/.scitex/agent-container/containers/sac-base.sif

    # Relaxed isolation — the dev agent shares the operator's identity and
    # host tree (the fleet dev default). Pairs with the raw_args below,
    # which re-declare the namespace + canonical HOME the overlay needs.
    relaxed: true

    # Persistent per-agent directory overlay: package installs, caches and
    # $HOME state survive restarts while the base SIF stays immutable. sac
    # auto-creates the overlay dir and materialises to_home/ into its upper
    # home on first start.
    overlay: ~/.scitex/agent-container/containers/overlays/{name}/
    overlay_size: ""
    overlay_create_if_missing: true

    # Full host reach at the CANONICAL path (source ~ is expanded by sac;
    # the destination must be absolute). Narrow this to specific project
    # trees for a capsule-style agent.
    binds:
      - {home}:{home}:rw

    # Per-agent env. SCITEX_TODO_AGENT_ID makes scitex-todo writes
    # attribute to THIS agent. (sac AUTO-injects
    # SCITEX_AGENT_CONTAINER_STATE_DB + binds the per-agent state dir, so
    # the state DB needs no manual entry here.)
    env:
      SCITEX_TODO_AGENT_ID: {name}

    # Relaxed mode skips sac's curated isolation prepend, so re-declare the
    # user namespace, filesystem isolation, and the canonical container HOME
    # that the overlay upper home is materialised under.
    raw_args:
      - --userns
      - --containall
      - --home=/home/agent

    post: ""
    environment: {{}}
    def_file: ""
    nv: false
    rocm: false
    tmpfs_size: 2G
    fakeroot: false
    jail: false
    nested_build: false

  claude:
    model: opus[1m]
    flags:
      - --dangerously-skip-permissions
    session: continue
    auto_accept: true

    # Fleet push channels: sac control bus + shared scitex-todo store + the
    # Telegram bridge (operator DMs wake the agent). server:sac is also
    # auto-injected by the loader; listed here for visibility.
    channels:
      - server:sac
      - server:scitex-todo
      - server:claude-code-telegrammer

    raw_options: {{}}
    continue_max_age_minutes: null
    resume_id: ""
    account: ""
    # Pin this agent to a dedicated account by pointing credentials_file at
    # that account's LIVE .credentials.json (else the shared host OAuth is
    # forwarded automatically).
    credentials_file: ""
    # The ROTATION POOL, filled at create time with every account in the store.
    # The selector rotates over this list by remaining headroom. ONE entry is
    # perfectly valid — it is the right answer for a single-account setup, and
    # rotation not happening with one account is arithmetic, not a fault. What
    # matters is HEADROOM, not length: an agent pinned to an account at 7d 100%
    # authenticates fine and then dies on every model call.
    credentials_files: {credentials_files}
    provider: null

  # Editable-install the agent's own repo so imports resolve to the live
  # working tree (edit -> test without reinstall). The `|| true` keeps
  # startup resilient when the repo has no installable package yet.
  startup_commands:
    - command: 'uv pip install -e {home}/proj/{name} --quiet || true'

  # Generic self-resume kick — the agent picks up its board / last state.
  # Replace with the agent's real first mission.
  startup_prompts:
    - Start or continue.

  health:
    enabled: true
    interval: 60
    timeout: 10
    method: sdk-alive

  watchdog:
    enabled: false
    interval: 1.5
    responses:
      y_n: "1"
      y_y_n: "2"
      waiting: /speak-and-call

  restart:
    policy: on-failure
    max_retries: 3
    prune_on_stop: false
    backoff:
      initial: 10
      max: 120
      multiplier: 2

  autonomous:
    enabled: false
    drive_until: DONE
    max_turns: 50
    idle_kick_after_s: 120
    kick_text: Continue. Print DONE when finished.

  hooks:
    pre_start: []
    post_start: []
    pre_stop: []
    post_stop: []
    on_compact: []
    on_restart: []
    on_diff: []

  context_management:
    trigger_at_percent: 70.0
    strategy: noop
    warn_before_n_checks: 0
    check_interval_seconds: 300
    state_file: ~/.scitex/agent-container/state/<agent>.json

  a2a:
    host: 127.0.0.1
    port: auto

  comms:
    outbound:
      siblings: allow
      parent: allow
    inbound:
      siblings: allow
      parent: allow
    a2a:
      listen: true

  lineage:
    group: ""
    may_spawn: true

# EOF
"""


_TEMPLATES = {
    "minimal": _MINIMAL_TEMPLATE,
    "full": _FULL_TEMPLATE,
}

__all__ = ["_FULL_TEMPLATE", "_MINIMAL_TEMPLATE", "_TEMPLATES"]
