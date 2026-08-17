"""The POSIX-sh the target actually runs. Shell, kept readable AS shell.

Extracted from :mod:`_relocate_probe_script`, which had grown past the point
where the renderer could be read without scrolling through a hundred lines of
quoted shell. The split is by language, not by feature: this file is the remote
program, that one assembles it for a given set of questions and reads the answers
back.

TWO TARGET SHELLS ARE NOT BASH. scitex-nas-01 and scitex-nas-02 are QNAP busybox.
So: no ``[[``, no ``local``, no arrays, no ``/dev/tcp``, no process substitution,
and no ``awk`` programs carrying quotes. Anything richer is a script that renders
fine, runs on the developer's laptop, and prints garbage on the machine the fleet
is actually moving onto.

EVERY HELPER PRINTS A VALUE OR THE LITERAL ``unknown`` — never a silent empty
string, which the parser would have to guess about. That is the shell end of the
three-valued rule: a section that cannot measure something must say so, because
the alternative is a confident wrong answer.
"""

from __future__ import annotations

import shlex

__all__ = [
    "HELPERS",
    "SAC_SECTION",
    "SAC_WHERE_SECTION",
    "START_ACCEPT_SECTION",
    "TCP_TIMEOUT_S",
    "groups_section",
]

# Connect timeout for the TCP reachability probes RUN ON THE TARGET. Short on
# purpose: this is a dry run, and a port that needs more than 3s to answer is
# not a port an agent should be booted against.
TCP_TIMEOUT_S = 3


# The remote helpers. Every one prints a value or the literal `unknown`.
HELPERS = f"""
SACRELOC_TCP_PY=$(cat <<'SACRELOCPY'
import socket, sys
s = socket.socket()
s.settimeout({TCP_TIMEOUT_S})
sys.exit(s.connect_ex((sys.argv[1], int(sys.argv[2]))))
SACRELOCPY
)

# python3 FIRST, nc second. `nc -z` is not universal — some netcat builds reject
# the flag, and a rejected flag exits non-zero, which reads as "port closed".
# That is the failure this whole design exists to prevent, so nc is used only
# after its own usage text is confirmed to mention -z.
sacreloc_tcp() {{
  if command -v python3 >/dev/null 2>&1; then
    if python3 -c "$SACRELOC_TCP_PY" "$1" "$2" >/dev/null 2>&1; then
      echo yes
    else
      echo no
    fi
    return 0
  fi
  if command -v nc >/dev/null 2>&1 && nc -h 2>&1 | grep -q -- '-z'; then
    if nc -z -w {TCP_TIMEOUT_S} "$1" "$2" >/dev/null 2>&1; then
      echo yes
    else
      echo no
    fi
    return 0
  fi
  echo unknown
}}

sacreloc_listening() {{
  if command -v ss >/dev/null 2>&1; then
    ss -ltn 2>/dev/null
    return 0
  fi
  if command -v netstat >/dev/null 2>&1; then
    netstat -ltn 2>/dev/null
    return 0
  fi
  return 1
}}

# Prints `cred=<path>|<expiresAt>|<yes|no>`. The refreshToken VALUE never
# leaves the target — only whether it is a non-empty string.
sacreloc_cred() {{
  _e=$(tr ',' '\\n' < "$1" | sed -n 's/.*"expiresAt"[ ]*:[ ]*\\([0-9][0-9]*\\).*/\\1/p' | head -1)
  _r=$(tr ',' '\\n' < "$1" | sed -n 's/.*"refreshToken"[ ]*:[ ]*"\\([^"]*\\)".*/\\1/p' | head -1)
  if [ -n "$_r" ]; then _p=yes; else _p=no; fi
  echo "$M cred=$1|$_e|$_p"
}}
"""


# WHERE sac IS, asked three times on purpose. `sac_path` is `command -v sac`
# under the RAW non-interactive PATH — the one a bare `ssh host sac …` gets.
# `sac_usable` is the same lookup under the PATH THIS SCRIPT IS RUNNING WITH,
# i.e. after the peer's env_preamble, which is the PATH every command a
# relocation sends actually runs under; that is the question the check needs
# answered, and reading only the raw one failed hosts whose preamble already
# works (ywata-note-win, measured 2026-08-12). `sac_found` looks harder still:
# the login shell first (which is where a venv PATH comes from), then the
# locations sac is actually installed in across this fleet.
#
# Measured 2026-08-11 on scitex-compute-04: sac_path is empty and sac_found is
# /home/ywatanabe/.env-sac/bin/sac. Those two lines together say "installed, not
# reachable the way you are calling it", which is a different fix from "install
# it" — and a single lookup cannot tell them apart, because both produce the
# same "No such file or directory".
#
# An EMPTY value is a measurement here, not a missing one: `sac_found=` means
# looked-and-found-nothing. A section that never ran prints no line at all, and
# the adapter turns that absence into UNKNOWN.
SAC_WHERE_SECTION = """
sacreloc_find_sac() {
  if [ -n "$SHELL" ] && [ -x "$SHELL" ]; then
    _p=$("$SHELL" -lc 'command -v sac' 2>/dev/null | tail -1)
    if [ -n "$_p" ] && [ -x "$_p" ]; then echo "$_p"; return 0; fi
  fi
  for _c in "$HOME/.env-sac/bin/sac" "$HOME/.local/bin/sac" \
            /opt/venv-sac/bin/sac /usr/local/bin/sac /usr/bin/sac; do
    if [ -x "$_c" ]; then echo "$_c"; return 0; fi
  done
  echo ""
}
sacreloc_raw=$(PATH="$SACRELOC_PATH0" command -v sac 2>/dev/null)
echo "$M sac_path=$sacreloc_raw"
echo "$M sac_found=$(sacreloc_find_sac)"
echo "$M sac_usable=$(command -v sac 2>/dev/null)"
"""


