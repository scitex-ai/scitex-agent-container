#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Verified spec handoff — put the lead's spec dir on a peer, and PROVE it.

Why this replaced ``rsync``
---------------------------
Dispatch used to deliver the spec with ``rsync -acv --delete`` over ssh and
treat ``rc == 0`` as delivery. Measured on ``scitex-nas-03`` (UGREEN DXP480T,
2026-08-15) that inference is false. Its ``/usr/bin/rsync`` is a vendor-patched
**setuid-root** 3.4.1 that routes even plain ``--server`` ssh transfers through
the rsync *daemon* module code, and the module table in ``/etc/rsyncd.conf``
maps module ``home`` to ``/home/ywatanabe``. The receiver strips the leading
``/home`` from the destination and then resolves the remainder INSIDE that
module root, so every path lands one level deep::

    dest /home/ywatanabe/__p__/   -> written to /home/ywatanabe/ywatanabe/__p__/
    dest ~/__p__/                 -> written to /home/ywatanabe/ywatanabe/__p__/   (same)
    dest ~/                       -> "created directory ywatanabe"

All three exited **0**, printed a normal file list, and reported bytes sent.
Nothing arrived at the requested path. A destination whose parent does not
exist under that doubled prefix fails the other way — ``mkdir ... (in home)
failed: No such file or directory (2)``, rc 11 — even when the requested
directory plainly exists. And ``--dry-run`` does not predict either outcome:
the same destination that fails a real write with rc 11 plans cleanly with
rc 0, because the dry run never calls ``mkdir``.

So on that peer the old code had two ways to be wrong and no way to notice:
a loud rc-12/rc-11 that reads like a permissions problem, or a silent rc-0
that puts the spec somewhere nobody reads — after which the remote
``sac agents start`` boots the agent from the STALE spec still sitting at the
real path, and the dispatch reports success. Repairing the destination string
cannot fix this: there is no path that both names the true directory and
survives the doubling, and a peer that mis-delivers silently must not be
trusted on the strength of an exit code anyway.

The replacement therefore does not ask a transport to be trustworthy. It
ships a tar stream through the ordinary login shell — the same shell
:func:`build_ssh_argv` already uses for the remote ``sac agents start``, which
works fine on every peer including this one — and then **re-reads the peer's
own digests and compares them to the lead's**. Delivery is a measurement, not
an exit code.

Three deliberate differences from the old rsync behaviour
--------------------------------------------------------
1. **Nothing is deleted.** ``rsync --delete`` would have removed every peer-side
   file the lead does not have. On ``scitex-nas-03`` that set includes
   ``start-telegram-sidecar.sh`` and the sidecar logs, which exist only there.
   Deleting operator-placed files as a side effect of starting an agent is the
   unrequested deletion #1048 just promoted to a first-class gate, so peer-only
   files are REPORTED and kept.
2. **The destination is resolved by the peer's shell** (``"$HOME/.scitex/..."``)
   rather than sent as a home-relative path. A bare relative destination
   resolves against the remote CWD, and on ``scitex-nas-03`` a non-interactive
   ``ssh … pwd`` is ``/home/ywatanabe/proj/scitex-hub``, not ``$HOME`` — which,
   with the old ``--delete``, aimed a mirroring delete at an unrelated repo.
   A registry-pinned ABSOLUTE root still wins; see :mod:`._dispatch_paths` for
   the Spartan incident that makes the registry authoritative.
3. **Drift is decided by content digests**, not by parsing ``--itemize-changes``.

