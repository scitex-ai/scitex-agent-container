---
description: |
  [TOPIC] Host filesystem + git/gh access for SDK agents.
  [DETAILS] How to give an SDK agent precise (or broad) access to host
  paths via spec.mounts, spec.user, and spec.env — three orthogonal
  primitives, no special "passthrough" flag.
tags: [scitex-agent-container-host-passthrough]
---

# Host filesystem access for SDK agents

The default sac SDK runner is **isolated**: only ``/work`` (workspace)
and ``/state`` (sac state) plus the agent's auth credentials are mounted
inside the container. Spawn an agent with a prompt that says "open
``/home/me/proj/foo/README.md``" and it'll fail with "No such file or
directory" because that path doesn't exist inside the container.

Three YAML primitives give an SDK agent host-shaped access. Use them
together when you need full path-mirroring (orchestrators), or just
``spec.mounts`` alone when you want a worker scoped to a single repo.

## ``spec.mounts`` — declarative bind mounts

```yaml
spec:
  mounts:
    - { src: ~/proj/scitex-stats, dst: ~/proj/scitex-stats, mode: rw }
    - { src: /data/big-corpus,    dst: /data/big-corpus,    mode: ro }
```

Each entry becomes ``--mount type=bind,src,dst[,readonly]``. ``~`` and
``$VAR`` are expanded on ``src`` so YAMLs stay portable across
machines. ``mode`` defaults to ``rw``.

If you don't list a path here, the agent **cannot see it**. That is the
worker's primary safety boundary — no read-only re-binding, no opt-out
flag. Just don't mount what the agent shouldn't reach.

## ``spec.user`` — who the container runs as

```yaml
spec:
  user: host         # operator's UID:GID — files written from inside
                     # land as your user on the host
  # OR
  user: "1000:1000"  # explicit numeric
  # OR (default)
  user: ""           # image's USER (typically `agent`, uid 1000)
```

Required when ``spec.mounts`` grants host paths and you want writes
from the agent to be owned by you, not by the image's `agent` user.

## ``spec.env`` — environment variable forwarding

```yaml
spec:
  env:
    HOME: ${HOME}      # tools that read $HOME pick the operator's home
    GH_TOKEN: ${GH_TOKEN}
```

When ``HOME`` is set, sac mounts the credential file (Anthropic
OAuth) at ``${HOME}/.claude/.credentials.json`` so ``Path.home()``
inside the container resolves to the same path the cred file is at.
Without ``env.HOME``, the cred file lands at the image default
(``/home/agent/.claude/.credentials.json``).

## Composition examples

### Worker scoped to one repo

```yaml
spec:
  runtime: docker
  image: scitex-agent-container:scitex
  mounts:
    - { src: ~/proj/scitex-stats, dst: ~/proj/scitex-stats, mode: rw }
  startup_commands:
    - command: |
        Run scitex-dev ecosystem audit-all scitex-stats and fix any
        violations. Commit and push when green.
```

The agent sees only scitex-stats and its own workspace + state. It
cannot read sac source, other agents' definitions, your dotfiles,
or any other project. Container's `agent` user (uid 1000) writes
land as `agent` on the host filesystem — fine if the repo dir is
group-writable or the operator runs sac as uid 1000 herself.

### Orchestrator (host-shaped, host-owned)

```yaml
spec:
  runtime: docker
  image: scitex-agent-container:scitex
  user: host                      # writes land as operator
  env:
    HOME: ${HOME}                  # tools resolve $HOME to host
  mounts:
    - { src: ~/.scitex/agent-container/agents,
        dst: ~/.scitex/agent-container/agents, mode: ro }
    # ^ orchestrator reads other agents' yamls to spawn them
    - { src: ~/proj, dst: ~/proj, mode: rw }
    # ^ broad project access; only set this for trusted orchestrators
```

## Required image bits

The default ``scitex-agent-container:scitex`` image installs
``gh`` (GitHub CLI), ``git``, and ``openssh-client`` so an agent given
host-shaped access can immediately query CI, push commits over SSH,
and pull skill content from private repos. If you build a custom
image, keep these tools.

## Watching the agent

```bash
sac agents start polish-stats
sac agents tail polish-stats -n 30      # rendered transcript
sac agents tail polish-stats --json     # raw session.jsonl records
sac agents health polish-stats         # is it alive?
sac agents stop polish-stats
```

## Caveats

* **No magic flag.** There is no ``home_passthrough``, ``protect_self``,
  or similar shortcut. ``spec.mounts`` + ``spec.user`` + ``spec.env``
  are the only mechanisms. If a path isn't in ``spec.mounts``, the
  agent cannot see it.
* **UID alignment.** When ``spec.user: host`` is set, the container's
  user is named ``agent`` in ``/etc/passwd`` even though it runs at
  the operator's UID. Tools that read username via
  ``getpass.getuser()`` may report ``agent`` while ``id -u`` reports
  the host UID. Filesystem semantics still work.
* **Multi-operator hosts.** ``${HOME}`` resolves to whoever invoked
  sac. On shared machines, prefer explicit absolute paths in
  ``spec.mounts`` for repeatable behaviour.
