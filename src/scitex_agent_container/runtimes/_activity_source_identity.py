"""Opaque equality identity for one owned native activity source."""

from __future__ import annotations

import hashlib
import json
import re

AUTHORITY_KEYS = ("agent", "host", "instance_id", "boot_id", "session_id")
SOURCE_KIND = "owned-native-source/v1"


def activity_source_id(target, file_identity):
    """Bind equality to all authority components and actual source metadata.

    No path, transcript, credential or readable private file identifier enters
    the public representation. The opaque token is not an authentication key.
    """
    if set(target) != set(AUTHORITY_KEYS) or any(
        not isinstance(target[key], str)
        or re.fullmatch(r"[A-Za-z0-9_.:-]{1,200}", target[key]) is None
        for key in AUTHORITY_KEYS
    ):
        raise ValueError("source identity requires exact native authority")
    if (
        not isinstance(file_identity, (tuple, list))
        or len(file_identity) != 2
        or any(type(value) is not int or value < 0 for value in file_identity)
    ):
        raise ValueError("source identity requires owned device/inode metadata")
    material = [SOURCE_KIND, *[target[key] for key in AUTHORITY_KEYS], *file_identity]
    return hashlib.sha256(
        json.dumps(material, separators=(",", ":")).encode("ascii")
    ).hexdigest()
