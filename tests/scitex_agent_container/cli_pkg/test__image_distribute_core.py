"""Unit and fake-transport integration tests for image distribution."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from scitex_agent_container.cli_pkg._image_distribute_core import (
    _REMOTE_PROGRAM,
    LinkState,
    distribute,
    resolve_artifact,
    validate_target_root,
)


class FileTransport:
    """A filesystem-backed transport: separate directories model hosts."""

    def __init__(self, base: Path, hosts: tuple[str, ...]):
        self.roots = {host: base / host for host in hosts}
        self.calls: list[tuple[str, str]] = []
        self.fail_stage: set[str] = set()
        self.fail_activate: set[str] = set()

    def paths(self, host, artifact, token="none"):
        root = self.roots[host]
        layer = root / artifact.layer_name
        return (
            layer / artifact.name,
            layer / f".incoming-distribute-{token}.sif",
            layer / f"{artifact.layer_name}.sif",
            root / f"{artifact.layer_name}.sif",
        )

    @staticmethod
    def _state(path: Path):
        if path.is_symlink():
            return os.readlink(path)
        if path.exists():
            raise RuntimeError("live path is not a symlink")
        return None

    @staticmethod
    def _check(path, artifact):
        data = path.read_bytes()
        if (
            len(data) != artifact.size
            or hashlib.sha256(data).hexdigest() != artifact.sha256
        ):
            raise RuntimeError("size/SHA mismatch")

    @staticmethod
    def _link(path, target):
        temp = path.with_name("." + path.name + ".tmp")
        temp.unlink(missing_ok=True)
        temp.symlink_to(target)
        os.replace(temp, path)

    def inspect(self, host, artifact, root):
        self.calls.append((host, "inspect"))
        _, _, inner, top = self.paths(host, artifact)
        return LinkState(self._state(inner), self._state(top))

    def stage(self, host, artifact, root, token):
        self.calls.append((host, "stage"))
        _, temp, _, _ = self.paths(host, artifact, token)
        temp.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(artifact.source, temp)
        if host in self.fail_stage:
            temp.write_bytes(b"corrupt")
        self._check(temp, artifact)

    def commit(self, host, artifact, root, token):
        self.calls.append((host, "commit"))
        final, temp, _, _ = self.paths(host, artifact, token)
        if final.exists():
            self._check(final, artifact)
            temp.unlink()
        else:
            os.replace(temp, final)
        self._check(final, artifact)

    def verify(self, host, artifact, root):
        self.calls.append((host, "verify"))
        self._check(self.paths(host, artifact)[0], artifact)

    def activate(self, host, artifact, root, token, before):
        self.calls.append((host, "activate"))
        if host in self.fail_activate:
            raise RuntimeError("activation refused")
        _, _, inner, top = self.paths(host, artifact)
        if LinkState(self._state(inner), self._state(top)) != before:
            raise RuntimeError("live links changed")
        self._link(inner, artifact.name)
        self._link(top, f"{artifact.layer_name}/{artifact.name}")
        return LinkState(self._state(inner), self._state(top))

    def verify_active(self, host, artifact, root, token):
        self.calls.append((host, "active"))
        _, _, inner, top = self.paths(host, artifact)
        got = LinkState(self._state(inner), self._state(top))
        want = LinkState(artifact.name, f"{artifact.layer_name}/{artifact.name}")
        if got != want:
            raise RuntimeError("active mismatch")
        return got

    def restore(self, host, artifact, root, token, before):
        self.calls.append((host, "restore"))
        _, _, inner, top = self.paths(host, artifact)
        for path, target in ((inner, before.inner), (top, before.top)):
            if target is None:
                path.unlink(missing_ok=True)
            else:
                self._link(path, target)

    def cleanup(self, host, artifact, root, token):
        self.calls.append((host, "cleanup"))
        self.paths(host, artifact, token)[1].unlink(missing_ok=True)


def _artifact(tmp_path: Path):
    source = tmp_path / "built.sif"
    source.write_bytes(b"immutable-sif-bytes")
    return resolve_artifact(source, "base")


def _seed_old_links(transport, hosts, artifact):
    states = {}
    for host in hosts:
        final, _, inner, top = transport.paths(host, artifact)
        final.parent.mkdir(parents=True)
        old = final.parent / "sac-base-old.sif"
        old.write_bytes(b"old")
        inner.symlink_to(old.name)
        top.symlink_to(f"sac-base/{old.name}")
        states[host] = LinkState(old.name, f"sac-base/{old.name}")
    return states


def test_source_is_resolved_and_named_by_complete_sha(tmp_path: Path):
    # Arrange
    source = tmp_path / "real.sif"
    source.write_bytes(b"abc")
    alias = tmp_path / "current.sif"
    alias.symlink_to(source.name)
    # Act
    artifact = resolve_artifact(alias, "base")
    digest = hashlib.sha256(b"abc").hexdigest()
    # Assert
    observed = (artifact.source, artifact.name, artifact.size)
    assert observed == (str(source.resolve()), f"sac-base-sha256-{digest}.sif", 3)


@pytest.mark.parametrize(
    "root", ["/", "relative/path", "/srv/*/images", "/srv/../images"]
)
def test_target_root_rejects_broad_or_ambiguous_paths(root: str):
    # Arrange
    invalid_root = root
    # Act
    # Assert
    with pytest.raises(ValueError):
        validate_target_root(invalid_root)


def test_dry_run_has_complete_receipts_and_never_calls_transport(tmp_path: Path):
    # Arrange
    artifact = _artifact(tmp_path)
    transport = FileTransport(tmp_path / "hosts", ("a", "b"))
    # Act
    result = distribute(
        artifact=artifact,
        hosts=("a", "b"),
        target_root="~/images",
        transport=transport,
        dry_run=True,
    )
    # Assert
    observed = (result.success, transport.calls, [row.status for row in result.hosts])
    assert observed == (True, [], ["planned", "planned"])


def test_all_hosts_verify_before_any_live_link_moves(tmp_path: Path):
    # Arrange
    hosts = ("a", "b")
    artifact = _artifact(tmp_path)
    transport = FileTransport(tmp_path / "hosts", hosts)
    _seed_old_links(transport, hosts, artifact)
    # Act
    result = distribute(
        artifact=artifact, hosts=hosts, target_root="~/images", transport=transport
    )
    first_activate = next(
        i for i, call in enumerate(transport.calls) if call[1] == "activate"
    )
    verify_indices = [
        i for i, call in enumerate(transport.calls) if call[1] == "verify"
    ]
    # Assert
    observed = (
        result.success,
        max(verify_indices) < first_activate,
        all(row.verified and row.status == "activated" for row in result.hosts),
        result.as_dict()["schema"],
    )
    assert observed == (True, True, True, "sac.image.distribution-receipt/v1")


def test_one_staging_mismatch_keeps_every_current_link_and_cleans_temps(tmp_path: Path):
    # Arrange
    hosts = ("a", "b")
    artifact = _artifact(tmp_path)
    transport = FileTransport(tmp_path / "hosts", hosts)
    old = _seed_old_links(transport, hosts, artifact)
    transport.fail_stage.add("b")
    # Act
    result = distribute(
        artifact=artifact, hosts=hosts, target_root="~/images", transport=transport
    )
    link_states = {}
    temps_absent = {}
    for host in hosts:
        _, _, inner, top = transport.paths(host, artifact)
        link_states[host] = LinkState(transport._state(inner), transport._state(top))
        temps_absent[host] = not any(inner.parent.glob(".incoming-distribute-*.sif"))
    # Assert
    observed = (
        result.success,
        all((host, "activate") not in transport.calls for host in hosts),
        link_states,
        temps_absent,
    )
    assert observed == (False, True, old, {"a": True, "b": True})


def test_partial_activation_is_compensated_and_old_artifacts_remain(tmp_path: Path):
    # Arrange
    hosts = ("a", "b")
    artifact = _artifact(tmp_path)
    transport = FileTransport(tmp_path / "hosts", hosts)
    old = _seed_old_links(transport, hosts, artifact)
    transport.fail_activate.add("b")
    # Act
    result = distribute(
        artifact=artifact, hosts=hosts, target_root="~/images", transport=transport
    )
    host_evidence = {}
    for host in hosts:
        final, _, inner, top = transport.paths(host, artifact)
        host_evidence[host] = (
            final.is_file(),
            (inner.parent / "sac-base-old.sif").is_file(),
            LinkState(transport._state(inner), transport._state(top)),
        )
    # Assert
    observed = (result.success, result.hosts[0].status, host_evidence)
    expected = (False, "restored", {host: (True, True, old[host]) for host in hosts})
    assert observed == expected


def test_duplicate_hosts_are_refused_before_transport(tmp_path: Path):
    # Arrange
    artifact = _artifact(tmp_path)
    transport = FileTransport(tmp_path / "hosts", ("a",))
    # Act
    # Assert
    with pytest.raises(ValueError, match="duplicates"):
        distribute(
            artifact=artifact,
            hosts=("a", "a"),
            target_root="~/images",
            transport=transport,
        )


def test_inline_receiver_stages_commits_verifies_and_activates(tmp_path: Path):
    """Exercise the exact receiver shipped over SSH, using local subprocesses."""
    # Arrange
    artifact = _artifact(tmp_path)
    root = tmp_path / "remote"
    layer_dir = root / "sac-base"
    layer_dir.mkdir(parents=True)
    old = layer_dir / "sac-base-old.sif"
    old.write_bytes(b"old")
    (layer_dir / "sac-base.sif").symlink_to(old.name)
    (root / "sac-base.sif").symlink_to(f"sac-base/{old.name}")
    token = "0123456789abcdef"

    def invoke(op: str, *, stdin: bytes | None = None, extra: str | None = None):
        argv = [
            sys.executable,
            "-c",
            _REMOTE_PROGRAM,
            op,
            str(root),
            artifact.layer,
            artifact.name,
            artifact.sha256,
            str(artifact.size),
            token,
        ]
        if extra is not None:
            argv.append(extra)
        proc = subprocess.run(argv, input=stdin, capture_output=True, check=True)
        return json.loads(proc.stdout)

    # Act
    before = invoke("inspect")
    invoke("stage", stdin=Path(artifact.source).read_bytes())
    invoke("commit")
    invoke("verify")
    after = invoke("activate", extra=json.dumps(before))
    observed = (
        invoke("active"),
        (layer_dir / artifact.name).read_bytes(),
        old.is_file(),
    )
    # Assert
    assert observed == (after, Path(artifact.source).read_bytes(), True)