# The two facts only the TARGET's own sac can answer: which runtimes its
# validator accepts, and which top-level spec keys it knows. Asked through the
# interpreter that BACKS the `sac` console script (read off its shebang), not
# whatever python3 is first on PATH — probing a different interpreter measures a
# different installation, which is the mistake `_hostsync._probe` documents at
# length. Both are best-effort: an older sac without these symbols prints
# nothing, the marker never appears, and the fact stays honestly unknown.
SAC_SECTION = """
SACRELOC_RUNTIMES_PY=$(cat <<'SACRELOCPY'
from scitex_agent_container.config._validation import _VALID_RUNTIMES as r
print(",".join(sorted(x for x in r if x)))
SACRELOCPY
)
SACRELOC_KEYS_PY=$(cat <<'SACRELOCPY'
from scitex_agent_container.config._validation import _KNOWN_TOP_LEVEL_KEYS as k
print(",".join(sorted(k)))
SACRELOCPY
)
py=python3
sacbin=$(command -v sac 2>/dev/null)
if [ -n "$sacbin" ]; then
  sb=$(head -1 "$sacbin" 2>/dev/null | sed -n 's|^#!\\([^ ]*\\).*|\\1|p')
  if [ -n "$sb" ] && [ -x "$sb" ]; then py=$sb; fi
fi
if command -v "$py" >/dev/null 2>&1; then
  rt=$("$py" -c "$SACRELOC_RUNTIMES_PY" 2>/dev/null)
  if [ -n "$rt" ]; then echo "$M runtimes=$rt"; fi
  sk=$("$py" -c "$SACRELOC_KEYS_PY" 2>/dev/null)
  if [ -n "$sk" ]; then echo "$M speckeys=$sk"; fi
fi
"""


# WOULD THE TARGET'S OWN `sac agents start` ACCEPT THIS AGENT? Asked of the
# target's sac rather than answered here: the drift guard IS the code that
# refuses the boot, and a second copy of its rule would pass on exactly the day
# the real one changed. Reuses `$py` from SAC_SECTION — the interpreter BACKING
# the target's `sac`, not whatever python3 is first on PATH.
#
# BOUNDED, because this one does network I/O: the guard runs a `git fetch` and
# the batch it lives in has ONE wall-clock budget for every fact, so a section
# that hung would cost all the others their answers. `timeout` where one exists.
#
# The dirty count is EVIDENCE, NOT VERDICT: the guard counts commits and refuses
# on those alone. It is taken because the remedy the guard prints is `git pull
# --ff-only`, which aborts on a dirty tree — 25 modified files in the dotfiles
# checkout backing ywata-note-win's agents dir, measured 2026-08-12.
START_ACCEPT_SECTION = """
SACRELOC_DRIFT_PY=$(cat <<'SACRELOCPY'
import os
from scitex_agent_container._drift import check_spec_source_drift
root = os.environ.get("SCITEX_DIR") or os.path.expanduser("~/.scitex")
s = check_spec_source_drift(os.path.join(root, "agent-container", "agents"))
print("%s|%d|%d|%s|%s" % (s.state.value, s.behind, s.ahead, s.repo, s.upstream))
SACRELOCPY
)
sacreloc_bounded() {
  if command -v timeout >/dev/null 2>&1; then
    timeout 25 "$@"
  else
    "$@"
  fi
}
if command -v "$py" >/dev/null 2>&1; then
  dr=$(sacreloc_bounded "$py" -c "$SACRELOC_DRIFT_PY" 2>/dev/null | tail -1)
  if [ -n "$dr" ]; then
    echo "$M startdrift=$dr"
    dr_repo=$(printf '%s' "$dr" | cut -d'|' -f4)
    if [ -n "$dr_repo" ] && command -v git >/dev/null 2>&1; then
      dr_n=$(sacreloc_bounded git -C "$dr_repo" status --porcelain 2>/dev/null | wc -l)
      echo "$M startdirty=$(printf '%s' "$dr_n" | tr -d ' ')"
    fi
  fi
fi
"""


def groups_section(labels_json: str) -> str:
    """Ask the TARGET's own sac what groups it makes of this spec's labels.

    Runs after :data:`SAC_SECTION` on purpose, reusing the ``$py`` that section
    resolved from the ``sac`` console script's shebang — the interpreter BACKING
    the installation, not whatever python3 leads the PATH.

    A PURE LABEL READ, deliberately. Resolving through the state db would ask
    whether the target already knows this agent, and it does not — it has never
    hosted it. The question that matters is whether the target's sac can read
    group labels AT ALL. Measured 2026-08-11: three hosts answered ``[]`` for
    every agent regardless of spec.yaml, and nine relocation probes were refused
    403 by exactly that.

    An old sac lacking the symbol prints NOTHING, the marker never arrives, and
    the fact stays honestly unknown rather than becoming an empty set that reads
    as a verdict.
    """
    return f"""
SACRELOC_GROUPS_PY=$(cat <<'SACRELOCPY'
import json, sys
from scitex_agent_container.config._group_resolver import all_named_groups
print(",".join(sorted(all_named_groups(json.loads(sys.argv[1])))))
SACRELOCPY
)
if command -v "$py" >/dev/null 2>&1; then
  gr=$("$py" -c "$SACRELOC_GROUPS_PY" {shlex.quote(labels_json)} 2>/dev/null)
  if [ $? -eq 0 ]; then echo "$M groups=$gr"; fi
fi
"""
