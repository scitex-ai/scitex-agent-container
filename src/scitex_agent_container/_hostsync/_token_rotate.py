"""Token WRITE paths for ``sac host push-config`` (ADR-0021 PR-B).

The mutating half of the token channel, kept apart from
:mod:`._token_state` the same way :mod:`._apply` is kept apart from
:mod:`._probe`: the read side can then be audited as inert, and the two
operations that can split the fleet's credentials live in one file short
enough to read whole.

Two verbs, deliberately very different in blast radius:

* :func:`push_master_bearer` — the OUTBOUND leg only. Writes the
  master's own bearer to the peer's ``peer-tokens/<master>.token``.
  Cannot break anything that was working (it is what the peer is
  SUPPOSED to hold) and needs no restart, because the forwarder reads
  that registry per-request (ADR-0020 §6).
* :func:`rotate_peer_tokens` — mints the peer's OWN new bearer and
  replaces it on both sides. This one CAN break live a2a, so it is
  single-peer, refuses on any unknown state, restarts the peer's listen,
  and proves the result with a falsifiable probe before discarding
  anything.

Why a rotation must restart the peer's listen
---------------------------------------------
A listen reads its token file ONCE, at startup
(``listen_cmds._do_start_listen`` -> ``ensure_token``). Overwriting the
file changes NOTHING in the running process. A rotation that skipped the
restart would leave the master holding a bearer the peer's listen does
not honour — master → peer a2a silently dead, with both files on disk
looking perfectly consistent. The restart is not a nicety; it is the
step that makes the write real.
"""

from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .._listen.peer_tokens import default_peer_tokens_dir, write_peer_token
from .._listen.tokens import default_token_path
from .._state.host_config import Config
from ._push_config import ConfigVerdict, check_config_peer
from ._push_tokens_io import (
    AuthProbe,
    probe_peer_listen_auth,
    read_peer_tokens,
    restart_peer_listen,
)
from ._push_tokens_io import write_peer_token as write_remote_token
from ._token_state import (
    DEFAULT_LISTEN_PORT,
    TokenStateResult,
    TokenVerdict,
    mint_bearer,
    peer_listen_token_rel_paths,
    read_master_bearer,
    read_master_copy,
    sha12,
)

__all__ = ["RotateResult", "push_master_bearer", "rotate_peer_tokens"]


@dataclass(frozen=True)
class RotateResult:
    """Outcome of one ``--rotate-tokens`` run. Digests only, never a value.

    ``action`` is ``rotated`` / ``refused`` / ``failed``. ``backup``
    names the retained master-side pre-rotate copy when (and only when)
    verification did not pass — the operator's undo.
    """

    peer: str
    action: str
    exit_code: int
    detail: str = ""
    new_sha12: str = ""
    old_sha12: str = ""
    backup: str = ""
    restarted: bool = False
    verified: bool = False

    @property
    def ok(self) -> bool:
        return self.exit_code == 0

    def to_dict(self) -> dict:
        return {
            "peer": self.peer,
            "action": self.action,
            "exit_code": self.exit_code,
            "detail": self.detail,
            "new_sha12": self.new_sha12,
            "old_sha12": self.old_sha12,
            "backup": self.backup,
            "restarted": self.restarted,
            "verified": self.verified,
        }


