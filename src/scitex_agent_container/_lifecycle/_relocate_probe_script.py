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

from ._relocate_probe_shell import (
    HELPERS as _HELPERS,
)
from ._relocate_probe_shell import (
    SAC_SECTION as _SAC_SECTION,
)
from ._relocate_probe_shell import (
    SAC_WHERE_SECTION as _SAC_WHERE_SECTION,
)
from ._relocate_probe_shell import (
    START_ACCEPT_SECTION as _START_ACCEPT_SECTION,
)
from ._relocate_probe_shell import (
    TCP_TIMEOUT_S as _TCP_TIMEOUT_S,
)
from ._relocate_probe_shell import (
    groups_section as _groups_section,
)

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
    #: Directories the agent must RUN IN — today just ``spec.workdir``, which
    #: apptainer receives as ``--pwd``. Tested with ``-d`` rather than ``-e``: a
    #: FILE where the workdir should be is not a workdir, and apptainer's failure
    #: for that case is as opaque as for an absent one.
    workdirs: tuple[str, ...] = ()
    #: ``metadata.labels`` as JSON, for the target's own resolver to read. Sent
    #: as one quoted argument so a label with a space or a quote in it cannot
    #: reshape the remote command.
    group_labels_json: str = ""
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
    missing_workdirs: tuple[str, ...] = ()
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

    if questions.workdirs:
        listed = " ".join(_q(w) for w in questions.workdirs)
        parts.append(
            f"for w in {listed}; do\n"
            '  if [ -d "$w" ]; then :; else echo "$M workdir_missing=$w"; fi\n'
            "done\n"
            f'echo "$M workdirs_checked={len(questions.workdirs)}"'
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
    if questions.group_labels_json:
        parts.append(_groups_section(questions.group_labels_json))
    parts.append('echo "$M end"')
    return "\n".join(parts) + "\n"


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
    missing_workdirs: list[str] = []
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
        elif key == "workdir_missing":
            missing_workdirs.append(value)
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
        missing_workdirs=tuple(missing_workdirs),
        credentials=tuple(credentials),
        ports_in_use=tuple(ports),
    )
