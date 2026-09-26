"""Fleet transaction for content-addressed SIF distribution.

The source artifact is identified by its complete SHA-256.  Every peer stages
to one explicit temporary path, verifies there, atomically renames to the
content-addressed final path, and verifies again.  Live links are changed only
after every peer has passed both checks.  No pruning belongs in this primitive.
"""

from __future__ import annotations

import base64
import hashlib
import json
import shlex
import subprocess
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path, PurePosixPath
from typing import BinaryIO, Protocol

from .._state.host_config import PeerSpec, build_ssh_argv

LAYERS = ("base", "scitex", "proxy")
DEFAULT_TARGET_ROOT = "~/.scitex/agent-container/containers"


@dataclass(frozen=True)
class Artifact:
    source: str
    layer: str
    sha256: str
    size: int
    name: str

    @property
    def layer_name(self) -> str:
        return f"sac-{self.layer}"


@dataclass(frozen=True)
class LinkState:
    inner: str | None
    top: str | None


@dataclass
class HostReceipt:
    host: str
    remote_path: str
    sha256: str
    size: int
    status: str = "pending"
    verified: bool = False
    phases: list[str] = field(default_factory=list)
    current_before: dict[str, str | None] | None = None
    current_after: dict[str, str | None] | None = None
    error: str | None = None


@dataclass
class DistributionResult:
    artifact: Artifact
    target_root: str
    dry_run: bool
    success: bool
    hosts: list[HostReceipt]

    def as_dict(self) -> dict:
        return {
            "schema": "sac.image.distribution-receipt/v1",
            "success": self.success,
            "dry_run": self.dry_run,
            "artifact": asdict(self.artifact),
            "target_root": self.target_root,
            "hosts": [asdict(row) for row in self.hosts],
        }


class ImageTransport(Protocol):
    def inspect(self, host: str, artifact: Artifact, root: str) -> LinkState: ...

    def stage(self, host: str, artifact: Artifact, root: str, token: str) -> None: ...

    def commit(self, host: str, artifact: Artifact, root: str, token: str) -> None: ...

    def verify(self, host: str, artifact: Artifact, root: str) -> None: ...

    def activate(
        self,
        host: str,
        artifact: Artifact,
        root: str,
        token: str,
        before: LinkState,
    ) -> LinkState: ...

    def verify_active(
        self, host: str, artifact: Artifact, root: str, token: str
    ) -> LinkState: ...

    def restore(
        self,
        host: str,
        artifact: Artifact,
        root: str,
        token: str,
        before: LinkState,
    ) -> None: ...

    def cleanup(self, host: str, artifact: Artifact, root: str, token: str) -> None: ...


def resolve_artifact(source: Path, layer: str) -> Artifact:
    """Resolve a regular SIF and give it a content-addressed remote name."""
    if layer not in LAYERS:
        raise ValueError(f"unknown image layer: {layer}")
    resolved = source.expanduser().resolve(strict=True)
    if not resolved.is_file():
        raise ValueError(f"source is not a regular file: {resolved}")
    if resolved.suffix != ".sif":
        raise ValueError(f"source must be a .sif artifact: {resolved}")
    digest = hashlib.sha256()
    size = 0
    with resolved.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            size += len(chunk)
            digest.update(chunk)
    sha256 = digest.hexdigest()
    name = f"sac-{layer}-sha256-{sha256}.sif"
    return Artifact(str(resolved), layer, sha256, size, name)


def validate_target_root(root: str) -> str:
    """Accept one explicit non-root POSIX directory, never a glob."""
    if not root or "\x00" in root or "\n" in root:
        raise ValueError("target root must be a non-empty single-line path")
    if any(char in root for char in "*?["):
        raise ValueError("target root must not contain glob metacharacters")
    path_text = root[2:] if root.startswith("~/") else root
    path = PurePosixPath(path_text)
    if not (root.startswith("~/") or path.is_absolute()):
        raise ValueError("target root must be absolute or start with '~/'.")
    if path == PurePosixPath("/") or ".." in path.parts:
        raise ValueError("target root must be a non-root path without '..'")
    return root.rstrip("/")


def _remote_path(root: str, artifact: Artifact) -> str:
    return f"{root}/{artifact.layer_name}/{artifact.name}"


