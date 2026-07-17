"""Verdicts + orchestration for ``sac host push-config`` (master → peer).

ADR-0021: the master's config.yaml is the fleet's ONLY hand-edited
topology file; every peer runs a GENERATED minimal client config
(:mod:`._peer_config`) pushed one-way from the master. This module
classifies what a peer holds against a fresh render and — in push mode
— acts, behind the same three-state honesty rules as ``sac host sync``:

* **UNDETERMINED never mutates** and is never reported as clean.
* **A hand-edited file is never overwritten without ``--adopt``.** The
  refusal prints the unified diff and names the next command; the adopt
  path backs the file up ON THE PEER before writing.
* **No quiet success.** CURRENT still says what it verified, and a push
  re-reads the peer and refuses to claim a success it cannot
  substantiate byte-for-byte.

The remote read/write transport lives in :mod:`._push_config_io`.
"""

from __future__ import annotations

import difflib
import enum
import hashlib
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone

from .._state.host_config import Config
from ._peer_config import (
    embedded_master_sha,
    is_generated,
    render_peer_config,
    strip_generated_stamp,
)
from ._push_config_io import RemoteConfigRead, read_peer_config, write_peer_config

__all__ = [
    "ConfigVerdict",
    "PushConfigResult",
    "check_config_peer",
    "classify_remote_config",
    "master_config_sha",
    "push_config_peer",
    "unified_config_diff",
]


class ConfigVerdict(enum.Enum):
    """What is on the peer, relative to the master's rendered truth."""

    #: Remote content matches the render (push timestamp aside).
    CURRENT = "current"
    #: Carries our header but differs — safe to overwrite on push.
    STALE_GENERATED = "stale-generated"
    #: Exists WITHOUT our header. Never overwritten without --adopt.
    HAND_EDITED = "hand-edited"
    #: No config.yaml on the peer. Push mode creates it.
    ABSENT = "absent"
    #: ssh failed / unreadable. Never mutated, never reported clean.
    UNDETERMINED = "undetermined"


# --check exit codes: worst-of across peers, mirroring ._sync.
_EXIT_CHECK = {
    ConfigVerdict.CURRENT: 0,
    ConfigVerdict.STALE_GENERATED: 1,
    ConfigVerdict.HAND_EDITED: 1,
    ConfigVerdict.ABSENT: 1,
    ConfigVerdict.UNDETERMINED: 2,
}


@dataclass(frozen=True)
class PushConfigResult:
    """Everything that happened to one peer. All of it gets printed.

    ``verdict`` is what was on the peer BEFORE any action; ``action`` is
    what push-config then did about it (``none`` / ``pushed`` /
    ``created`` / ``adopted`` / ``refused`` / ``failed``).
    """

    peer: str
    verdict: ConfigVerdict
    action: str
    exit_code: int
    detail: str = ""
    diff: str = ""
    sha12: str = ""  # master-config sha stamped into the render
    backup: str = ""  # peer-side backup path (adopt mode only)

    @property
    def ok(self) -> bool:
        return self.exit_code == 0

    def to_dict(self) -> dict:
        """JSON projection for ``--json`` (cron consumers)."""
        return {
            "peer": self.peer,
            "verdict": self.verdict.value,
            "action": self.action,
            "exit_code": self.exit_code,
            "detail": self.detail,
            "diff": self.diff,
            "sha12": self.sha12,
            "backup": self.backup,
        }


def master_config_sha(cfg: Config) -> str:
    """sha256 hexdigest of the master's config.yaml bytes (``""`` if none)."""
    p = cfg.source_path
    if p is None or not p.is_file():
        return ""
    return hashlib.sha256(p.read_bytes()).hexdigest()


def _yaml_payload(text: str) -> str:
    """The non-comment lines — what the peer's readers actually consume."""
    return "".join(
        line for line in text.splitlines(keepends=True) if not line.startswith("#")
    )


