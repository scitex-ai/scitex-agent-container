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
  # ---------------------------------------------------------------------
  # TWO AXES, TWO LINES. `harness:` names the PROGRAM that runs the loop;
  # `engine:` names the MODEL ENDPOINT that answers it. They are
  # independent: either can be flipped without touching the other, and
  # that is the whole point of the split.
  #
  #   harness: anthropic | codex        (anthropic == claude-code)
  #   runtime: tui | headless           (launch mode within the harness)
  #   engine:  <key from the fleet engine library, or from `engines:` below>
  #
  # Moving THIS agent onto Qwen is ONE line:  engine: qwen38-27b
  # Moving the WHOLE FLEET onto Qwen is ONE line, in the fleet library
  # ($SCITEX_DIR/agent-container/engines.yaml), not here.
  # ---------------------------------------------------------------------
  runtime: headless
  harness: anthropic
  # No `engine:` line = follow the fleet default. Uncomment to PIN this
  # agent to one backend, immune to any fleet-wide edit:
  # engine: qwen38-27b
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
    # EXPLICIT-EMPTY, and that is the new grammar, not an omission: the
    # ENGINE carries the model and the endpoint (see `engine:` at the top
    # of spec:). A value written HERE is read as a LEGACY backend pin and
    # takes precedence over the fleet default, which is exactly what a
    # freshly scaffolded spec should NOT do — it would be born unable to
    # follow a fleet-wide backend switch. Pin a backend with
    # `engine: <key>`, never by writing a model down here.
    model: ""
    flags:
      - --dangerously-skip-permissions
    channels: []
    raw_options: {{}}
    # resume, always. OPERATOR RULING 2026-08-28: fresh is wrong, continue is
    # wrong, resume-with-an-id is the only correct value — an agent that
    # restarts must continue the SAME conversation, not a fresh one and not
    # merely the latest one. `null` used to role-derive to continue-or-fresh,
    # which is how 374 live specs across the fleet ended up losing every
    # agent's memory on every restart.
    #
    # Safe with an empty resume_id: the runner warns and falls back to
    # --continue (_runners/_tmux/claude_code.py), so a brand-new agent with no
    # session yet is never blocked — and it upgrades itself the moment an id
    # is pinned below.
    session: resume
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
#   * SCITEX_CARDS_AGENT_ID       card-store writes attribute to THIS agent
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
  # TWO AXES, TWO LINES — `harness:` is the PROGRAM, `engine:` is the
  # MODEL ENDPOINT, and neither implies the other. Flipping this agent to
  # a local Qwen is one added line (`engine: qwen38-27b`); flipping the
  # whole fleet is one line in the fleet engine library, not here.
  runtime: tui
  harness: anthropic
  # engine: <key>   # omitted = follow the fleet default; state it to PIN.
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

    # Per-agent env. SCITEX_CARDS_AGENT_ID makes scitex-cards writes
    # attribute to THIS agent; it is the CANONICAL board-identity name (its
    # predecessor SCITEX_TODO_AGENT_ID is retired and must not be emitted
    # into a new spec). (sac AUTO-injects SCITEX_AGENT_CONTAINER_STATE_DB +
    # binds the per-agent state dir, so the state DB needs no manual entry
    # here.)
    env:
      SCITEX_CARDS_AGENT_ID: {name}

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
    # EXPLICIT-EMPTY, and that is the new grammar, not an omission: the
    # ENGINE carries the model and the endpoint (see `engine:` at the top
    # of spec:). A value written HERE is read as a LEGACY backend pin and
    # takes precedence over the fleet default, which is exactly what a
    # freshly scaffolded spec should NOT do — it would be born unable to
    # follow a fleet-wide backend switch. Pin a backend with
    # `engine: <key>`, never by writing a model down here.
    model: ""
    flags:
      - --dangerously-skip-permissions
    # resume, always — see the note on the other template above.
    session: resume
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
