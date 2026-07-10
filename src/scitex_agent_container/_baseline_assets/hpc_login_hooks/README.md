# HPC login-node command whitelist hook

Canonical, version-controlled, tested source for the PreToolUse Bash
hook that keeps agents hosted ON an HPC login node (spartan-login*)
from running heavy compute there. Sibling in spirit to
`../git_identity_hooks/`: the authoritative copy lives here in the
package and is **propagated** into the fleet baseline that
`runtimes/_to_home.py` materializes into every agent's
`$HOME/.claude/`.

## Why

Operator directive (2026-07-10, his own design): agents run on
`spartan-login1.hpc.unimelb.edu.au` (unimelb Spartan). Login nodes are
the cluster's shared front door — heavy CPU/RAM/IO there degrades every
user's session and draws admin complaints (prior incidents: 2026-06-09
du/find GPFS scan, 2026-07-01 TeX compile). His words:

> login nodeでやってよいコマンドだけ並べて、whitelistでfilteringするのが
> 良い。error messageにフィードバックとして、slurm使えとか、module load
> 使えとか、srun overlapとか、scitex-hpc permanent使えとか言ったり

and, for the gating mechanism:

> hookで if node == spartan-login-node みたいに条件分岐

So: **whitelist, not deny-list** (the existing
`deny_heavy_spartan_login.sh` deny-lists heavy commands sent over
`ssh spartan ...` FROM other hosts; this hook is complementary — it
gates execution ON the node itself, and a whitelist fails safe for
command classes nobody predicted).

## What

- `enforce_hpc_login_node_whitelist.sh` — the PreToolUse Bash hook
  (wrapper: `--self-test`, enablement switch, bypasses, delegation).
- `hpc_login_whitelist_core.py` — the decision engine (hostname gate,
  quote-aware command parsing incl. heredoc bodies / wrappers /
  `bash -c` recursion, judging). **Fails open** on any introspection
  error (hostname resolution, bad gate regex, unparseable payload,
  missing core file): a broken hook must never brick the agent.
- `hpc_login_whitelist_policy.py` — the policy data: whitelist,
  heavy-git gate, blocked-command classes, and the per-class
  educational error texts. "Add a command / reword an error" never
  touches parsing logic.
- `settings.local.json.fragment.json` — the PreToolUse wiring snippet
  to merge into the baseline `settings.json` (see below).

### Gate (where the hook is live)

Hostname is matched (regex, case-insensitive) against
`$SAC_HPC_LOGIN_NODE_PATTERN` (default `spartan-login`; empty string
disables the hook entirely). Everywhere else the hook is a strict
no-op — shipping it fleet-wide is zero-risk.

### Whitelist (control-plane commands; rationale)

