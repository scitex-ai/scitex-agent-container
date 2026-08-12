"""One remote script, one round trip, and an answer that degrades PER FACT.

:mod:`_relocate_probe` defines the port — eleven callables, and a failed one
must produce ``None``. This module is half the adapter: the POSIX-sh script the
target runs, and the parser that turns its output back into facts. Nothing here
opens a connection; it renders a string and reads a string, so every parse rule
below is unit-testable against captured output from a real host.

WHY ONE BATCHED CALL. Eleven sequential ssh round trips to a NAS across a home
LAN is tens of seconds for a command whose entire job is to say "not yet". The
script answers everything in one connection.

WHY BATCHING IS DANGEROUS, AND WHAT IS DONE ABOUT IT. The obvious batched
implementation returns one blob and one status: if anything goes wrong the
caller has eleven unknowns, or — far worse — eleven confident falses. Three
rules keep the batch honest, and they are the reason this module exists as its
own file:

    1. NO ``set -e``.  A section that fails must not abort the sections after
       it. Each answer is printed the moment it is known, so a later failure
       cannot retract an earlier measurement.
    2. EVERY FACT IS ITS OWN MARKER LINE.  Facts are never positional and never
       share a line, so an unparseable field can only cost its own fact. The
       parser reads keys, never offsets.
    3. A FACT IS ONLY OBSERVED IF ITS LINE IS PRESENT.  Absence is never read as
       a negative. :func:`parse_probe_output` returns what it SAW; the adapter
       raises for anything missing, which :func:`.._relocate_probe.probe` turns
       into ``None``.

Together those mean a script that dies halfway still yields honest partial
facts: the ones printed before the failure are observed, the rest are unknown,
and no fact is ever inferred from another fact's absence.

THE MARKER PREFIX IS LOAD-BEARING. A login shell prints motd, direnv notices,
Lmod chatter and "Welcome to Ubuntu" banners onto the same stdout. Every line we
consume starts with ``SAC_RELOC ``, so a peer's noise can never be mistaken for a
measurement — the same defence :mod:`.._hostsync._probe` uses, for the same
reason.

TWO TARGET SHELLS ARE NOT BASH. scitex-nas-01 and scitex-nas-02 are QNAP
busybox. So: no ``[[``, no ``local``, no arrays, no ``/dev/tcp``, no process
substitution, and no ``awk`` programs carrying quotes. Anything richer is a
script that renders fine, runs on the developer's laptop, and prints garbage on
the machine the fleet is actually moving onto.
"""

from __future__ import annotations

import shlex
from dataclasses import dataclass, field

__all__ = [
    "MARKER",
    "REMOTE_DEFAULT_CREDENTIAL",
    "CredentialLine",
    "RemoteQuestions",
    "RemoteReadout",
    "parse_probe_output",
    "render_probe_script",
]

#: Prefix on every line this module consumes. Peer motd/banner noise never
#: carries it, so it cannot be mistaken for a measurement.
MARKER = "SAC_RELOC"

#: The SDK's default credential location, expanded BY THE TARGET's shell. It is
#: always checked in addition to whatever the spec declares.
REMOTE_DEFAULT_CREDENTIAL = "$HOME/.claude/.credentials.json"

# Connect timeout for the TCP reachability probes RUN ON THE TARGET. Short on
# purpose: this is a dry run, and a port that needs more than 3s to answer is
# not a port an agent should be booted against.
_TCP_TIMEOUT_S = 3


@dataclass(frozen=True)
class RemoteQuestions:
    """What to ask the target. Each field an empty default meaning DO NOT ASK.

    An unasked question is not a negative answer: the section is omitted from
    the script, the marker never appears, and the adapter reports the fact as
    unknown with a reason. That keeps "the spec declares no image" from
    rendering as "the image is missing on the target".
    """

    #: Absolute path of the agent's SIF on the target.
    image: str = ""
    #: Bind SOURCE paths (the left half of ``src:dst:mode``).
    bind_sources: tuple[str, ...] = ()
    #: Card store endpoint AS THE AGENT WOULD DIAL IT FROM THE TARGET. A
    #: loopback host is correct here and is deliberately probed: after the move
    #: the agent runs ON the target, so ``127.0.0.1:5432`` means the TARGET's
    #: postgres — which is exactly the 2026-08-07 failure (5432 here, 5442
    #: there).
    card_store_host: str = ""
    card_store_port: int = 0
    #: Candidate credential files, in the spec's own order.
    credential_paths: tuple[str, ...] = ()
    #: Ports the spec PINS. The script answers "which of THESE are listening",
    #: not "enumerate every port" — the check only ever intersects.
    required_ports: tuple[int, ...] = ()
    #: The hub, as an address that means something ON THE TARGET. Unlike the
    #: card store, a loopback hub address must NOT be probed: from the target it
    #: would measure the TARGET's own loopback and report the hub down (or up)
    #: for a reason that has nothing to do with the hub.
    hub_host: str = ""
    hub_port: int = 0


