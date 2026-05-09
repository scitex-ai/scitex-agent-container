---
description: |
  [TOPIC] Host filesystem + git/gh passthrough for SDK agents.
  [DETAILS] How to spawn SDK agents that can read host project repos,
  push commits, and query GitHub CI from inside the container — using
  spec.home_passthrough and spec.mounts.
tags: [scitex-agent-container-host-passthrough]
---

# Host passthrough — making SDK agents see the host filesystem

The default sac SDK runner is **isolated**: only ``/work`` (workspace)
and ``/state`` (sac state) are mounted. Spawn an agent with a prompt
that says "open ``/home/me/proj/foo/README.md``" and it'll fail with
"No such file or directory" because that path doesn't exist inside
the container.

Two YAML knobs let an SDK agent operate on the host's filesystem
directly — useful when an orchestrator wants to delegate work that
naturally lives in the operator's checked-out source trees.

## ``spec.home_passthrough: true`` — mirror the operator's $HOME

```yaml
spec:
  runtime: docker
  image: scitex-agent-container:sdk-persistent
  home_passthrough: true
  workdir: /tmp/foo-workdir
  startup_commands:
    - command: |
        Open /home/<operator>/proj/figrecipe/README.md and report.
```

When ``home_passthrough`` is on, sac:

* bind-mounts host ``$HOME`` at the same absolute path inside the
  container,
* sets the container's ``HOME`` env var to match,
* runs the container as host ``UID:GID`` (so file writes from the
  agent are owned by the operator on disk),
* forwards ``~/.config/gh`` (read-only) so the ``gh`` CLI's per-host
  token store works inside the container.

Net effect: prompts that say ``/home/<operator>/proj/<repo>/`` work
unchanged. The agent can ``git push``, ``gh pr create``, ``gh run
list`` against the operator's authenticated identity.

## ``spec.mounts`` — extra ad-hoc bind mounts

```yaml
spec:
  mounts:
    - {src: /data/big-corpus, dst: /data/big-corpus, mode: ro}
    - {src: ~/.cache/huggingface, dst: ~/.cache/huggingface}
```

Each entry becomes ``--mount type=bind,src,dst[,readonly]``.
``~`` and ``$VAR`` are expanded on ``src`` so YAMLs stay portable
across machines. ``mode`` defaults to ``rw``.

Use ``spec.mounts`` for paths *outside* ``$HOME`` you want exposed,
or for finer control than the broad ``home_passthrough`` switch.
The two are independent — combine them freely.

## Required image bits

The default ``scitex-agent-container:sdk-persistent`` image installs
``gh`` (GitHub CLI), ``git``, and ``openssh-client`` so a
home-passthrough'd agent can immediately query CI, push commits over
SSH, and pull skill content from private repos. If you build a
custom image, keep these tools.

## Watching the agent

```bash
sac agent start polish-foo
sac agent tail polish-foo -n 30      # rendered transcript
sac agent tail polish-foo --json     # raw session.jsonl records
sac agent inspect polish-foo         # is it alive?
sac agent stop polish-foo
```

``sac agent tail`` reads ``~/.scitex/agent-container/runtime/<agent>/<agent>/session.jsonl``
(the structured transcript the SDK runner persists) and pretty-prints
the assistant turns.

## Caveats

* **Trust boundary.** ``home_passthrough`` is full read-write to
  ``$HOME``. If you need the agent constrained to a single repo,
  prefer ``spec.mounts`` with ``mode: ro`` (or ``mode: rw`` only on
  the specific repo path).
* **Filesystem identity.** The container's user is named ``agent``
  in ``/etc/passwd`` even though it runs at the operator's UID.
  Tools that read username via ``getpass.getuser()`` will report
  ``agent`` while ``id -u`` reports the host UID.
* **Multi-operator hosts.** ``home_passthrough`` exposes the
  invoking operator's ``$HOME``, not a generic one. On shared
  machines, prefer explicit ``spec.mounts`` for repeatable behaviour.