| group | commands | why |
| ----- | -------- | --- |
| SLURM | squeue sbatch scancel sacct sinfo srun salloc scontrol sstat sprio sshare seff sattach sbcast sdiag sreport | the point of a login node; `srun`/`sbatch`/`salloc` pass with ANY payload — it executes on a COMPUTE allocation |
| modules | module, ml | Lmod discovery/loading is control-plane |
| remote/transfer | ssh scp rsync sftp | launching AWAY from the node; small transfers (bulk → DTN) |
| git | git (day-to-day) | code sync is login work; `gc`/`fsck`/`repack`/`filter-branch`/… are gated (object-store-heavy IO) |
| nav/inspect | ls pwd cd echo printf date hostname whoami id uname which type stat file readlink realpath dirname basename df free uptime ps kill pkill sleep true false test [ | O(small) |
| text plumbing | cat head tail less more wc cut tr sort uniq diff grep rg fd sed awk jq tee touch mkdir cp mv rm ln chmod | agent bread-and-butter; `du`/`find`/`ncdu` are NOT here (2026-06-09 incident class — `fd` is the sanctioned lookup) |
| multiplexers | tmux screen | pane control, not compute |
| fleet CLIs | sac scitex-todo scitex-hpc scitex scitex-dev gh direnv | control plane by construction |
| network | curl wget | API calls (big downloads → job / DTN) |
| python | `python* -c` one-liners ≤ `$SAC_HPC_LOGIN_PYC_MAX` chars (default 500); `--version` | tiny introspection, not compute |

Per-host extension without editing the hook:
`SAC_HPC_LOGIN_EXTRA_ALLOW='cmd1,cmd2'`.

### Educational error catalogue (the operator's key ask)

Every block names the RIGHT route for that command class, plus the four
generic routes (srun --overlap onto an existing allocation / sbatch /
salloc / scitex-hpc permanent session / module load):

| class | examples | education |
| ----- | -------- | --------- |
| build_test | pytest make cmake gcc cargo nvcc mvn … | `srun --overlap --jobid <id> <cmd>`, `sbatch --wrap`, scitex-hpc permanent for dev loops |
| pkg_env | pip uv conda npm poetry apt spack … | `module load <name>` (`module spider` to search); env builds inside a job |
| container | apptainer singularity docker podman | build/exec as a job |
| archive_io | tar gzip xz zip zstd 7z … | inside a job; pure data movement → DTN |
| fs_scan | du ncdu find tree locate | `fd` for lookups, `df -h` for capacity, real scans in a job (2026-06-09) |
| tex | pdflatex latexmk biber … | srun --overlap / sbatch (2026-07-01) |
| interpreter | python script, ipython jupyter R matlab julia node … | srun --overlap / sbatch / salloc / scitex-hpc permanent; `python -c` one-liners stay allowed |
| pyc_too_long | python -c over the size guard | write a file, submit it |
| git_heavy | git gc/fsck/repack/… | run inside a job; day-to-day git stays whitelisted |
| script | bash foo.sh, ./run.sh | `sbatch <script>` (add #SBATCH headers) |
| default | anything else | if it computes, it belongs on a compute node |

### Fail-open (safety of the hook itself)

Hostname introspection failure, an invalid `$SAC_HPC_LOGIN_NODE_PATTERN`
regex, an unparseable payload, or a missing core file all WARN (stderr)
and ALLOW. Framing: a guardrail for cooperative agents, not a security
boundary — command/process substitution (`$(...)`, `<(...)`) payloads
are not descended into (same precision as the sibling hooks) and the
bypasses are documented:

- env escape: `SAC_HPC_LOGIN_ALLOW=1`
- inline marker: `# hook-bypass: hpc-login`

## How to deploy fleet-wide

The hook only FIRES once it is in the materialized baseline. The
package copy here is the source of truth; propagate it exactly like
`git_identity_hooks`:

1. Copy **all three** script files to the shared baseline pre-tool-use
   dir:

       <agents_dir>/_shared/to_home/.claude/hooks/pre-tool-use/enforce_hpc_login_node_whitelist.sh
       <agents_dir>/_shared/to_home/.claude/hooks/pre-tool-use/hpc_login_whitelist_core.py
       <agents_dir>/_shared/to_home/.claude/hooks/pre-tool-use/hpc_login_whitelist_policy.py

   (In this fleet that is
   `~/.dotfiles/src/.scitex/agent-container/agents/_shared/to_home/.claude/hooks/pre-tool-use/`,
   which `~/.scitex/agent-container/agents/_shared/to_home` symlinks
   to.) `runtimes/_to_home.py` materializes that tree into every
   agent's `$HOME/.claude/hooks/pre-tool-use/` on each start — no SIF
   rebuild. Spartan-hosted agents pick it up via the normal dotfiles
   sync to that host.

2. Merge the `PreToolUse` `Bash`-matcher entry from
   `settings.local.json.fragment.json` into the baseline
   `<agents_dir>/_shared/to_home/.claude/settings.json` `hooks` block
   (append it to the existing `Bash` matcher's `hooks` list, next to
   `deny_heavy_spartan_login.sh`).

3. Restart agents (or wait for the next natural restart). Claude Code
   re-reads `~/.claude/settings.json` on each session boot.

## Verification

- `bash enforce_hpc_login_node_whitelist.sh --self-test` — 50+ cases:
  gate on/off/custom-pattern/fail-open, every whitelist group, every
  blocked class, the educational message content, both bypasses, the
  extra-allow extension, and heredoc-body handling. Exit 0 iff all pass.
- Regression suite: `tests/integration/hpc_login_hooks/` drives the
  same scenarios from pytest via subprocess (no mocks).
