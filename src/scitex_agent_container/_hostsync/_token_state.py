"""Token-state verdicts for ``sac host push-config`` (ADR-0021 PR-B).

The READ half of the token channel: what does a peer hold, and does it
match the master? Mutates nothing — a structural guarantee, the same way
:mod:`._probe` is inert next to :mod:`._apply`. The rotation lives in
:mod:`._token_rotate`; the transport in :mod:`._push_tokens_io`.

The two legs a peer's token state has
-------------------------------------
Cross-host a2a needs a bearer in BOTH directions, and they are different
secrets (ADR-0020 §5 — the per-host blast radius of
:mod:`.._listen.peer_tokens`):

* **outbound** — the peer's ``peer-tokens/<master>.token`` must equal the
  MASTER's listen bearer. This is what spartan-dev presents when it calls
  home; if it drifts, peer → master a2a dies.
* **inbound** — the master's ``peer-tokens/<peer>.token`` must equal the
  PEER's listen bearer. This is what the master presents when it calls
  the peer; if it drifts, master → peer a2a dies.

Either leg can rot on its own, and today both rot SILENTLY. That is the
whole product here.

Why the peer's hostname is read, not assumed
--------------------------------------------
:func:`.._listen.tokens.default_token_path` keys the file on
``socket.gethostname()``. On a multi-login-node cluster that name is not
stable: a listen restarted on spartan-login2 reads a DIFFERENT file than
one started on spartan-login1, mints a fresh bearer when that file is
missing, and the master's copy silently stops matching. The master cannot
know which node it will reach, so it ASKS — and when several
``listen-*.token`` files disagree it reports UNDETERMINED rather than
guessing which one the running listen holds.

Secrecy
-------
Only 12-char sha256 prefixes reach a result, a log line, or JSON. The
read path digests ON THE PEER, so a peer's token value never enters this
process at all. Mirrors :func:`.._listen.peer_tokens.list_peer_hosts`,
which has never returned a value either.
"""

from __future__ import annotations

import enum
import hashlib
import secrets
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from .._listen.peer_tokens import default_peer_tokens_dir
from .._listen.tokens import default_token_path, read_token
from .._state.host_config import Config
from ._push_tokens_io import read_peer_tokens

__all__ = [
    "DEFAULT_LISTEN_PORT",
    "TokenStateResult",
    "TokenVerdict",
    "check_tokens_peer",
    "classify_token_state",
    "mint_bearer",
    "peer_listen_token_rel_paths",
    "read_master_bearer",
    "read_master_copy",
    "sha12",
    "stable_listen_token_name",
]

#: The ``sac listen`` bind port every host defaults to (``listen_cmds``'s
#: ``--bind 127.0.0.1:7878``). The rotation's verification probe needs a
#: port and no config key carries a PEER's listen port today, so this is
#: the default the CLI exposes as ``--listen-port``.
DEFAULT_LISTEN_PORT = 7878


class TokenVerdict(enum.Enum):
    """What the peer's bearer state is, relative to the master's."""

    #: Both legs observed and matching.
    TOKENS_CURRENT = "tokens-current"
    #: Observed, and at least one leg does not match. a2a is broken (or
    #: will be at the next restart) in at least one direction.
    TOKENS_DRIFTED = "tokens-drifted"
    #: Observed, and at least one leg's token file does not exist.
    TOKENS_ABSENT = "tokens-absent"
    #: We could not look, or what we saw is ambiguous. Never mutated,
    #: never reported clean.
    UNDETERMINED = "undetermined"


#: --check exit codes: worst-of across peers, mirroring ._push_config.
EXIT_CHECK = {
    TokenVerdict.TOKENS_CURRENT: 0,
    TokenVerdict.TOKENS_DRIFTED: 1,
    TokenVerdict.TOKENS_ABSENT: 1,
    TokenVerdict.UNDETERMINED: 2,
}


def sha12(value: str) -> str:
    """First 12 hex chars of ``sha256(value)`` — the ONLY token shape we print.

    12 hex chars is 48 bits: ample to tell two tokens apart by eye, and
    useless for recovering either. Empty in, empty out — so a missing
    token renders as an obvious blank rather than as the digest of
    ``""``, which would be a real-looking constant that silently matches
    every other missing token (a "these two agree" reading where the
    truth is "neither exists").
    """
    if not value:
        return ""
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:12]


def mint_bearer() -> str:
    """A new listen bearer, shape-identical to ``.._listen.tokens.ensure_token``.

    Same primitive (``secrets.token_urlsafe(32)``, 256 bits) so a rotated
    token is indistinguishable from a self-minted one — the peer's listen
    must not be able to tell, and neither must anything downstream.
    """
    return secrets.token_urlsafe(32)


def stable_listen_token_name(peer: str) -> str:
    """The canonical-name-keyed listen token FILE NAME for ``peer``.

    The FQDN fix (ADR-0021 §Tokens): ``listen-spartan.token`` instead of
    ``listen-spartan-login1.hpc.unimelb.edu.au.token``. Keyed on the
    peer's canonical name — the one identity that does NOT change when a
    login node does.
    """
    return f"listen-{peer}.token"


