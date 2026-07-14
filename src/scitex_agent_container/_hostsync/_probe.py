"""READ-ONLY remote probe for ``sac host sync``. Mutates nothing, ever.

That is a structural guarantee, not a comment: the single mutating
operation lives in :mod:`._apply`, so this module can be read — and
audited — as inert. ``sac host sync --check`` runs ONLY this.

How the checkout is located (the whole trick)
---------------------------------------------
We do NOT guess a path like ``~/proj/scitex-agent-container``. Two
independent reasons, both measured on Spartan 2026-07-14, not inferred:

1. A ``~`` in a REMOTE path expands on the CALLING process — the
   CENTRE's home, not the peer's. This is the exact footgun
   :mod:`.._state.host_registry` exists to keep out of sac.
2. Even the right-looking path lies. On Spartan
   ``~/proj/scitex-agent-container`` is a SYMLINK into
   ``/data/gpfs/projects/punim0264/ywatanabe/scitex-agent-container``,
   and ``~/.scitex`` had silently been a symlink into an unrelated paper
   project for weeks.

So we ask the peer's own interpreter where it LOADS sac from
(``scitex_agent_container.__file__``) and take the git toplevel that
CONTAINS that file. The tree we reconcile is then, by construction, the
tree that actually backs the running code — not a path we hoped was
right. The same probe is re-run after the fast-forward to verify.

And we probe with the interpreter that BACKS THE ``sac`` CONSOLE SCRIPT
(read off its shebang), not with whatever ``python3`` happens to be
first on PATH. Probing a different interpreter than the one that runs
sac would be measuring the wrong thing — the classic mistake of
inspecting a shadow instead of asking the thing.

Version strings are never consulted. They are a proven liar: this repo
shipped NINE tags that published nothing, and ywata-note-win's
``.dist-info`` still reports 0.21.11 beside current code. Only the
loaded module PATH and a live SYMBOL are evidence.
"""

from __future__ import annotations

import shlex
import subprocess

from .._state.host_config import PeerSpec, build_ssh_argv
from ._model import GraphState, PeerSyncReport

__all__ = ["MARKER", "SYMBOL_PROBE", "probe_peer", "render_probe_snippet"]

# Every line we parse back carries this prefix, so a peer's motd / rc
# noise can never be mistaken for probe output.
MARKER = "SAC_SYNC"

# The symbol we assert is really loaded. A version string proves nothing;
# an imported symbol with a real signature proves the module on disk is
# the module in memory. Kept as one constant so the probe and the
# post-sync verification can never drift apart.
SYMBOL_PROBE = (
    "import inspect; "
    "from scitex_agent_container._state import port_allocator as p; "
    "print(list(inspect.signature(p.claim_port).parameters))"
)

# Cap on how many dirty paths / ahead-commits we haul back, so a
# catastrophically dirty peer cannot flood the operator's terminal. The
# COUNT is always exact (computed remotely); only the listing is capped.
_LIST_CAP = 40