def push_master_bearer(
    peer: str,
    cfg: Config,
    *,
    master_name: str = "",
    timeout: int = 30,
    runner=subprocess.run,
    master_token_path: Path | None = None,
) -> TokenStateResult:
    """Ensure the peer's ``peer-tokens/<master>.token`` is the master's bearer.

    Verified by re-reading the peer's DIGEST and comparing it to the
    master's — never by the write's exit code. A write that returns 0 and
    lands nothing is a case this codebase has already paid for.
    """
    master = master_name or cfg.canonical_host()
    bearer = read_master_bearer(master_token_path)
    path = master_token_path or default_token_path()
    if not bearer:
        return TokenStateResult(
            peer=peer,
            verdict=TokenVerdict.UNDETERMINED,
            action="refused",
            exit_code=2,
            detail=(
                f"the master has no listen bearer at {path} — nothing to push. "
                "Start the master's listen first:  sac listen start"
            ),
        )
    rel = f"peer-tokens/{master}.token"
    ok, detail = write_remote_token(
        peer, cfg.peers, bearer, [rel], timeout=timeout, runner=runner
    )
    if not ok:
        return TokenStateResult(
            peer=peer,
            verdict=TokenVerdict.UNDETERMINED,
            action="failed",
            exit_code=2,
            detail=f"could not write {rel} on '{peer}': {detail}",
            master_bearer_sha12=sha12(bearer),
        )
    after = read_peer_tokens(peer, cfg.peers, timeout=timeout, runner=runner)
    if not after.ok:
        return TokenStateResult(
            peer=peer,
            verdict=TokenVerdict.UNDETERMINED,
            action="failed",
            exit_code=2,
            detail=(
                f"the write returned success but the read-back failed "
                f"({after.detail}) — sac does not report a success it cannot "
                "substantiate"
            ),
            master_bearer_sha12=sha12(bearer),
        )
    got = after.peer_tokens.get(f"{master}.token", "")[:12]
    if got != sha12(bearer):
        return TokenStateResult(
            peer=peer,
            verdict=TokenVerdict.TOKENS_DRIFTED,
            action="failed",
            exit_code=2,
            detail=(
                f"wrote {rel} on '{peer}' but it reads back "
                f"sha256:{got or '<absent>'}, not the master's "
                f"sha256:{sha12(bearer)}"
            ),
            master_bearer_sha12=sha12(bearer),
            peer_holds_master_sha12=got,
            peer_hostname=after.hostname,
        )
    return TokenStateResult(
        peer=peer,
        verdict=TokenVerdict.TOKENS_CURRENT,
        action="pushed",
        exit_code=0,
        detail=(
            f"the peer now holds the master's bearer (sha256:{sha12(bearer)}); "
            "verified by digest read-back. No listen restart needed — the "
            "forwarder reads peer-tokens/ per request."
        ),
        master_bearer_sha12=sha12(bearer),
        peer_holds_master_sha12=got,
        peer_hostname=after.hostname,
    )


def _verify_rotation(
    peer: str,
    cfg: Config,
    *,
    new_token: str,
    port: int,
    timeout: int,
    runner,
) -> tuple[bool, str]:
    """Did the peer's listen actually adopt ``new_token``? ``(ok, detail)``.

    TWO probes, because one cannot disagree. The new token must be
    ACCEPTED, and a freshly minted bogus token must be REJECTED.

    The control probe is the point. ``/v1/health`` is in
    :attr:`.._listen.auth.BearerAuthMiddleware.PUBLIC_PATHS`, so it
    answers 200 to any bearer at all; verifying against it would be a
    test that passes whether or not the rotation worked. Even against
    the authenticated ``/agents``, a single positive cannot distinguish
    "the listen adopted our token" from "this listen admits everything".
    The bogus probe is what makes the answer falsifiable — and this is
    the one step in the flow where being wrong silently splits the
    fleet's credentials.
    """
    good = probe_peer_listen_auth(
        peer, cfg.peers, bearer=new_token, port=port, timeout=timeout, runner=runner
    )
    if not good.answered:
        return False, (
            f"the peer's listen did not answer the authenticated probe on "
            f"127.0.0.1:{port} ({good.detail}) — the rotation is UNVERIFIED"
        )
    if good.rejected:
        return False, (
            f"the peer's listen REJECTED the new bearer (HTTP {good.status}) — "
            "it did not adopt the rotated token. Its restart either failed, or "
            "it reads a token file other than the ones seeded"
        )
    control: AuthProbe = probe_peer_listen_auth(
        peer,
        cfg.peers,
        bearer=mint_bearer(),
        port=port,
        timeout=timeout,
        runner=runner,
    )
    if not control.answered:
        return False, (
            "the control probe got no answer — cannot confirm the peer's auth "
            "gate is discriminating, so the rotation stays UNVERIFIED "
            f"({control.detail})"
        )
    if not control.rejected:
        return False, (
            f"the peer's listen ADMITTED a bogus bearer (HTTP {control.status}) "
            "— its auth gate is not discriminating, so accepting the new token "
            "proves NOTHING. Reporting this as verified would be a false green"
        )
    return True, (
        f"the peer's listen accepted the new bearer (HTTP {good.status}) and "
        f"rejected a bogus one (HTTP {control.status}) — the gate is live and "
        "the rotation is real"
    )