Symlinks are shipped by tar but not verified: both manifests count regular
files only (``find -type f`` on the peer, ``is_file() and not is_symlink()``
here), so the two sides always describe the same set.
"""

from __future__ import annotations

import hashlib
import shlex
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

    from ..._state.host_config import PeerSpec

    #: Runs a POSIX-``sh`` script somewhere and returns its raw result. The
    #: one seam between this module and the network: :func:`ssh_runner`
    #: supplies the real one, and a test supplies a peer that behaves
    #: differently (one that re-roots writes, say) WITHOUT patching anything
    #: inside this module.
    PeerShell = Callable[[str, "bytes | None"], "subprocess.CompletedProcess[bytes]"]

__all__ = [
    "EXCLUDED_NAMES",
    "HandoffPlan",
    "local_manifest",
    "manifest_script",
    "parse_manifest",
    "plan_handoff",
    "push_spec_dir",
    "read_remote_manifest",
    "ssh_runner",
]

#: Directory names never shipped and never compared — peer-side runtime
#: state and build caches. Matched at ANY depth, which is what rsync's
#: ``--exclude=runtime/`` did too.
EXCLUDED_NAMES: tuple[str, ...] = (
    "runtime",
    "__pycache__",
    ".pytest_cache",
    "_sphinx_html",
)


@dataclass(frozen=True)
class HandoffPlan:
    """What delivering the lead's spec dir to a peer would change.

    ``new`` are relpaths the peer does not have, ``changed`` are relpaths it
    has with different content, and ``extra`` are the peer's own files the
    lead does not have (kept, never deleted).
    """

    new: tuple[str, ...]
    changed: tuple[str, ...]
    extra: tuple[str, ...]

    @property
    def first_launch(self) -> bool:
        """True when the peer has none of the lead's files yet."""
        return bool(self.new) and not self.changed

    @property
    def drift(self) -> bool:
        """True when the peer holds a DIFFERENT version of a shared file.

        Only ``changed`` counts. ``extra`` used to count as drift because
        rsync reported the deletions it planned; nothing is deleted now, so
        a peer-only file is news, not a conflict.
        """
        return bool(self.changed)

    def summary(self) -> str:
        """One-line human summary used by ``--dry-run`` and error text."""
        return (
            f"{len(self.new)} new, {len(self.changed)} changed, "
            f"{len(self.extra)} peer-only"
        )


def _digest(path: Path) -> str:
    """md5 of a file's bytes — an integrity check, not a security claim.

    Chosen because ``md5sum`` is the one checksum tool present on every peer
    in this fleet, including the NAS busybox-ish userlands; the comparison is
    only ever lead-vs-peer for files the lead just wrote.
    """
    digest = hashlib.md5(usedforsecurity=False)
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 16), b""):
            digest.update(chunk)
    return digest.hexdigest()


def local_manifest(src_dir: Path) -> dict[str, str]:
    """``{relpath: md5}`` for every regular file the lead would ship."""
    manifest: dict[str, str] = {}
    for path in sorted(src_dir.rglob("*")):
        if path.is_symlink() or not path.is_file():
            continue
        rel = path.relative_to(src_dir)
        if any(part in EXCLUDED_NAMES for part in rel.parts):
            continue
        manifest[rel.as_posix()] = _digest(path)
    return manifest


def manifest_script(remote_dir: str) -> str:
    """POSIX ``sh`` printing ``<md5>  ./<relpath>`` for the peer's spec dir.

    An ABSENT directory is not an error — it is a first launch, so the script
    exits 0 with no output. A missing ``md5sum`` IS an error: without it we
    cannot verify delivery, and proceeding unverified is the exact failure
    this module exists to remove.
    """
    prunes = " ".join(f"-name {shlex.quote(n)} -prune -o" for n in EXCLUDED_NAMES)
    return (
        f'd="{remote_dir}"; [ -d "$d" ] || exit 0; cd "$d" || exit 1; '
        "command -v md5sum >/dev/null 2>&1 || "
        '{ echo "sac: md5sum not found on peer" >&2; exit 3; }; '
        f"find . {prunes} -type f -exec md5sum {{}} +"
    )


def parse_manifest(stdout: str) -> dict[str, str]:
    """Parse ``md5sum`` output into ``{relpath: md5}``.

    ``md5sum`` prints ``<hash>  <path>`` (two spaces, or one plus a binary
    marker). Paths arrive as ``./sub/file`` because the script ``cd``s into
    the spec dir first; the ``./`` is stripped so both sides use the same
    keys as :func:`local_manifest`.
    """
    manifest: dict[str, str] = {}
    for line in stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        digest, _, rest = line.partition(" ")
        rel = rest.strip().lstrip("*").strip()
        if not digest or not rel:
            continue
        if rel.startswith("./"):
            rel = rel[2:]
        manifest[rel] = digest
    return manifest