def render_probe_snippet(ref: str = "") -> str:
    """Render the POSIX-sh probe that runs on the peer.

    ``ref`` is the git ref to reconcile against; empty means "the
    checkout's own ``@{upstream}``", which makes the default behaviour
    exactly ``git pull --ff-only``. It is shell-quoted here, so a
    hostile-looking ref cannot break out of the snippet.

    Every failure path still prints a marker line — the parser must
    never be left guessing, because "no output" would otherwise be
    indistinguishable from "clean".
    """
    ref_q = shlex.quote(ref)
    return rf"""
set -u
M={MARKER}

# The interpreter that actually backs `sac` here — not whatever python3
# is first on PATH. If sac is not installed, fall back to python3 so we
# can still report WHY rather than dying silently.
py=python3
sacbin=$(command -v sac 2>/dev/null || true)
if [ -n "$sacbin" ]; then
  shebang=$(head -1 "$sacbin" 2>/dev/null | sed -n 's|^#!\([^ ]*\).*|\1|p')
  if [ -n "$shebang" ] && [ -x "$shebang" ]; then py="$shebang"; fi
fi
echo "$M interpreter=$py"

mod=$("$py" -c "import scitex_agent_container as s; print(s.__file__)" 2>/dev/null || true)
if [ -z "$mod" ]; then
  echo "$M state=no-module"
  echo "$M end"
  exit 0
fi
echo "$M module=$mod"

# Git toplevel CONTAINING the loaded module — never a guessed path.
top=$(git -C "$(dirname "$mod")" rev-parse --show-toplevel 2>/dev/null || true)
if [ -z "$top" ]; then
  echo "$M state=not-a-checkout"
  echo "$M end"
  exit 0
fi
echo "$M repo=$top"

if ! git -C "$top" fetch --quiet 2>/dev/null; then
  echo "$M state=fetch-failed"
  echo "$M end"
  exit 0
fi

ref={ref_q}
if [ -z "$ref" ]; then
  ref=$(git -C "$top" rev-parse --abbrev-ref --symbolic-full-name '@{{upstream}}' 2>/dev/null || true)
fi
if [ -z "$ref" ]; then
  echo "$M state=no-upstream"
  echo "$M end"
  exit 0
fi
echo "$M target=$ref"

target_sha=$(git -C "$top" rev-parse --verify --quiet "$ref^{{commit}}" 2>/dev/null || true)
if [ -z "$target_sha" ]; then
  echo "$M state=bad-ref"
  echo "$M end"
  exit 0
fi
echo "$M target_sha=$target_sha"
echo "$M head=$(git -C "$top" rev-parse HEAD 2>/dev/null)"

# The object graph — content-addressed, exact, clock-independent.
# mtimes are NOT used: a plain `git pull` rewrites them without changing
# content, and GPFS clock skew across hosts makes them meaningless.
echo "$M ahead=$(git -C "$top" rev-list --count "$target_sha"..HEAD 2>/dev/null)"
echo "$M behind=$(git -C "$top" rev-list --count HEAD.."$target_sha" 2>/dev/null)"

# What a --force would be destroying. Printed BEFORE any decision, so
# nobody discards work they never saw.
git -C "$top" log --no-merges --format="$M ahead_commit=%h %s" \
    -n {_LIST_CAP} "$target_sha"..HEAD 2>/dev/null || true

git -C "$top" status --porcelain 2>/dev/null | head -n {_LIST_CAP} \
  | while IFS= read -r line; do echo "$M dirty=$line"; done

sym=$("$py" -c {shlex.quote(SYMBOL_PROBE)} 2>/dev/null || true)
echo "$M symbol=$sym"
echo "$M end"
"""


def _parse(peer: str, stdout: str) -> PeerSyncReport:
    """Parse marker lines into a :class:`PeerSyncReport`.

    Truncated output (no ``end`` sentinel) is UNREACHABLE, never
    "clean" — a probe that did not finish has told us nothing, and
    rendering that as a pass is how a false-green ships stale code.
    """
    fields: dict[str, str] = {}
    dirty: list[str] = []
    ahead_commits: list[str] = []
    saw_end = False
    for raw in stdout.splitlines():
        line = raw.strip()
        if not line.startswith(MARKER + " "):
            continue
        body = line[len(MARKER) + 1 :]
        if body == "end":
            saw_end = True
            continue
        key, _, value = body.partition("=")
        if key == "dirty":
            dirty.append(value)
        elif key == "ahead_commit":
            ahead_commits.append(value)
        else:
            fields[key] = value

    if not saw_end:
        return PeerSyncReport(
            peer=peer,
            state=GraphState.UNREACHABLE,
            detail="probe output truncated (no end marker) — peer state unknown",
        )

    early = fields.get("state")
    if early == "no-module":
        return PeerSyncReport(
            peer=peer,
            state=GraphState.NO_MODULE,
            detail=(
                "scitex_agent_container is not importable by the interpreter "
                f"backing sac ({fields.get('interpreter', '?')})"
            ),
        )
    if early == "not-a-checkout":
        return PeerSyncReport(
            peer=peer,
            state=GraphState.NOT_A_CHECKOUT,
            module=fields.get("module", ""),
            detail=(
                "sac is installed as a plain wheel, not an editable checkout — "
                "there is no git tree here to reconcile. Reinstall it from a "
                "checkout, or upgrade it with pip."
            ),
        )
    if early in ("fetch-failed", "no-upstream", "bad-ref"):
        reasons = {
            "fetch-failed": "git fetch failed on the peer (offline / auth / timeout)",
            "no-upstream": "the peer's branch has no upstream; pass --ref explicitly",
            "bad-ref": "the requested ref does not resolve on the peer",
        }
        return PeerSyncReport(
            peer=peer,
            state=GraphState.UNREACHABLE,
            repo=fields.get("repo", ""),
            module=fields.get("module", ""),
            detail=reasons[early],
        )

    def _int(key: str) -> int | None:
        try:
            return int(fields[key])
        except (KeyError, ValueError):
            return None

    ahead = _int("ahead")
    behind = _int("behind")
    if ahead is None or behind is None:
        return PeerSyncReport(
            peer=peer,
            state=GraphState.UNREACHABLE,
            repo=fields.get("repo", ""),
            detail="could not compare HEAD with the target ref on the peer",
        )

    if ahead and behind:
        state = GraphState.DIVERGED
    elif ahead:
        state = GraphState.AHEAD
    elif behind:
        state = GraphState.BEHIND
    else:
        state = GraphState.CURRENT

    return PeerSyncReport(
        peer=peer,
        state=state,
        head=fields.get("head", ""),
        target=fields.get("target", ""),
        target_sha=fields.get("target_sha", ""),
        ahead=ahead,
        behind=behind,
        repo=fields.get("repo", ""),
        module=fields.get("module", ""),
        symbol=fields.get("symbol", ""),
        dirty_files=tuple(dirty),
        ahead_commits=tuple(ahead_commits),
    )