@dataclass(frozen=True)
class CredentialLine:
    """One credential file that EXISTS on the target, described without leaking.

    ``expires_at_ms`` is the raw ``claudeAiOauth.expiresAt``; ``refresh_present``
    says only whether ``refreshToken`` is a non-empty string. The token value
    itself is never printed by the script and never travels — an ssh transcript
    of a preflight must not be a credential leak.
    """

    path: str
    expires_at_ms: float | None
    refresh_present: bool | None


@dataclass(frozen=True)
class RemoteReadout:
    """What the target actually said. Only what it said.

    ``complete`` records whether the closing marker arrived. It is reported, not
    acted on: a truncated run still yields every fact printed before the cut,
    because each was true when printed.
    """

    started: bool = False
    complete: bool = False
    fields: dict[str, str] = field(default_factory=dict)
    missing_binds: tuple[str, ...] = ()
    credentials: tuple[CredentialLine, ...] = ()
    ports_in_use: tuple[int, ...] = ()


def _q(value: str) -> str:
    return shlex.quote(value)


def _tcp_section(name: str, host: str, port: int) -> str:
    if not host or port <= 0:
        return ""
    return f'echo "$M {name}=$(sacreloc_tcp {_q(host)} {port})"\n'


def render_probe_script(questions: RemoteQuestions, *, preamble: str = "") -> str:
    """Render the POSIX-sh script that measures ``questions`` on the target.

    ``preamble`` is the peer's ``env_preamble`` from ``config.yaml`` (Spartan's
    Lmod loads, scitex-compute-03's venv PATH). It runs first because without it
    ``sac`` is not on PATH there, and the two facts that ask the target's own
    validator would silently go unanswered on exactly the hosts the fleet is
    moving onto.

    Every interpolated value is ``shlex.quote``d. Paths come from a spec file,
    which is operator-authored but not therefore harmless — a bind source with a
    space in it would otherwise split into two failing tests and report a
    missing path that does not exist.
    """
    parts: list[str] = []
    # Captured BEFORE the preamble, which exists precisely to put things on PATH.
    # The sac-presence question is about the PATH a bare `ssh host sac …` runs
    # under, so measuring it after the preamble would answer a different
    # question in the same words — and answer it "yes" on exactly the hosts
    # (scitex-compute-03/-04) where the bare form fails.
    parts.append('SACRELOC_PATH0="$PATH"')
    if preamble.strip():
        parts.append(preamble.strip())
    # No `set -e`: a section that fails must not abort the sections after it.
    # That single omission is what makes this batch degrade per fact.
    parts.append(f"M={MARKER}")
    parts.append(_HELPERS)
    parts.append('echo "$M begin"')
    parts.append('echo "$M epoch=$(date +%s)"')

    if questions.image:
        parts.append(
            f"if [ -e {_q(questions.image)} ]; then\n"
            '  echo "$M image=present"\n'
            "else\n"
            '  echo "$M image=absent"\n'
            "fi"
        )

    if questions.bind_sources:
        listed = " ".join(_q(b) for b in questions.bind_sources)
        parts.append(
            f"for b in {listed}; do\n"
            '  if [ -e "$b" ]; then :; else echo "$M bind_missing=$b"; fi\n'
            "done\n"
            f'echo "$M binds_checked={len(questions.bind_sources)}"'
        )

    card = _tcp_section(
        "cardstore", questions.card_store_host, questions.card_store_port
    )
    if card:
        parts.append(card.rstrip("\n"))

    # ALWAYS asked, even when the spec names no candidate: the SDK's default
    # location is where a stale file actually bit us (2026-08-07), and `~` cannot
    # be expanded here — it would expand to the CALLER's home, not the target's.
    # So `$HOME` is left for the remote shell, unquoted, on purpose.
    cred_paths = " ".join(_q(c) for c in questions.credential_paths)
    parts.append(
        f'for c in {cred_paths} "{REMOTE_DEFAULT_CREDENTIAL}"; do\n'
        '  if [ -f "$c" ]; then sacreloc_cred "$c"; fi\n'
        "done\n"
        f'echo "$M creds_checked={len(questions.credential_paths) + 1}"'
    )

    if questions.required_ports:
        listed = " ".join(str(p) for p in questions.required_ports)
        parts.append(
            "ports_out=$(sacreloc_listening)\n"
            "if [ $? -eq 0 ]; then\n"
            f"  for p in {listed}; do\n"
            '    if printf "%s\\n" "$ports_out" | grep -qE "[:.]$p([[:space:]]|$)"; then\n'
            '      echo "$M port_in_use=$p"\n'
            "    fi\n"
            "  done\n"
            f'  echo "$M ports_checked={len(questions.required_ports)}"\n'
            "else\n"
            '  echo "$M ports_tool=none"\n'
            "fi"
        )
    else:
        # Nothing pinned: the honest answer is "none of the zero required ports
        # is in use", which needs no tool on the target and cannot be wrong.
        parts.append('echo "$M ports_checked=0"')

    hub = _tcp_section("hub", questions.hub_host, questions.hub_port)
    if hub:
        parts.append(hub.rstrip("\n"))

    parts.append(_SAC_WHERE_SECTION)
    parts.append(_SAC_SECTION)
    parts.append(_START_ACCEPT_SECTION)
    parts.append('echo "$M end"')
    return "\n".join(parts) + "\n"