def peer_listen_token_rel_paths(peer: str, hostname: str) -> list[str]:
    """Every listen-token path a rotation must seed on ``peer``.

    Both the hostname-keyed path the peer's listen reads TODAY
    (:func:`.._listen.tokens.default_token_path`) and the stable
    canonical path it should read once its launcher pins ``--token-file``.
    Writing the SAME value to both is what makes the FQDN ambiguity
    harmless: whichever file the listen picks up, it comes up holding the
    rotated bearer. De-duplicated, because on a host whose hostname IS
    its canonical name (mba) the two paths are one.
    """
    rels = []
    for name in (f"listen-{hostname}.token", stable_listen_token_name(peer)):
        rel = f"tokens/{name}"
        if rel not in rels:
            rels.append(rel)
    return rels


@dataclass(frozen=True)
class TokenStateResult:
    """One peer's observed token state. Digests only — never a value."""

    peer: str
    verdict: TokenVerdict
    action: str = "none"
    exit_code: int = 0
    detail: str = ""
    peer_hostname: str = ""
    #: sha12 of the MASTER's own listen bearer (what the peer should hold).
    master_bearer_sha12: str = ""
    #: sha12 of the peer's ``peer-tokens/<master>.token`` (what it holds).
    peer_holds_master_sha12: str = ""
    #: sha12 of the PEER's listen bearer (what the master should hold).
    peer_bearer_sha12: str = ""
    #: sha12 of the master's ``peer-tokens/<peer>.token`` (what it holds).
    master_holds_peer_sha12: str = ""
    #: Every ``tokens/listen-*.token`` seen on the peer, name -> sha12.
    listen_files: dict[str, str] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.exit_code == 0

    def to_dict(self) -> dict:
        """JSON projection for ``--json``. Digests only, by construction."""
        return {
            "peer": self.peer,
            "verdict": self.verdict.value,
            "action": self.action,
            "exit_code": self.exit_code,
            "detail": self.detail,
            "peer_hostname": self.peer_hostname,
            "master_bearer_sha12": self.master_bearer_sha12,
            "peer_holds_master_sha12": self.peer_holds_master_sha12,
            "peer_bearer_sha12": self.peer_bearer_sha12,
            "master_holds_peer_sha12": self.master_holds_peer_sha12,
            "listen_files": dict(self.listen_files),
        }


def _live_listen_digest(listen_files: dict[str, str], hostname: str) -> tuple[str, str]:
    """The digest the peer's RUNNING listen holds. ``(digest, why_not)``.

    The honest reading of an ambiguous filesystem. A listen reads
    ``listen-<gethostname()>.token`` at ITS boot, on whichever node it
    runs on — which need not be the node this ssh landed on. So:

    * no listen token at all -> ``("", "")``: absent, decidable.
    * every file agrees -> that digest, whichever one it read.
    * files disagree -> ``("", <why>)``: UNDETERMINED. We genuinely do
      not know which node the listen booted on, and picking the file
      that happens to match the master's copy would be an answer chosen
      to agree with us — evidence that could not have disagreed.
    """
    if not listen_files:
        return "", ""
    if any(not d for d in listen_files.values()):
        undigested = sorted(n for n, d in listen_files.items() if not d)
        return "", (
            f"the peer could not digest {', '.join(undigested)} "
            "(no sha256sum / shasum / openssl there) — its token state is "
            "unreadable, which is not the same as unchanged"
        )
    digests = set(listen_files.values())
    if len(digests) == 1:
        return digests.pop(), ""
    listing = ", ".join(f"{n}=sha256:{d[:12]}" for n, d in sorted(listen_files.items()))
    return "", (
        f"the peer holds {len(digests)} DIFFERENT listen tokens ({listing}) and "
        f"its listen may have booted on any of them — this ssh reached "
        f"'{hostname}'. Which one the running listen serves is UNKNOWABLE from "
        "here. Rotate to collapse them to a single value:  "
        "sac host push-config --rotate-tokens <peer>"
    )


def _drift_legs(
    peer: str,
    master_name: str,
    *,
    master_sha: str,
    peer_holds_master: str,
    peer_bearer_sha: str,
    master_holds_sha: str,
) -> list[str]:
    """The mismatched legs, each naming the direction it breaks."""
    drifted = []
    if peer_holds_master and peer_holds_master != master_sha:
        drifted.append(
            f"OUTBOUND ({peer} -> {master_name} a2a) is BROKEN: the peer's "
            f"peer-tokens/{master_name}.token is sha256:{peer_holds_master}, "
            f"but the master's listen bearer is sha256:{master_sha}"
        )
    if peer_bearer_sha and master_holds_sha and peer_bearer_sha != master_holds_sha:
        drifted.append(
            f"INBOUND ({master_name} -> {peer} a2a) is BROKEN: the master's "
            f"peer-tokens/{peer}.token is sha256:{master_holds_sha}, but the "
            f"peer's listen bearer is sha256:{peer_bearer_sha}"
        )
    return drifted