def distribute(
    *,
    artifact: Artifact,
    hosts: tuple[str, ...],
    target_root: str,
    transport: ImageTransport | None,
    dry_run: bool = False,
) -> DistributionResult:
    """Run the fail-closed fleet transaction and return per-host receipts."""
    target_root = validate_target_root(target_root)
    if not hosts:
        raise ValueError("at least one explicit target host is required")
    if len(set(hosts)) != len(hosts):
        raise ValueError("target hosts must not contain duplicates")
    rows = [
        HostReceipt(
            host=host,
            remote_path=_remote_path(target_root, artifact),
            sha256=artifact.sha256,
            size=artifact.size,
        )
        for host in hosts
    ]
    result = DistributionResult(artifact, target_root, dry_run, False, rows)
    if dry_run:
        for row in rows:
            row.status = "planned"
            row.phases = ["inspect", "stage", "commit", "verify", "activate"]
        result.success = True
        return result
    if transport is None:
        raise ValueError("a transport is required outside dry-run")

    token = uuid.uuid4().hex
    before: dict[str, LinkState] = {}
    failed = False
    for row in rows:
        try:
            state = transport.inspect(row.host, artifact, target_root)
            before[row.host] = state
            row.current_before = asdict(state)
            row.status = "inspected"
            row.phases.append("inspected")
        except Exception as exc:  # transport failures become receipts
            row.status, row.error, failed = "failed", str(exc), True
    if failed:
        return result

    for row in rows:
        try:
            transport.stage(row.host, artifact, target_root, token)
            row.status = "staged"
            row.phases.append("staged_verified")
        except Exception as exc:
            row.status, row.error, failed = "failed", str(exc), True
    if failed:
        _cleanup_staging(rows, artifact, target_root, token, transport)
        return result

    for row in rows:
        try:
            transport.commit(row.host, artifact, target_root, token)
            row.status = "committed"
            row.phases.append("atomically_committed")
        except Exception as exc:
            row.status, row.error, failed = "failed", str(exc), True
    _cleanup_staging(rows, artifact, target_root, token, transport)
    if failed:
        return result

    for row in rows:
        try:
            transport.verify(row.host, artifact, target_root)
            row.verified = True
            row.phases.append("final_verified")
        except Exception as exc:
            row.status, row.error, failed = "failed", str(exc), True
    if failed:
        return result

    activated: list[HostReceipt] = []
    for row in rows:
        try:
            after = transport.activate(
                row.host, artifact, target_root, token, before[row.host]
            )
            row.current_after = asdict(after)
            row.status = "activated"
            row.phases.append("activated")
            activated.append(row)
        except Exception as exc:
            row.status, row.error, failed = "failed", str(exc), True
            break
    if failed:
        _restore_activated(activated, artifact, target_root, token, before, transport)
        return result

    # Verify the published links on every peer.  A late mismatch is still a
    # fleet failure and gets the same compensating restoration.
    for row in rows:
        try:
            after = transport.verify_active(row.host, artifact, target_root, token)
            row.current_after = asdict(after)
            row.phases.append("active_verified")
        except Exception as exc:
            row.status, row.error, failed = "failed", str(exc), True
    if failed:
        _restore_activated(rows, artifact, target_root, token, before, transport)
        return result
    result.success = True
    return result


def _cleanup_staging(rows, artifact, root, token, transport) -> None:
    for row in rows:
        try:
            transport.cleanup(row.host, artifact, root, token)
        except Exception as exc:
            row.phases.append(f"staging_cleanup_failed: {exc}")


def _restore_activated(rows, artifact, root, token, before, transport) -> None:
    for row in reversed(rows):
        try:
            transport.restore(row.host, artifact, root, token, before[row.host])
            row.status = "restored"
            row.current_after = asdict(before[row.host])
            row.phases.append("restored_after_fleet_failure")
        except Exception as exc:
            row.status = "rollback_failed"
            row.error = f"{row.error + '; ' if row.error else ''}restore failed: {exc}"