def _refuse(peer: str, detail: str) -> RotateResult:
    return RotateResult(peer=peer, action="refused", exit_code=2, detail=detail)


def rotate_peer_tokens(
    peer: str,
    cfg: Config,
    *,
    master_name: str = "",
    now: datetime | None = None,
    timeout: int = 30,
    restart_timeout: int = 120,
    port: int = DEFAULT_LISTEN_PORT,
    runner=subprocess.run,
    tokens_dir: Path | None = None,
    mint=mint_bearer,
) -> RotateResult:
    """Mint, distribute, restart, verify — the full rotation for ONE peer.

    The contract, in order (ADR-0021 §Tokens):

    1. **Refuse on an unknown peer.** Both the config state and the token
       read must be OBSERVED. A peer we cannot read is a peer we do not
       rotate — rotating blind is how both sides end up holding different
       secrets with nobody watching.
    2. **Mint** a new bearer (:func:`._token_state.mint_bearer`).
    3. **Seed the peer** — every candidate listen path (this node's
       hostname-keyed one AND the stable canonical one), same value.
    4. **Store the master's copy**, retaining the old one as
       ``<file>.pre-rotate-<stamp>``.
    5. **Restart the peer's listen** — the file is inert until then.
    6. **Verify** with a falsifiable authenticated probe.
    7. **Only then discard** the pre-rotate copy.

    Every failure leg names WHICH SIDE HOLDS WHAT and keeps the backup.
    The two sides are never left silently split: if this function cannot
    prove both ends agree, it says so and exits non-zero.
    """
    master = master_name or cfg.canonical_host()
    stamp_dt = now if now is not None else datetime.now(timezone.utc)
    stamp = stamp_dt.strftime("%Y%m%dT%H%M%SZ")
    tdir = tokens_dir if tokens_dir is not None else default_peer_tokens_dir()

    # (1) Refuse on any unknown state — config first, then tokens.
    cfg_state = check_config_peer(
        peer, cfg, master_name=master, timeout=timeout, runner=runner
    )
    if cfg_state.verdict is ConfigVerdict.UNDETERMINED:
        return _refuse(
            peer,
            f"refusing to rotate '{peer}': its config state is UNKNOWN "
            f"({cfg_state.detail}). A peer we cannot read is a peer we do not "
            f"rotate. Fix reachability first:  sac host probe {peer}",
        )
    before = read_peer_tokens(peer, cfg.peers, timeout=timeout, runner=runner)
    if not before.ok:
        return _refuse(
            peer,
            f"refusing to rotate '{peer}': its token state is UNKNOWN "
            f"({before.detail})",
        )

    old_master_copy = read_master_copy(peer, tdir)
    old_sha = sha12(old_master_copy)
    new_token = mint()
    new_sha = sha12(new_token)
    rel_paths = peer_listen_token_rel_paths(peer, before.hostname)

    # (3) Seed every candidate listen-token path on the peer.
    ok, detail = write_remote_token(
        peer,
        cfg.peers,
        new_token,
        rel_paths,
        backup_stamp=stamp,
        timeout=timeout,
        runner=runner,
    )
    if not ok:
        return RotateResult(
            peer=peer,
            action="failed",
            exit_code=2,
            detail=(
                f"could not seed the peer's listen token ({detail}). NOTHING "
                f"changed on the master: it still holds "
                f"sha256:{old_sha or '<none>'} for '{peer}'. The peer may hold "
                f"a PARTIAL write across {', '.join(rel_paths)} — its RUNNING "
                "listen is unaffected (it read its token at boot), but re-run "
                "this rotation before that listen restarts"
            ),
            new_sha12=new_sha,
            old_sha12=old_sha,
        )

    # (4) Store the master's copy, retaining the old one until verified.
    src = tdir / f"{peer}.token"
    backup_path = tdir / f"{peer}.token.pre-rotate-{stamp}"
    had_backup = False
    if src.is_file():
        try:
            shutil.copy2(src, backup_path)  # copy2 preserves the 0600 mode
            had_backup = True
        except OSError as exc:
            return RotateResult(
                peer=peer,
                action="failed",
                exit_code=2,
                detail=(
                    f"could not retain the master's pre-rotate copy ({exc}) — "
                    "refusing to overwrite the only copy of the old bearer. The "
                    f"peer's token FILES now hold sha256:{new_sha}; its running "
                    "listen still serves the old one, and the master still "
                    f"holds sha256:{old_sha}"
                ),
                new_sha12=new_sha,
                old_sha12=old_sha,
            )
    try:
        write_peer_token(peer_host=peer, token=new_token, tokens_dir=tdir)
    except (OSError, ValueError) as exc:
        return RotateResult(
            peer=peer,
            action="failed",
            exit_code=2,
            detail=(
                f"could not store the master's copy ({exc}). SPLIT: the peer's "
                f"token files hold sha256:{new_sha}, the master still holds "
                f"sha256:{old_sha or '<none>'}. The peer's RUNNING listen is "
                "unaffected until it restarts — re-run the rotation, or the "
                "peer desyncs at its next restart"
            ),
            new_sha12=new_sha,
            old_sha12=old_sha,
            backup=str(backup_path) if had_backup else "",
        )

    def _split(reason: str, *, restarted: bool) -> RotateResult:
        """A failure AFTER both sides were written — the loud case."""
        where = str(backup_path) if had_backup else "<the master had no prior copy>"
        return RotateResult(
            peer=peer,
            action="failed",
            exit_code=2,
            detail=(
                f"{reason}\n"
                f"    STATE: the master and the peer's token FILES both hold "
                f"sha256:{new_sha}. Whether the peer's LISTEN serves it is "
                f"UNVERIFIED — until it does, {master} -> {peer} a2a is DOWN.\n"
                f"    The old bearer (sha256:{old_sha or '<none>'}) is RETAINED "
                f"at {where}.\n"
                f"    Recover:  ssh {peer} -- sac listen restart   then re-check:"
                f"  sac host push-config --check {peer}"
            ),
            new_sha12=new_sha,
            old_sha12=old_sha,
            backup=str(backup_path) if had_backup else "",
            restarted=restarted,
        )

    # (5) Restart — the seeded file is inert until the process re-reads it.
    ok, detail = restart_peer_listen(
        peer, cfg.peers, timeout=restart_timeout, runner=runner
    )
    if not ok:
        return _split(f"the peer's listen did not restart ({detail}).", restarted=False)

    # (6) Verify, falsifiably.
    verified, detail = _verify_rotation(
        peer, cfg, new_token=new_token, port=port, timeout=timeout, runner=runner
    )
    if not verified:
        return _split(detail, restarted=True)

    # (7) Only now is the old bearer safe to discard.
    if had_backup:
        try:
            backup_path.unlink()
        except OSError:  # stx-allow: fallback (reason: a retained backup is harmless — failing a VERIFIED rotation over an unlink would be worse)
            pass
    return RotateResult(
        peer=peer,
        action="rotated",
        exit_code=0,
        detail=(
            f"minted a new bearer for '{peer}' (sha256:{new_sha}), seeded "
            f"{', '.join(rel_paths)}, stored the master's copy, restarted its "
            f"listen and VERIFIED: {detail}. The old bearer "
            f"(sha256:{old_sha or '<none>'}) is discarded"
        ),
        new_sha12=new_sha,
        old_sha12=old_sha,
        restarted=True,
        verified=True,
    )