def plan_handoff(
    local: Mapping[str, str], remote: Mapping[str, str]
) -> HandoffPlan:
    """Diff two manifests into a :class:`HandoffPlan`."""
    new = tuple(sorted(rel for rel in local if rel not in remote))
    changed = tuple(
        sorted(rel for rel, dig in local.items() if rel in remote and remote[rel] != dig)
    )
    extra = tuple(sorted(rel for rel in remote if rel not in local))
    return HandoffPlan(new=new, changed=changed, extra=extra)


def ssh_runner(peer: str, peers: Mapping[str, PeerSpec]) -> PeerShell:
    """A :data:`PeerShell` that runs scripts on ``peer`` over ssh.

    The script is collapsed into ONE pre-quoted argv element because ssh
    word-joins everything after the host and hands the result to the remote
    shell — the same reason :func:`build_ssh_argv` pre-quotes its
    ``env_preamble`` wrapper. That remote shell is also what expands the
    ``$HOME`` in a registry-unpinned destination.
    """
    from ..._state.host_config import build_ssh_argv

    def run(
        script: str, stdin: bytes | None = None
    ) -> subprocess.CompletedProcess[bytes]:
        argv = build_ssh_argv(peer, [f"sh -c {shlex.quote(script)}"], dict(peers))
        return subprocess.run(argv, input=stdin, capture_output=True, check=False)

    return run


def read_remote_manifest(remote_dir: str, shell: PeerShell) -> dict[str, str]:
    """``{relpath: md5}`` as the PEER reports it. Empty when absent."""
    result = shell(manifest_script(remote_dir))
    if result.returncode != 0:
        raise RuntimeError(
            f"Could not read the spec manifest at {remote_dir} "
            f"(rc={result.returncode}).\n"
            f"stderr:\n{result.stderr.decode('utf-8', 'replace')}"
        )
    return parse_manifest(result.stdout.decode("utf-8", "replace"))


def push_spec_dir(
    src_dir: Path,
    remote_dir: str,
    shell: PeerShell,
    *,
    peer: str = "peer",
) -> dict[str, str]:
    """Ship ``src_dir`` to ``peer:remote_dir`` and PROVE every file landed.

    Returns the peer's post-transfer manifest. Raises ``RuntimeError`` when
    the local ``tar`` fails, when the remote extraction fails, or — the point
    of the whole module — when the peer's own digests do not match the lead's
    after a transfer that claimed success.
    """
    excludes = [f"--exclude={name}" for name in EXCLUDED_NAMES]
    tar = subprocess.run(
        ["tar", "-C", str(src_dir), "-czf", "-", *excludes, "."],
        capture_output=True,
        check=False,
    )
    if tar.returncode != 0:
        raise RuntimeError(
            f"Could not pack the spec dir {src_dir!s} (tar rc={tar.returncode}):\n"
            f"{tar.stderr.decode('utf-8', 'replace')}"
        )

    extract = shell(
        f'd="{remote_dir}"; mkdir -p "$d" && tar -C "$d" -xzf -',
        tar.stdout,
    )
    if extract.returncode != 0:
        raise RuntimeError(
            f"Spec handoff to {peer!r} failed while extracting into "
            f"{remote_dir} (rc={extract.returncode}):\n"
            f"{extract.stderr.decode('utf-8', 'replace')}"
        )

    expected = local_manifest(src_dir)
    landed = read_remote_manifest(remote_dir, shell)
    missing = sorted(rel for rel in expected if rel not in landed)
    wrong = sorted(
        rel for rel, dig in expected.items() if rel in landed and landed[rel] != dig
    )
    if missing or wrong:
        raise RuntimeError(
            f"Spec handoff to {peer!r} reported success but the peer does NOT "
            f"have what was sent — the agent would boot from a stale spec.\n"
            f"  target:  {remote_dir}\n"
            f"  missing: {', '.join(missing) or 'none'}\n"
            f"  differing: {', '.join(wrong) or 'none'}\n"
            "The transfer exited 0, so this is the peer writing somewhere "
            "other than the requested path (a vendor-patched rsync/tar that "
            "re-roots paths does exactly this). Verify with:\n"
            f"  ssh {peer} 'ls -la {remote_dir}'"
        )
    return landed


# EOF
