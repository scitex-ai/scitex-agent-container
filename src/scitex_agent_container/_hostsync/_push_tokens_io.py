"""Remote I/O for the token half of ``sac host push-config`` (ADR-0021).

The transport sibling of :mod:`._push_config_io` (which moves the peer's
config.yaml); this module moves — and, far more often, merely OBSERVES —
the peer's bearer tokens. :mod:`._push_tokens` holds the verdicts.

The rule that shapes every function here
----------------------------------------
**Digests cross the wire, never values.** The read path computes
``sha256`` ON THE PEER and returns hex digests, so a token value never
enters the master's process at all — which is a structural guarantee, not
a promise to be careful: code that never holds a secret cannot leak one.
Only the write path carries a value, it rides stdin (never the argv,
which is visible in the peer's process table), and it is never returned,
logged, or printed. :func:`.._listen.peer_tokens.list_peer_hosts` set
this precedent — "token VALUES are never returned" — and this module is
its remote-side twin.

Inherited verbatim from :mod:`._push_config_io`
-----------------------------------------------
* Paths expand REMOTELY (``$HOME`` inside the snippet). A locally
  expanded ``~`` yields the MASTER's home — the exact footgun this
  subsystem exists to kill.
* Marker-framed output; no ``end`` marker means UNDETERMINED, never
  "absent" and never "clean".
* Every snippet line is a complete command (ssh joins the argv with
  spaces and a login shell may execute the lines directly — see
  :func:`.._state._host_ssh.build_ssh_argv` for the two parse modes a
  dispatched snippet must survive).

Portability note the fleet forced
---------------------------------
``sha256sum`` is coreutils-only; mba is macOS, where the tool is
``shasum -a 256``. The digest helper tries both and falls back to
``openssl dgst``, on ONE line each so both dispatch parse modes survive
it. A peer with none of the three reports ``nodigest`` — which lands as
UNDETERMINED, never as a false match.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass, field

from .._state.host_config import PeerSpec, build_ssh_argv

__all__ = [
    "MARKER",
    "REMOTE_LISTEN_TOKEN_GLOB",
    "REMOTE_PEER_TOKENS_DIR",
    "REMOTE_TOKENS_DIR",
    "AuthProbe",
    "RemoteTokenRead",
    "probe_peer_listen_auth",
    "read_peer_tokens",
    "render_token_read_snippet",
    "render_token_write_snippet",
    "restart_peer_listen",
    "write_peer_token",
]

# Marker prefix for every parsed line — peer motd/rc noise can never be
# mistaken for token state (same discipline as ._push_config_io.MARKER).
MARKER = "SAC_PUSHTOK"

# Peer-side locations, expanded by the PEER's shell — never locally.
REMOTE_TOKENS_DIR = "$HOME/.scitex/agent-container/tokens"
REMOTE_PEER_TOKENS_DIR = "$HOME/.scitex/agent-container/peer-tokens"
REMOTE_LISTEN_TOKEN_GLOB = f"{REMOTE_TOKENS_DIR}/listen-*.token"


@dataclass(frozen=True)
class RemoteTokenRead:
    """One read of a peer's token state. ``ok=False`` == we do not know.

    ``listen_tokens`` / ``peer_tokens`` map a FILE NAME to a full sha256
    hex digest. Values are never present — see the module docstring.

    ``hostname`` is the peer's own ``hostname`` output, i.e. the name
    ``socket.gethostname()`` would return there. It is what decides WHICH
    ``listen-<host>.token`` a listen booting on that node reads
    (:func:`.._listen.tokens.default_token_path`), and it is the whole
    reason this read exists: the master cannot know it a priori, because
    on a multi-login-node cluster it changes between logins.
    """

    ok: bool
    detail: str = ""
    hostname: str = ""
    listen_tokens: dict[str, str] = field(default_factory=dict)
    peer_tokens: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class AuthProbe:
    """One authenticated request against a peer's listen.

    ``status`` is the HTTP status the peer's listen answered with, or
    ``-1`` when the request never got an answer (ssh/curl transport
    failure) — the same three-state shape ``_restart._http_get`` uses,
    and for the same reason: "I could not ask" must never be recorded as
    "it said no".

    ``rejected`` is the load-bearing field: ``True`` iff the listen
    ANSWERED and refused the bearer (401/403). A transport failure is
    neither accepted nor rejected — it is unknown.
    """

    status: int
    body: str = ""
    detail: str = ""

    @property
    def answered(self) -> bool:
        """True iff the listen answered at all (any HTTP status)."""
        return self.status > 0

    @property
    def rejected(self) -> bool:
        """True iff the listen answered AND refused the bearer."""
        return self.status in (401, 403)

    @property
    def accepted(self) -> bool:
        """True iff the listen answered and did NOT refuse the bearer.

        Deliberately not ``status == 200``: the auth middleware runs
        OUTSIDE the router, so any non-401/403 answer — including a 404
        — proves the bearer cleared the gate. Gating on 200 is the exact
        mistake ``wait_for_health`` documents (card
        ``sac-listen-restart-healthcheck-bearer``): it re-classified a
        live, answering daemon as dead and destroyed a healthy process.
        """
        return self.answered and not self.rejected


def render_token_read_snippet() -> str:
    """POSIX-sh token-state probe. Digests only; marker-framed.

    Emits, in order: the peer's ``hostname``; one ``listen=`` line per
    ``tokens/listen-*.token``; one ``peer=`` line per
    ``peer-tokens/*.token``; then ``end``. A directory that does not
    exist simply contributes no lines — its ABSENCE is read off the
    missing lines, while a transport failure omits ``end`` and is
    therefore UNDETERMINED. Those two must never collapse into one
    verdict, which is why the ``end`` marker is unconditional.

    ``_d`` is defined as a ONE-LINE function so it survives both
    dispatch parse modes (see the module docstring). It prints nothing
    when no digest tool exists, so the caller sees a file it could not
    digest rather than a file that "matched".
    """
    return (
        "\n"
        f"M={MARKER}\n"
        # One-line digest helper: coreutils, then macOS/BSD, then openssl.
        '_d() { if command -v sha256sum >/dev/null 2>&1; then sha256sum "$1" 2>/dev/null '
        "| cut -d' ' -f1; elif command -v shasum >/dev/null 2>&1; then shasum -a 256 \"$1\" "
        "2>/dev/null | cut -d' ' -f1; elif command -v openssl >/dev/null 2>&1; then openssl "
        "dgst -sha256 \"$1\" 2>/dev/null | awk '{print $NF}'; fi; }\n"
        'echo "$M hostname=$(hostname 2>/dev/null)"\n'
        f'for f in {REMOTE_LISTEN_TOKEN_GLOB}; do [ -f "$f" ] || continue; '
        'echo "$M listen=$(basename "$f") $(_d "$f")"; done\n'
        f'for f in {REMOTE_PEER_TOKENS_DIR}/*.token; do [ -f "$f" ] || continue; '
        'echo "$M peer=$(basename "$f") $(_d "$f")"; done\n'
        'echo "$M end"\n'
    )


def render_token_write_snippet(rel_paths: list[str], *, backup_stamp: str = "") -> str:
    """The remote token write: ONE value on stdin, landed at N paths.

    ``rel_paths`` are paths under ``$HOME/.scitex/agent-container``
    (e.g. ``tokens/listen-spartan.token``). Every path receives the SAME
    stdin bytes, which is the point: seeding both the hostname-keyed path
    a listen reads TODAY and the stable canonical path it should read
    tomorrow makes the two indistinguishable, so a listen booting on
    EITHER login node — and reading either file — comes up holding the
    same bearer. That is what collapses the FQDN ambiguity by
    construction rather than by hoping.

    ``umask 077`` + tmp-file + atomic ``mv`` mirror
    :func:`._push_config_io.render_write_snippet`: a dropped connection
    can never leave a half-written token, and the file lands 0600.
    ``set -eu`` aborts before any ``mv`` if an earlier step fails.

    ``backup_stamp`` non-empty copies each existing file to
    ``<file>.pre-rotate-<stamp>`` first — the peer-side half of the
    retain-until-verified rule.
    """
    if not rel_paths:
        raise ValueError("render_token_write_snippet: rel_paths must be non-empty")
    lines = [
        "\nset -eu\n",
        "umask 077\n",
        "v=$(cat)\n",  # the value: read ONCE from stdin, reused for every path
    ]
    for rel in rel_paths:
        if "'" in rel or '"' in rel or "$" in rel:
            raise ValueError(f"render_token_write_snippet: unsafe path {rel!r}")
        full = f"$HOME/.scitex/agent-container/{rel}"
        lines.append(f'p="{full}"\n')
        lines.append('mkdir -p "$(dirname "$p")"\n')
        if backup_stamp:
            lines.append(
                f'if [ -e "$p" ]; then cp -p "$p" "$p.pre-rotate-{backup_stamp}"; fi\n'
            )
        lines.append('printf %s "$v" > "$p.tmp"\n')
        lines.append('chmod 600 "$p.tmp"\n')
        lines.append('mv "$p.tmp" "$p"\n')
    return "".join(lines)


def _parse_read(stdout: str, *, rc: int, stderr: str) -> RemoteTokenRead:
    """Parse marker lines into a :class:`RemoteTokenRead`.

    No ``end`` marker means the probe did not finish — UNDETERMINED. A
    finished probe that lists no files is a peer with NO tokens, which is
    a real, actionable state (ABSENT); collapsing the two would let a
    dead transport read as "this peer has no tokens", and the rotation
    would then happily "fix" a peer it never saw.
    """
    hostname = ""
    listen_tokens: dict[str, str] = {}
    peer_tokens: dict[str, str] = {}
    saw_end = False
    for raw in stdout.splitlines():
        line = raw.strip()
        if not line.startswith(MARKER + " "):
            continue
        body = line[len(MARKER) + 1 :]
        if body == "end":
            saw_end = True
        elif body.startswith("hostname="):
            hostname = body[len("hostname=") :].strip()
        elif body.startswith("listen=") or body.startswith("peer="):
            key, _, rest = body.partition("=")
            name, _, digest = rest.partition(" ")
            target = listen_tokens if key == "listen" else peer_tokens
            target[name.strip()] = digest.strip()
    if not saw_end:
        tail = (stderr or "").strip().splitlines()[-1:] or [""]
        return RemoteTokenRead(
            ok=False,
            detail=(
                "token probe returned no end marker — peer token state unknown "
                f"(ssh exit {rc}: {tail[0][:120]})"
            ),
        )
    if not hostname:
        return RemoteTokenRead(
            ok=False,
            detail=(
                "token probe finished but the peer reported no hostname — "
                "cannot tell which listen-<host>.token its listen reads"
            ),
        )
    return RemoteTokenRead(
        ok=True,
        hostname=hostname,
        listen_tokens=listen_tokens,
        peer_tokens=peer_tokens,
    )


def _dispatch(
    peer: str,
    peers: dict[str, PeerSpec],
    command: list[str],
    *,
    timeout: int,
    runner,
    stdin: str | None = None,
) -> tuple[bool, subprocess.CompletedProcess | None, str]:
    """Run ``command`` on ``peer``. Returns ``(ok, proc, detail)``.

    The single ssh entry point for this module, so every call inherits
    :func:`build_ssh_argv`'s ``via:`` chain, ``env_preamble`` and
    SCITEX_DIR pinning — and so a call site added later cannot silently
    miss them. Never raises: every failure degrades to ``ok=False`` with
    an actionable detail, because an exception here would be an UNKNOWN
    dressed up as a crash.
    """
    try:
        argv = build_ssh_argv(
            peer,
            command,
            peers,
            extra_opts=["-o", f"ConnectTimeout={min(timeout, 15)}"],
        )
    except KeyError:
        return (
            False,
            None,
            f"peer '{peer}' is not defined in config.yaml — add it with:  "
            f"sac host add {peer} --ssh <user@host>",
        )
    try:
        kwargs = {
            "capture_output": True,
            "text": True,
            "timeout": timeout,
            "check": False,
        }
        if stdin is not None:
            kwargs["input"] = stdin
        proc = runner(argv, **kwargs)
    except subprocess.TimeoutExpired:
        return False, None, f"ssh timed out after {timeout}s"
    except (
        FileNotFoundError,
        OSError,
        subprocess.SubprocessError,
    ) as exc:  # stx-allow: fallback (reason: ssh spawn failure → UNKNOWN, never a false verdict)
        return False, None, f"ssh failed: {type(exc).__name__}: {exc}"
    return True, proc, ""


def read_peer_tokens(
    peer: str,
    peers: dict[str, PeerSpec],
    *,
    timeout: int = 30,
    runner=subprocess.run,
) -> RemoteTokenRead:
    """Read the peer's token DIGESTS + hostname. Never raises, never writes."""
    ok, proc, detail = _dispatch(
        peer,
        peers,
        ["sh", "-c", render_token_read_snippet()],
        timeout=timeout,
        runner=runner,
    )
    if not ok or proc is None:
        return RemoteTokenRead(ok=False, detail=detail)
    return _parse_read(proc.stdout or "", rc=proc.returncode, stderr=proc.stderr or "")


def write_peer_token(
    peer: str,
    peers: dict[str, PeerSpec],
    value: str,
    rel_paths: list[str],
    *,
    backup_stamp: str = "",
    timeout: int = 30,
    runner=subprocess.run,
) -> tuple[bool, str]:
    """Write ``value`` to every ``rel_paths`` entry on the peer. ``(ok, detail)``.

    ``value`` rides stdin — never the argv, which is world-readable in
    the peer's process table for the life of the command. ``detail``
    never contains ``value``; on failure it carries the remote stderr
    tail, which the write snippet keeps value-free by construction.
    """
    if not value:
        return False, "refusing to write an empty token value"
    ok, proc, detail = _dispatch(
        peer,
        peers,
        ["sh", "-c", render_token_write_snippet(rel_paths, backup_stamp=backup_stamp)],
        timeout=timeout,
        runner=runner,
        stdin=value,
    )
    if not ok or proc is None:
        return False, detail
    if proc.returncode != 0:
        tail = (proc.stderr or "").strip().splitlines()[-1:] or [""]
        return False, f"remote token write exited {proc.returncode}: {tail[0][:120]}"
    return True, ""


def restart_peer_listen(
    peer: str,
    peers: dict[str, PeerSpec],
    *,
    timeout: int = 120,
    runner=subprocess.run,
) -> tuple[bool, str]:
    """Restart the peer's ``sac listen``. Returns ``(ok, detail)``.

    Dispatches ``sac listen restart`` — the peer's OWN verb, which owns
    the stop/self-heal/relaunch/health sequence (:mod:`.._listen._restart`).
    We deliberately do not re-implement any of it here: a second
    restart implementation on the master would be a copy that drifts, and
    the peer's verb already fails loud with the real cause.

    This is THE step that makes a rotation take effect: a running listen
    read its token file ONCE at startup, so overwriting the file changes
    nothing in memory until the process restarts.
    """
    ok, proc, detail = _dispatch(
        peer, peers, ["sac", "listen", "restart"], timeout=timeout, runner=runner
    )
    if not ok or proc is None:
        return False, detail
    if proc.returncode != 0:
        stderr = (proc.stderr or "").strip().splitlines()
        tail = stderr[-1:] or [""]
        return (
            False,
            f"`sac listen restart` on the peer exited {proc.returncode}: {tail[0][:160]}",
        )
    return True, ""


# The authenticated route the verification probe hits. NOT ``/v1/health``:
# that path is in ``_listen.auth.BearerAuthMiddleware.PUBLIC_PATHS``, so it
# answers 200 to ANY bearer — including a stale one. A probe that cannot
# fail is not evidence, and verifying a rotation with one would report
# success for a listen that never adopted the new token. ``/agents`` is a
# read-only GET behind the bearer gate: right token -> not 401/403, wrong
# token -> 403.
AUTH_PROBE_PATH = "/agents"


def probe_peer_listen_auth(
    peer: str,
    peers: dict[str, PeerSpec],
    *,
    bearer: str,
    port: int = 7878,
    path: str = AUTH_PROBE_PATH,
    timeout: int = 30,
    runner=subprocess.run,
) -> AuthProbe:
    """GET ``path`` on the peer's listen with ``bearer``, via ssh + curl.

    The bearer rides an ``@-`` stdin config file (``curl --config``),
    never the argv: a token in the argv is readable by every user on the
    peer via ``ps`` for the life of the request, and this function's
    whole job is to handle a live bearer safely. ``--max-time`` bounds
    the request ON the peer; the ssh timeout bounds it here.

    Returns an :class:`AuthProbe`. A transport failure yields
    ``status=-1`` (unknown) rather than a rejection: "I could not ask" is
    not "it said no", and treating it as one would make a rotation
    report FAILED — and retain a backup forever — over a flaky link.
    """
    if not bearer:
        raise ValueError("probe_peer_listen_auth: bearer must be non-empty")
    # curl reads the header from stdin so the token never reaches the argv
    # (nor the peer's process table). ``-w`` appends the status on its own
    # line; ``-s`` keeps curl's progress meter out of the parse.
    remote = (
        f"curl -sS --config - --max-time {int(timeout)} "
        f'-o /dev/null -w "\\n{MARKER} status=%{{http_code}}\\n" '
        f"http://127.0.0.1:{int(port)}{path}"
    )
    config = f'header = "Authorization: Bearer {bearer}"\n'
    ok, proc, detail = _dispatch(
        peer,
        peers,
        ["sh", "-c", remote],
        timeout=timeout + 15,
        runner=runner,
        stdin=config,
    )
    if not ok or proc is None:
        return AuthProbe(status=-1, detail=detail)
    status = -1
    for raw in (proc.stdout or "").splitlines():
        line = raw.strip()
        if line.startswith(MARKER + " status="):
            try:
                status = int(line.split("=", 1)[1])
            except ValueError:
                status = -1
    if status <= 0:
        tail = (proc.stderr or "").strip().splitlines()[-1:] or [""]
        return AuthProbe(
            status=-1,
            detail=(
                "the peer's listen never answered the authenticated probe "
                f"(ssh exit {proc.returncode}: {tail[0][:120]})"
            ),
        )
    return AuthProbe(status=status)
