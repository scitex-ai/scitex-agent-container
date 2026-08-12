#!/bin/bash
# -*- coding: utf-8 -*-
# File: src/scitex_agent_container/_baseline_assets/image_build_hooks/deny_raw_apptainer_build.sh
#
# Description: PreToolUse hook for Bash. Refuses a HAND-RUN
# `apptainer build` / `singularity build` of scitex-agent-container's OWN
# images, and names `sac image build` as the command to run instead.
#
# DECLARED BY scitex-agent-container (the leaf owns the rule about its own
# images), APPLIED BY scitex-dev through the `scitex_dev.hooks` entry-point
# group -- see the sibling README.md and
# ``scitex_agent_container._claude_hooks_plugin``. Do not hand-place this
# file; it is deployed to $HOME/.claude/hooks/pre-tool-use/.
#
# WHY THIS RULE EXISTS
# --------------------
# sac's image build is NOT `apptainer build` plus arguments. `sac image
# build` stages a build-context DIRECTORY holding the .def alongside a
# `scitex-agent-container-src/` copy of the installed package, and the .def
# resolves its `%files` sources against that staging dir. apptainer-base.def
# documents the contract in its own %files comment:
#
#     # Bundle the package's OWN source tree so the in-SIF sac is the
#     # source tree that shipped this .def -- never a `git+...@main`
#     # snapshot of whatever happened to be on a branch at build time.
#     ...
#     # apptainer resolves the relative source path below against the
#     # build CWD, which the CLI sets to that staging dir.
#     scitex-agent-container-src /opt/scitex-agent-container-src
#
# A hand-run build skips the staging. It does NOT fail loudly -- it produces
# a SIF whose in-image sac is whatever happened to be lying around, and the
# mismatch surfaces weeks later as a version that makes no sense. That is
# the "artifact exists, but not built from the tree you think" class.
#
# The build is additionally becoming STAGED (base -> scitex -> ...), where a
# hand-run build also bypasses parent-chain resolution and staleness
# checking, silently layering a child on a stale or missing parent.
#
# This is NOT a duplicate of enforce_heavy_job_demotion.sh. That hook judges
# only whether a heavy command was NICE'd; a fully demoted
# `nice -n 19 ionice -c 2 -n 7 apptainer build ... apptainer-base.def`
# sails through it today. This hook closes that gap.
#
# THE DISCRIMINATOR, AND WHY
# --------------------------
# `apptainer build` against an unrelated image is legitimate and MUST keep
# working. A guard that blocks unrelated builds gets disabled, and then the
# real rule is gone with it. So we block only when the build is
# demonstrably one of OURS, keyed on the recipe's CONTENT -- not on its
# filename, not on the output SIF's name:
#
#   PRIMARY   a readable file argument declaring the label key
#             `org.scitex.layer` (every sac recipe declares it under
#             %labels). Content-keyed, so it survives the .def renames
#             now in flight, survives the recipe directory moving, and
#             still catches a .def copied to /tmp. It is also the SAME
#             notion of "ours" the built artifact carries --
#             `apptainer inspect --labels x.sif` reports
#             `org.scitex.layer: base` -- so guard and artifact agree.
#
#             We match the KEY and stop. A matcher spelled
#             `org\.scitex\.layer (base|scitex|proxy)` would silently stop
#             catching new stages once `system-deps` / `python-pkgs`
#             appear, while still passing its own tests -- a guard whose
#             trigger is narrower than its stated rule.
#
#   FALLBACK  the command text names sac's own recipe dir
#             (`scitex_agent_container/containers`) or sac's SIF output
#             dir (`.scitex/agent-container/containers`). This is the only
#             signal available when the content cannot be read: a build
#             driven over ssh on another host, or a path not yet created.
#
# Honest limit: a build whose recipe is unreadable AND which names none of
# sac's directories passes through. That is the safe direction to be wrong.
#
# NOT blocked, deliberately:
#   - `sac image build ...`              -- the sanctioned path
#   - `containers/spartan-sif-bake.sh`   -- the sanctioned remote bake. It
#        does its OWN $CTX staging, so it earns the exemption on merit.
#   - read-only apptainer verbs          -- inspect / exec / run / instance
#   - any build carrying no sac signal
#
# Bypass (rare -- know why you are doing it):
#   1. Append marker `hook-bypass: raw-apptainer-build` to the command.
#   2. Or export `SAC_ALLOW_RAW_IMAGE_BUILD=1`.