# The remote helpers, kept out of the renderer so the shell is readable as
# shell. Every one prints a value or the literal `unknown` — never a silent
# empty string, which the parser would have to guess about.
_HELPERS = f"""
SACRELOC_TCP_PY=$(cat <<'SACRELOCPY'
import socket, sys
s = socket.socket()
s.settimeout({_TCP_TIMEOUT_S})
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
    if nc -z -w {_TCP_TIMEOUT_S} "$1" "$2" >/dev/null 2>&1; then
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
_SAC_WHERE_SECTION = """
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
_SAC_SECTION = """
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
# the real one changed. Reuses `$py` from _SAC_SECTION — the interpreter BACKING
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
_START_ACCEPT_SECTION = """
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


def _parse_cred(value: str) -> CredentialLine | None:
    """``<path>|<expiresAt>|<yes|no>`` -> a :class:`CredentialLine`.

    A malformed field costs only itself: an unparseable expiry yields ``None``
    for the expiry while the path and the refresh-token flag survive.
    """
    path, sep, rest = value.partition("|")
    if not sep or not path:
        return None
    raw_expiry, _, raw_refresh = rest.partition("|")
    try:
        expires_at_ms: float | None = float(raw_expiry)
    except ValueError:
        expires_at_ms = None
    refresh: bool | None
    if raw_refresh == "yes":
        refresh = True
    elif raw_refresh == "no":
        refresh = False
    else:
        refresh = None
    return CredentialLine(
        path=path, expires_at_ms=expires_at_ms, refresh_present=refresh
    )


def parse_probe_output(stdout: str) -> RemoteReadout:
    """Read marker lines into a :class:`RemoteReadout`. Never raises.

    Reports what was SEEN and nothing else. There is deliberately no defaulting
    here: a fact whose line never arrived is simply absent from ``fields``, and
    it is the adapter — not the parser — that turns absence into an unknown. A
    parser that filled in ``False`` for a missing line would destroy the whole
    three-valued chain in one convenient-looking place.
    """
    fields: dict[str, str] = {}
    missing_binds: list[str] = []
    credentials: list[CredentialLine] = []
    ports: list[int] = []
    started = False
    complete = False

    for raw in stdout.splitlines():
        line = raw.strip()
        if not line.startswith(MARKER + " "):
            continue
        body = line[len(MARKER) + 1 :]
        if body == "begin":
            started = True
            continue
        if body == "end":
            complete = True
            continue
        key, sep, value = body.partition("=")
        if not sep:
            continue
        if key == "bind_missing":
            missing_binds.append(value)
        elif key == "cred":
            parsed = _parse_cred(value)
            if parsed is not None:
                credentials.append(parsed)
        elif key == "port_in_use":
            try:
                ports.append(int(value))
            except ValueError:
                continue
        else:
            fields[key] = value

    return RemoteReadout(
        started=started,
        complete=complete,
        fields=fields,
        missing_binds=tuple(missing_binds),
        credentials=tuple(credentials),
        ports_in_use=tuple(ports),
    )