def classify_token_state(
    peer: str,
    *,
    master_name: str,
    master_bearer: str,
    master_holds_peer: str,
    remote,
) -> TokenStateResult:
    """Classify a peer's token state against the master's. Pure.

    ``master_bearer`` / ``master_holds_peer`` are VALUES read from the
    master's own disk (the only place this process legitimately holds
    one); they are digested here and never stored on the result.
    """
    master_sha = sha12(master_bearer)
    master_holds_sha = sha12(master_holds_peer)

    if not remote.ok:
        return TokenStateResult(
            peer=peer,
            verdict=TokenVerdict.UNDETERMINED,
            exit_code=2,
            detail=remote.detail,
            master_bearer_sha12=master_sha,
            master_holds_peer_sha12=master_holds_sha,
        )

    listen_files = {n: d[:12] for n, d in sorted(remote.listen_tokens.items())}
    peer_holds_master = remote.peer_tokens.get(f"{master_name}.token", "")[:12]
    peer_bearer, ambiguous = _live_listen_digest(remote.listen_tokens, remote.hostname)
    peer_bearer_sha = peer_bearer[:12]

    common = {
        "peer": peer,
        "peer_hostname": remote.hostname,
        "master_bearer_sha12": master_sha,
        "peer_holds_master_sha12": peer_holds_master,
        "peer_bearer_sha12": peer_bearer_sha,
        "master_holds_peer_sha12": master_holds_sha,
        "listen_files": listen_files,
    }

    if not master_bearer:
        return TokenStateResult(
            verdict=TokenVerdict.UNDETERMINED,
            exit_code=2,
            detail=(
                "the MASTER has no listen bearer on disk "
                f"({default_token_path()}) — cannot say what '{peer}' should "
                "hold for it. Start the master's listen first:  sac listen start"
            ),
            **common,
        )
    if ambiguous:
        return TokenStateResult(
            verdict=TokenVerdict.UNDETERMINED, exit_code=2, detail=ambiguous, **common
        )

    absent = []
    if not peer_holds_master:
        absent.append(f"the peer has no peer-tokens/{master_name}.token")
    if not peer_bearer_sha:
        absent.append("the peer has no tokens/listen-*.token (its listen never ran)")
    if not master_holds_peer:
        absent.append(f"the master has no peer-tokens/{peer}.token")

    drifted = _drift_legs(
        peer,
        master_name,
        master_sha=master_sha,
        peer_holds_master=peer_holds_master,
        peer_bearer_sha=peer_bearer_sha,
        master_holds_sha=master_holds_sha,
    )

    note = ""
    expected = f"listen-{remote.hostname}.token"
    if listen_files and expected not in listen_files:
        note = (
            f"\n    NOTE: this ssh reached '{remote.hostname}', which has no "
            f"tokens/{expected} — a listen (re)started HERE would MINT a fresh "
            "bearer and silently desync the master. --rotate-tokens seeds both "
            "this node's path and the stable canonical one."
        )

    if drifted:
        detail = "; ".join(drifted)
        if absent:
            detail += " | also: " + "; ".join(absent)
        return TokenStateResult(
            verdict=TokenVerdict.TOKENS_DRIFTED,
            exit_code=1,
            detail=detail + note,
            **common,
        )
    if absent:
        return TokenStateResult(
            verdict=TokenVerdict.TOKENS_ABSENT,
            exit_code=1,
            detail="; ".join(absent) + note,
            **common,
        )
    return TokenStateResult(
        verdict=TokenVerdict.TOKENS_CURRENT,
        exit_code=0,
        detail=(
            f"both legs match: the peer holds the master's bearer "
            f"(sha256:{master_sha}) and the master holds the peer's "
            f"(sha256:{peer_bearer_sha})"
        )
        + note,
        **common,
    )


def read_master_bearer(token_path: Path | None = None) -> str:
    """The master's OWN listen bearer VALUE, or ``""``. Never logged."""
    return read_token(token_path or default_token_path()) or ""


def read_master_copy(peer: str, tokens_dir: Path | None = None) -> str:
    """The master's ``peer-tokens/<peer>.token`` VALUE, or ``""``.

    Reads the file directly rather than via
    :func:`.._listen.peer_tokens.read_peer_token`, which RAISES on a
    missing file. A missing copy is a normal, reportable state here
    (TOKENS_ABSENT) — not an exception.
    """
    tdir = tokens_dir if tokens_dir is not None else default_peer_tokens_dir()
    src = tdir / f"{peer}.token"
    if not src.is_file():
        return ""
    try:
        return src.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def check_tokens_peer(
    peer: str,
    cfg: Config,
    *,
    master_name: str = "",
    timeout: int = 30,
    runner=subprocess.run,
    tokens_dir: Path | None = None,
    master_token_path: Path | None = None,
) -> TokenStateResult:
    """Read-only token verdict for one peer. Mutates nothing, ever."""
    master = master_name or cfg.canonical_host()
    remote = read_peer_tokens(peer, cfg.peers, timeout=timeout, runner=runner)
    return classify_token_state(
        peer,
        master_name=master,
        master_bearer=read_master_bearer(master_token_path),
        master_holds_peer=read_master_copy(peer, tokens_dir),
        remote=remote,
    )