set -u

THIS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LOG_PATH="${SCITEX_AGENT_CONTAINER_HOOK_LOG_PATH:-$THIS_DIR/.$(basename "$0").log}"

# ------------------------------------------------------------------
# Decision engine.
#
# Kept in python rather than grep because "is this a build?" needs a token
# walk: `apptainer --debug build x.sif y.def` IS a build, while
# `apptainer exec img.sif make build` is NOT, and no honest single regex
# separates those. It must also see through two real-world wrappers --
# an ABSOLUTE argv[0] (`/usr/bin/apptainer`, which is what
# spartan-sif-bake.sh:275 produces via `command -v`) and a quoted
# `bash -c "... exec \"$APPTAINER\" build ..."` under srun/ssh, where the
# build appears only INSIDE an argument. An argv[0] match misses both;
# re-splitting builder-bearing tokens catches them.
#
# Prints `<reason>|<evidence>` and exits 2 when the command must be
# refused; exits 0 silently otherwise.
# ------------------------------------------------------------------
_decide() {
    python3 -c '
import os
import re
import shlex
import sys

RAW = sys.stdin.read()

# The label KEY every sac recipe declares under %labels. Deliberately
# unanchored to any value set, so a new stage is caught the day it lands.
LABEL_KEY = re.compile(r"^[ \t]*org\.scitex\.layer\b", re.MULTILINE)

# sac`s own recipe dir / SIF output dir. Used ONLY when content is unreadable.
PATH_MARKERS = (
    "scitex_agent_container/containers",
    "scitex-agent-container/containers",
    ".scitex/agent-container/containers",
)

# Sanctioned wrappers that do their own staging and are exempt on merit.
SANCTIONED = ("spartan-sif-bake.sh",)

BUILDERS = ("apptainer", "singularity")


def tokens(text):
    """Best-effort shell split; falls back to whitespace on unbalanced quotes."""
    try:
        return shlex.split(text, comments=False, posix=True)
    except ValueError:
        return text.split()


def invokes_build(toks):
    """True if an apptainer/singularity token is followed by the `build` verb.

    basename() so an ABSOLUTE argv[0] (/usr/bin/apptainer) still matches.
    Leading dashes are skipped so `apptainer --debug build` counts, while
    `apptainer exec img.sif make build` does not (first bare word is exec).
    """
    for i, tok in enumerate(toks):
        if os.path.basename(tok) not in BUILDERS:
            continue
        for nxt in toks[i + 1:]:
            if nxt.startswith("-"):
                continue
            if nxt == "build":
                return True
            break
    return False


def _subcommands(toks, text):
    """Tokens that are themselves commands (a quoted `bash -c` payload)."""
    for tok in toks:
        if tok != text and any(b in tok for b in BUILDERS):
            yield tok


def build_detected(text):
    toks = tokens(text)
    if invokes_build(toks):
        return True
    return any(invokes_build(tokens(sub)) for sub in _subcommands(toks, text))


def flatten(text):
    toks = tokens(text)
    out = list(toks)
    for sub in _subcommands(toks, text):
        out.extend(tokens(sub))
    return out


def sac_recipe_argument(toks):
    """First readable file argument that declares the sac layer label."""
    for tok in toks:
        if not tok or tok.startswith("-"):
            continue
        path = os.path.expanduser(tok)
        if not os.path.isfile(path):
            continue
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as fh:
                head = fh.read(200_000)
        except OSError:
            continue
        if LABEL_KEY.search(head):
            return os.path.abspath(path)
    return None


if any(w in RAW for w in SANCTIONED):
    sys.exit(0)

if not build_detected(RAW):
    sys.exit(0)

hit = sac_recipe_argument(flatten(RAW))
if hit:
    print("recipe|" + hit)
    sys.exit(2)

for marker in PATH_MARKERS:
    if marker in RAW:
        print("path|" + marker)
        sys.exit(2)

sys.exit(0)
' 2>/dev/null
}

# ------------------------------------------------------------------
# Self-test -- the measured refuse/allow pair, runnable at any time:
#   bash deny_raw_apptainer_build.sh --self-test
# ------------------------------------------------------------------
if [[ "${1:-}" == "--self-test" ]]; then
    echo "=== Self-test: $(basename "$0") ==="
    pass=0
    fail=0
    tmp="$(mktemp -d)"
    trap 'rm -rf "$tmp"' EXIT

    # Stand-in for a sac recipe: carries the label KEY and nothing else, so
    # the test proves we key on the key -- not on a filename, not on a
    # known layer VALUE.
    cat >"$tmp/some-renamed-stage.def" <<'DEF'