def classify_remote_config(
    remote: RemoteConfigRead, rendered: str
) -> tuple[ConfigVerdict, str]:
    """Classify the peer's config against the rendered truth. Pure.

    The ``# generated:`` push timestamp is the ONE ignored difference:
    it records when a push landed, so it always differs, and comparing
    it would make CURRENT unreachable. The master-config sha embedded on
    that same line IS compared — a peer stamped with an older master
    sha is stale even when the derived keys happen to match (that is
    header-only staleness; a push refreshes the header).
    """
    if not remote.ok:
        return ConfigVerdict.UNDETERMINED, remote.detail
    if remote.absent:
        return ConfigVerdict.ABSENT, "no config.yaml on the peer"
    if remote.text == rendered:
        return ConfigVerdict.CURRENT, "byte-identical with the rendered config"
    if not is_generated(remote.text):
        return (
            ConfigVerdict.HAND_EDITED,
            "the peer's config.yaml does not carry the generated header",
        )
    if strip_generated_stamp(remote.text) == strip_generated_stamp(
        rendered
    ) and embedded_master_sha(remote.text) == embedded_master_sha(rendered):
        return (
            ConfigVerdict.CURRENT,
            "content identical (only the push timestamp differs)",
        )
    if _yaml_payload(remote.text) == _yaml_payload(rendered):
        return (
            ConfigVerdict.STALE_GENERATED,
            "header-only drift: provenance/comment lines changed "
            f"(peer stamped sha256:{embedded_master_sha(remote.text) or '?'}, "
            f"master is sha256:{embedded_master_sha(rendered) or '?'}); "
            "derived keys unchanged",
        )
    return (
        ConfigVerdict.STALE_GENERATED,
        "keys differ from the master's rendering",
    )


def unified_config_diff(remote_text: str, rendered: str, peer: str) -> str:
    """Unified diff, remote → rendered (what a push would change)."""
    return "".join(
        difflib.unified_diff(
            remote_text.splitlines(keepends=True),
            rendered.splitlines(keepends=True),
            fromfile=f"{peer}:~/.scitex/agent-container/config.yaml",
            tofile="rendered (master truth)",
        )
    )


def check_config_peer(
    peer: str,
    cfg: Config,
    *,
    master_name: str = "",
    now: datetime | None = None,
    timeout: int = 30,
    runner=subprocess.run,
) -> PushConfigResult:
    """Read-only verdict for one peer. Mutates nothing.

    ``master_name`` defaults to the local canonical host — push-config
    runs ON the master, so the centre's own name is the right identity
    for both the header and the emitted ``peers:`` route back.
    """
    master = master_name or cfg.canonical_host()
    sha = master_config_sha(cfg)
    rendered = render_peer_config(
        peer, cfg, master_name=master, now=now, master_sha=sha
    )
    remote = read_peer_config(peer, cfg.peers, timeout=timeout, runner=runner)
    verdict, detail = classify_remote_config(remote, rendered)
    diff = ""
    if remote.ok and not remote.absent and verdict is not ConfigVerdict.CURRENT:
        diff = unified_config_diff(remote.text, rendered, peer)
    return PushConfigResult(
        peer=peer,
        verdict=verdict,
        action="none",
        exit_code=_EXIT_CHECK[verdict],
        detail=detail,
        diff=diff,
        sha12=sha[:12],
    )


