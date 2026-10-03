"""Bounded, descriptor-fenced reads in an observed process's namespace."""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path


def identity(metadata):
    """Exclude atime, which the observer's own read may legitimately change."""
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_mode,
        metadata.st_uid,
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
    )


def read_bytes(path, limit, *, private=False):
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode) or before.st_size > limit:
            raise ValueError("bounded-regular-file-required")
        if private and (before.st_uid != os.getuid() or before.st_mode & 0o077):
            raise ValueError("private-owner-required")
        chunks = []
        total = 0
        while total <= limit:
            chunk = os.read(descriptor, min(65536, limit + 1 - total))
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
        data = b"".join(chunks)
        if len(data) > limit or identity(before) != identity(os.fstat(descriptor)):
            raise ValueError("observed-file-changed")
        if identity(before) != identity(Path(path).lstat()):
            raise ValueError("observed-file-replaced")
        return data
    finally:
        os.close(descriptor)


def read_json(path, limit=1024 * 1024, *, private=False):
    payload = json.loads(read_bytes(path, limit, private=private))
    if not isinstance(payload, dict):
        raise ValueError("object-required")
    return payload


def process_environment(process):
    """Values remain local to the observer; callers publish an allowlist only."""
    raw = read_bytes(process / "environ", 2 * 1024 * 1024)
    return dict(item.split("=", 1) for item in raw.decode().split("\0") if "=" in item)


def mount_source(mountinfo, destination, *, host_mountinfo):
    """Translate one explicit bind using the kernel's same-device/root mapping."""

    def entries(text):
        def decode(value):
            return (
                value.replace("\\040", " ")
                .replace("\\011", "\t")
                .replace("\\134", "\\")
            )

        rows = []
        for line in text.splitlines():
            fields = line.split()
            if len(fields) >= 7:
                rows.append((fields[2], decode(fields[3]), decode(fields[4])))
        return rows

    selected = [row for row in entries(mountinfo) if row[2] == destination]
    if len(selected) != 1:
        raise ValueError("selected-bind-unknown")
    device, root, _ = selected[0]
    candidates = []
    for host_device, host_root, host_destination in entries(host_mountinfo):
        if device != host_device:
            continue
        try:
            relative = Path(root).relative_to(host_root)
        except ValueError:
            continue
        candidate = Path(host_destination) / relative
        try:
            metadata = candidate.stat()
        except OSError:
            continue
        candidates.append((candidate, metadata.st_dev, metadata.st_ino))
    unique = {(device, inode) for _, device, inode in candidates}
    if len(unique) != 1:
        raise ValueError("selected-bind-ambiguous")
    return min((path for path, _, _ in candidates), key=lambda path: len(str(path)))