Bootstrap: docker
From: ubuntu@sha256:dead
%labels
    org.scitex.layer a-stage-that-does-not-exist-yet
DEF
    # Stand-in for somebody else's recipe: no sac label.
    cat >"$tmp/unrelated.def" <<'DEF'
Bootstrap: docker
From: alpine:3.20
%labels
    org.example.layer whatever
DEF

    # Re-invoke through `bash "$SELF"` rather than bare `"$0"`: $0 is
    # whatever argv[0] the caller used, and a bare relative name is not on
    # PATH -- that spelling makes every case exit 127 and the suite reports
    # a uniform failure that looks like a logic bug.
    SELF="$(cd "$(dirname "$0")" && pwd)/$(basename "$0")"

    run() {
        local desc="$1" cmd="$2" want="$3" rc
        printf '%s' "{\"tool_name\":\"Bash\",\"tool_input\":{\"command\":$(printf '%s' "$cmd" |
            python3 -c 'import sys,json;print(json.dumps(sys.stdin.read()))')}}" |
            bash "$SELF" >/dev/null 2>&1
        rc=$?
        if [[ "$rc" == "$want" ]]; then
            echo "  PASS (rc=$rc) $desc"
            pass=$((pass + 1))
        else
            echo "  FAIL got $rc want $want: $desc -- cmd: $cmd"
            fail=$((fail + 1))
        fi
    }

    echo "-- REFUSED: a sac image built by hand --"
    run "raw build of a sac recipe" \
        "apptainer build out.sif $tmp/some-renamed-stage.def" 2
    run "singularity alias" \
        "singularity build out.sif $tmp/some-renamed-stage.def" 2
    run "global flag before the verb" \
        "apptainer --debug build out.sif $tmp/some-renamed-stage.def" 2
    run "sandbox form advertised in the def header" \
        "apptainer build --sandbox base/ $tmp/some-renamed-stage.def" 2
    run "sudo-wrapped" \
        "sudo apptainer build --fakeroot out.sif $tmp/some-renamed-stage.def" 2
    run "fully demoted (heavy-job hook would allow this)" \
        "nice -n 19 ionice -c 2 -n 7 apptainer build out.sif $tmp/some-renamed-stage.def" 2
    run "absolute argv[0] inside a quoted bash -c" \
        "bash -c 'exec /usr/bin/apptainer build --force p.sif $tmp/some-renamed-stage.def'" 2
    run "wheel recipe dir by path (file unreadable)" \
        "apptainer build out.sif /opt/x/scitex_agent_container/containers/apptainer-scitex.def" 2
    run "remote build naming sac's SIF dir" \
        "ssh spartan 'apptainer build ~/.scitex/agent-container/containers/sac-base.sif r.def'" 2

    echo "-- ALLOWED: everything else keeps working --"
    run "unrelated recipe" \
        "apptainer build out.sif $tmp/unrelated.def" 0
    run "unrelated docker uri" \
        "apptainer build myimage.sif docker://ubuntu:24.04" 0
    run "the sanctioned CLI" \
        "sac image build base -y" 0
    run "the sanctioned bake wrapper" \
        "bash src/scitex_agent_container/containers/spartan-sif-bake.sh --layer base" 0
    run "read-only inspect of a sac SIF" \
        "apptainer inspect --labels ~/.scitex/agent-container/containers/sac-base.sif" 0
    run "read-only deffile of a sac SIF" \
        "apptainer inspect --deffile ~/.scitex/agent-container/containers/sac-base.sif" 0
    run "exec whose argv merely contains the word build" \
        "apptainer exec img.sif make build" 0
    run "apptainer version" "apptainer --version" 0
    run "not the Bash tool" "" 0

    echo "-- BYPASS --"
    run "marker bypass" \
        "apptainer build out.sif $tmp/some-renamed-stage.def # hook-bypass: raw-apptainer-build" 0
    SAC_ALLOW_RAW_IMAGE_BUILD=1 run "env-var bypass" \
        "apptainer build out.sif $tmp/some-renamed-stage.def" 0

    echo "pass=$pass fail=$fail"
    [[ $fail -eq 0 ]] && exit 0 || exit 1