_REMOTE_PROGRAM = r"""import hashlib,json,os,sys
from pathlib import Path
op,root_s,layer,name,want_sha,want_size,token,*rest=sys.argv[1:]
root=Path(root_s).expanduser(); want_size=int(want_size)
layer_name='sac-'+layer; layer_dir=root/layer_name
final=layer_dir/name; temp=layer_dir/('.incoming-distribute-'+token+'.sif')
inner=layer_dir/(layer_name+'.sif'); top=root/(layer_name+'.sif')
def state(p):
    if p.is_symlink(): return os.readlink(p)
    if p.exists(): raise RuntimeError('refusing non-symlink live path: '+str(p))
    return None
def check(p):
    if p.is_symlink() or not p.is_file(): raise RuntimeError('artifact is not a regular file: '+str(p))
    h=hashlib.sha256(); n=0
    with p.open('rb') as f:
        while True:
            b=f.read(1024*1024)
            if not b: break
            n+=len(b); h.update(b)
    got=h.hexdigest()
    if n != want_size or got != want_sha: raise RuntimeError('size/SHA mismatch at '+str(p)+': got '+str(n)+'/'+got)
def atom_link(p,target):
    t=p.parent/('.distribute-link-'+token+'-'+p.name)
    if t.exists() or t.is_symlink(): t.unlink()
    t.symlink_to(target); os.replace(t,p)
def restore_one(p,target):
    if target is None:
        if p.is_symlink(): p.unlink()
    else: atom_link(p,target)
if op == 'inspect':
    out={'inner':state(inner),'top':state(top)}
elif op == 'stage':
    layer_dir.mkdir(parents=True,exist_ok=True)
    try:
        with temp.open('xb') as f:
            while True:
                b=sys.stdin.buffer.read(1024*1024)
                if not b: break
                f.write(b)
            f.flush(); os.fsync(f.fileno())
        check(temp); out={'staged':str(temp)}
    except Exception:
        if temp.is_file() and not temp.is_symlink(): temp.unlink()
        raise
elif op == 'commit':
    check(temp)
    if final.exists() or final.is_symlink():
        check(final); temp.unlink()
    else:
        os.replace(temp,final)
        fd=os.open(layer_dir,os.O_RDONLY); os.fsync(fd); os.close(fd)
    check(final); out={'committed':str(final)}
elif op == 'verify':
    check(final); out={'verified':str(final)}
elif op == 'activate':
    expected=json.loads(rest[0]); check(final)
    if state(inner) != expected['inner'] or state(top) != expected['top']:
        raise RuntimeError('live links changed since preflight; refusing activation')
    try:
        atom_link(inner,name); atom_link(top,layer_name+'/'+name)
        check(inner.resolve()); check(top.resolve())
    except Exception:
        restore_one(inner,expected['inner']); restore_one(top,expected['top']); raise
    out={'inner':state(inner),'top':state(top)}
elif op == 'active':
    check(final)
    if state(inner) != name or state(top) != layer_name+'/'+name:
        raise RuntimeError('live link mismatch after activation')
    out={'inner':state(inner),'top':state(top)}
elif op == 'restore':
    old=json.loads(rest[0])
    if state(inner) != name or state(top) != layer_name+'/'+name:
        raise RuntimeError('live links changed after activation; refusing restore')
    restore_one(inner,old['inner']); restore_one(top,old['top'])
    out={'inner':state(inner),'top':state(top)}
elif op == 'cleanup':
    if temp.is_symlink(): raise RuntimeError('refusing symlink staging cleanup: '+str(temp))
    if temp.is_file(): temp.unlink()
    out={'cleaned':str(temp)}
else: raise RuntimeError('unknown operation: '+op)
print(json.dumps(out,sort_keys=True))
"""


class SshImageTransport:
    """OpenSSH transport using an inline Python receiver on configured peers."""

    def __init__(self, peers: dict[str, PeerSpec], *, timeout: int = 3600):
        self.peers = peers
        self.timeout = timeout

    def _call(
        self,
        host: str,
        op: str,
        artifact: Artifact,
        root: str,
        token: str = "none",
        extra: str | None = None,
        stdin: BinaryIO | None = None,
    ) -> dict:
        loader = (
            "import base64;exec(base64.b64decode("
            + repr(base64.b64encode(_REMOTE_PROGRAM.encode()).decode())
            + "))"
        )
        remote = [
            "python3",
            "-c",
            loader,
            op,
            root,
            artifact.layer,
            artifact.name,
            artifact.sha256,
            str(artifact.size),
            token,
        ]
        if extra is not None:
            remote.append(extra)
        quoted = [shlex.quote(part) for part in remote]
        argv = build_ssh_argv(host, quoted, self.peers)
        proc = subprocess.run(
            argv,
            stdin=stdin,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=self.timeout,
        )
        stdout = proc.stdout.decode("utf-8", "replace").strip()
        stderr = proc.stderr.decode("utf-8", "replace").strip()
        if proc.returncode != 0:
            raise RuntimeError(
                f"{op} failed on {host} (ssh rc={proc.returncode}): "
                f"{stderr or stdout or 'remote returned no detail'}"
            )
        try:
            return json.loads(stdout.splitlines()[-1])
        except (IndexError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"{op} on {host} returned no valid receipt") from exc

    def inspect(self, host, artifact, root):
        out = self._call(host, "inspect", artifact, root)
        return LinkState(out["inner"], out["top"])

    def stage(self, host, artifact, root, token):
        with Path(artifact.source).open("rb") as source:
            self._call(host, "stage", artifact, root, token, stdin=source)

    def commit(self, host, artifact, root, token):
        self._call(host, "commit", artifact, root, token)

    def verify(self, host, artifact, root):
        self._call(host, "verify", artifact, root)

    def activate(self, host, artifact, root, token, before):
        expected = json.dumps(asdict(before), separators=(",", ":"))
        out = self._call(host, "activate", artifact, root, token, expected)
        return LinkState(out["inner"], out["top"])

    def verify_active(self, host, artifact, root, token):
        out = self._call(host, "active", artifact, root, token)
        return LinkState(out["inner"], out["top"])

    def restore(self, host, artifact, root, token, before):
        state = json.dumps(asdict(before), separators=(",", ":"))
        self._call(host, "restore", artifact, root, token, state)

    def cleanup(self, host, artifact, root, token):
        self._call(host, "cleanup", artifact, root, token)


__all__ = [
    "Artifact",
    "DEFAULT_TARGET_ROOT",
    "DistributionResult",
    "HostReceipt",
    "ImageTransport",
    "LAYERS",
    "LinkState",
    "SshImageTransport",
    "distribute",
    "resolve_artifact",
    "validate_target_root",
]