def push_config_peer(
    peer: str,
    cfg: Config,
    *,
    adopt: bool = False,
    master_name: str = "",
    now: datetime | None = None,
    timeout: int = 30,
    runner=subprocess.run,
) -> PushConfigResult:
    """Reconcile one peer's client config to the rendered truth, or refuse.

    Probe → classify → act. The only verdicts that authorise a write are
    ABSENT (create) and STALE_GENERATED (overwrite our own output);
    HAND_EDITED additionally requires ``adopt=True`` and is backed up on
    the peer first. After any write the peer is re-read and the result
    is only reported as a success when the read-back bytes equal what we
    intended — a push that cannot substantiate itself reports FAILED.
    """
    master = master_name or cfg.canonical_host()
    sha = master_config_sha(cfg)
    stamp_dt = now if now is not None else datetime.now(timezone.utc)
    rendered = render_peer_config(
        peer, cfg, master_name=master, now=stamp_dt, master_sha=sha
    )
    remote = read_peer_config(peer, cfg.peers, timeout=timeout, runner=runner)
    verdict, detail = classify_remote_config(remote, rendered)
    sha12 = sha[:12]

    if verdict is ConfigVerdict.UNDETERMINED:
        return PushConfigResult(
            peer=peer,
            verdict=verdict,
            action="refused",
            exit_code=2,
            detail=(
                f"refusing to push to '{peer}': its config state is UNKNOWN "
                f"({detail}). An unknown peer is not a writable peer — sac "
                "never mutates on an unobserved negative. Fix reachability "
                f"first:  sac host probe {peer}"
            ),
            sha12=sha12,
        )
    if adopt and verdict is not ConfigVerdict.HAND_EDITED:
        return PushConfigResult(
            peer=peer,
            verdict=verdict,
            action="refused",
            exit_code=1,
            detail=(
                f"--adopt is only for a HAND-EDITED config; '{peer}' is "
                f"{verdict.value}. Run without --adopt."
            ),
            sha12=sha12,
        )
    if verdict is ConfigVerdict.CURRENT:
        return PushConfigResult(
            peer=peer,
            verdict=verdict,
            action="none",
            exit_code=0,
            detail=f"{detail} (master-config sha256:{sha12})",
            sha12=sha12,
        )
    if verdict is ConfigVerdict.HAND_EDITED and not adopt:
        return PushConfigResult(
            peer=peer,
            verdict=verdict,
            action="refused",
            exit_code=1,
            detail=(
                f"REFUSING to overwrite '{peer}': its config.yaml is "
                "hand-edited (no generated header). sac never silently "
                "replaces a file a human wrote. Review the diff below; to "
                "replace it (backing it up on the peer first) run:\n"
                f"    sac host push-config {peer} --adopt"
            ),
            diff=unified_config_diff(remote.text, rendered, peer),
            sha12=sha12,
        )

    backup_stamp = stamp_dt.strftime("%Y%m%dT%H%M%SZ") if adopt else ""
    ok, write_detail = write_peer_config(
        peer,
        cfg.peers,
        rendered,
        backup_stamp=backup_stamp,
        timeout=timeout,
        runner=runner,
    )
    if not ok:
        return PushConfigResult(
            peer=peer,
            verdict=verdict,
            action="failed",
            exit_code=2,
            detail=f"write failed: {write_detail}",
            sha12=sha12,
        )
    after = read_peer_config(peer, cfg.peers, timeout=timeout, runner=runner)
    if not (after.ok and not after.absent and after.text == rendered):
        cause = after.detail or (
            "file still absent" if after.absent else "read-back bytes differ"
        )
        return PushConfigResult(
            peer=peer,
            verdict=verdict,
            action="failed",
            exit_code=2,
            detail=(
                f"write returned success but verification failed ({cause}) — "
                "sac does not report a success it cannot substantiate"
            ),
            sha12=sha12,
        )
    action = (
        "adopted"
        if adopt
        else ("created" if verdict is ConfigVerdict.ABSENT else "pushed")
    )
    backup = (
        f"~/.scitex/agent-container/config.yaml.pre-adopt-{backup_stamp}"
        if adopt
        else ""
    )
    return PushConfigResult(
        peer=peer,
        verdict=verdict,
        action=action,
        exit_code=0,
        detail=(
            f"{detail} → {action}; verified byte-identical on read-back "
            f"(master-config sha256:{sha12})"
        ),
        sha12=sha12,
        backup=backup,
    )