def probe_peer(
    peer: str,
    peers: dict[str, PeerSpec],
    *,
    ref: str = "",
    timeout: int = 120,
    runner=subprocess.run,
) -> PeerSyncReport:
    """ssh ``peer`` and read its sac checkout state. Never raises, never writes.

    Rides :func:`build_ssh_argv` — sac's single remote-dispatch choke
    point — so the peer's ``via:`` ProxyJump chain and ``env_preamble``
    (Lmod on Spartan) apply automatically, and a call site added later
    cannot silently miss them.

    Args:
        peer: peer key from config.yaml's ``peers:`` block.
        peers: parsed peers map (ssh target + jump chain + preamble).
        ref: git ref to reconcile against; ``""`` = the peer's own
            ``@{upstream}`` (making the default exactly ``pull --ff-only``).
        timeout: ssh wall-clock cap. A hung peer is UNREACHABLE — which
            is UNKNOWN, and therefore never syncable.
        runner: injectable ``subprocess.run``-shaped callable (a real one;
            tests pass real callables and PATH shims, never mocks).

    Returns:
        A :class:`PeerSyncReport`. Every failure degrades to an
        undetermined state carrying an actionable ``detail`` — never an
        exception, and never a false "clean".
    """
    try:
        ssh_argv = build_ssh_argv(
            peer,
            ["sh", "-c", render_probe_snippet(ref)],
            peers,
            extra_opts=["-o", f"ConnectTimeout={min(timeout, 15)}"],
        )
    except KeyError:
        return PeerSyncReport(
            peer=peer,
            state=GraphState.UNREACHABLE,
            detail=(
                f"peer '{peer}' is not defined in config.yaml — "
                f"add it with:  sac host add {peer} --ssh <user@host>"
            ),
        )
    try:
        proc = runner(
            ssh_argv, capture_output=True, text=True, timeout=timeout, check=False
        )
    except subprocess.TimeoutExpired:
        return PeerSyncReport(
            peer=peer,
            state=GraphState.UNREACHABLE,
            detail=f"ssh timed out after {timeout}s",
        )
    except (
        FileNotFoundError,
        OSError,
        subprocess.SubprocessError,
    ) as exc:  # stx-allow: fallback (reason: ssh missing / spawn error → UNKNOWN, never a crash and never a false-clean)
        return PeerSyncReport(
            peer=peer,
            state=GraphState.UNREACHABLE,
            detail=f"ssh failed: {type(exc).__name__}: {exc}",
        )
    report = _parse(peer, proc.stdout or "")
    if report.state is GraphState.UNREACHABLE and not report.detail.startswith("probe"):
        return report
    if report.state is GraphState.UNREACHABLE:
        tail = (proc.stderr or "").strip().splitlines()[-1:] or [""]
        return PeerSyncReport(
            peer=peer,
            state=GraphState.UNREACHABLE,
            detail=f"{report.detail} (ssh exit {proc.returncode}: {tail[0][:120]})",
        )
    return report