fi

# ------------------------------------------------------------------
# Enablement switch (centralized project-switch/switch.yaml), when present.
# ------------------------------------------------------------------
HELPER_SCRIPT="$(dirname "$THIS_DIR")/project-switch/hook_switch_helper.sh"
if [[ -f "$HELPER_SCRIPT" ]]; then
    # shellcheck source=/dev/null
    source "$HELPER_SCRIPT"
    if declare -f check_hook_enabled_or_exit >/dev/null 2>&1; then
        check_hook_enabled_or_exit "$(basename "$0")"
    fi
fi

# ------------------------------------------------------------------
# Env-var escape
# ------------------------------------------------------------------
[[ "${SAC_ALLOW_RAW_IMAGE_BUILD:-}" == "1" ]] && exit 0

# ------------------------------------------------------------------
# Read input + extract command (Bash tool only)
# ------------------------------------------------------------------
INPUT="$(cat)"

CMD=$(printf '%s' "$INPUT" | python3 -c '
import json, sys
try:
    d = json.load(sys.stdin)
except Exception:
    sys.exit(0)
if d.get("tool_name", "") != "Bash":
    sys.exit(0)
print((d.get("tool_input", {}) or {}).get("command", "") or "", end="")
' 2>/dev/null) || exit 0
[[ -z "$CMD" ]] && exit 0

# ------------------------------------------------------------------
# String-marker escape
# ------------------------------------------------------------------
if printf '%s' "$CMD" | grep -qF 'hook-bypass: raw-apptainer-build'; then
    exit 0
fi

# ------------------------------------------------------------------
# Decide. Fail OPEN: if python3 is missing or the engine errors, a broken
# guard must not wedge the agent's Bash tool.
# ------------------------------------------------------------------
VERDICT="$(printf '%s' "$CMD" | _decide)"
[[ $? == 2 ]] || exit 0
[[ -n "$VERDICT" ]] || exit 0

REASON="${VERDICT%%|*}"
EVIDENCE="${VERDICT#*|}"

if [[ "$REASON" == "recipe" ]]; then
    TRIGGER="Triggered by this file:
  $EVIDENCE
It declares the label key \`org.scitex.layer\` under %labels, which is how a
sac recipe identifies itself — the same label \`apptainer inspect --labels\`
reports on the built SIF."
else
    TRIGGER="Triggered by: the command names sac's own image directory
  $EVIDENCE
(the recipe itself was not readable from here — e.g. a build driven on
another host — so the path is the only available signal.)"
fi

printf '[%s] BLOCK %s :: %s :: %s\n' \
    "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$REASON" "$EVIDENCE" "$CMD" \
    >>"$LOG_PATH" 2>/dev/null || true

cat >&2 <<EOF
BLOCKED by deny_raw_apptainer_build.sh: hand-run \`apptainer build\` of a sac image.

$TRIGGER

Build it through the CLI instead — same recipe, correct build context:

  sac image build base            # or: scitex | proxy
  sac image build base -y         # non-interactive
  sac image build --help          # remote / reproducible options

WHY THIS IS BLOCKED
sac's image build is not \`apptainer build\` plus arguments. \`sac image build\`
stages a build-context directory holding the .def alongside a
\`scitex-agent-container-src/\` copy of the installed package, and the .def
resolves its \`%files\` sources against that staging dir. apptainer-base.def
says so itself: it bundles "the package's OWN source tree so the in-SIF sac is
the source tree that shipped this .def — never a \`git+...@main\` snapshot".

A hand-run build skips the staging and does NOT fail loudly. It produces a SIF
whose in-image sac is whatever happened to be lying around, and you find out
weeks later from a version that makes no sense. The build is also becoming
staged (base → scitex → …), where a hand-run build additionally bypasses
parent-chain resolution and staleness checking — silently layering a child on
a stale or missing parent.

STILL ALLOWED
  - \`apptainer build\` of anything that is not ours (no \`org.scitex.layer\`)
  - read-only verbs: \`apptainer inspect --labels|--deffile\`, exec, run
  - \`sac image build\` and sac's own \`spartan-sif-bake.sh\` remote bake

Rare override (know why you're doing it):
  SAC_ALLOW_RAW_IMAGE_BUILD=1   or append   # hook-bypass: raw-apptainer-build
EOF

exit 2

# EOF
