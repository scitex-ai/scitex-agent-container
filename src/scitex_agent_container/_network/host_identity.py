"""Local-vs-remote host identity check.

Canonical name + aliases come from :mod:`scitex_resource` —
``~/.scitex/resource/config.yaml``'s ``machine.canonical_name`` /
``machine.aliases``. See scitex-resource README and the scitex-python
``arch-local-state-directories`` skill (§9 Cross-package SoC) for the
ecosystem rule: one package owns each domain; this module consumes
the public API.

Legacy ``~/.scitex/host-identity.yaml`` is still read for back-compat —
its aliases are unioned in. Migrate by moving them to
``~/.scitex/resource/config.yaml`` under ``machine.aliases`` and
deleting the legacy file.

File schema::

    aliases:
      - nas
      - DXP480TPLUS-994
      - ugreen
      - localhost

If the file is absent, defaults are auto-derived from ``socket``
(``hostname``, short hostname, ``os.uname().nodename``, plus the
universal loopback names). Bootstrap with::

    sac host-identity init [--alias <ssh-name>]...
"""

from __future__ import annotations

import os
import socket
from pathlib import Path

import yaml

HOST_IDENTITY_PATH = Path.home() / ".scitex" / "host-identity.yaml"

_UNIVERSAL_LOOPBACK: set[str] = {"localhost", "127.0.0.1", "::1"}

_CACHE: set[str] | None = None


def _short(name: str) -> str:
    return name.split(".", 1)[0] if name else name


def _auto_aliases() -> set[str]:
    """Names this host always answers to, derived from the OS."""
    names: set[str] = set(_UNIVERSAL_LOOPBACK)
    # stx-allow: fallback (reason: socket.gethostname() can fail in restricted container environments; loopback names are still returned as a safe baseline)
    try:
        hn = socket.gethostname()
        if hn:
            names.add(hn)
            names.add(_short(hn))
    except Exception:  # stx-allow: fallback (reason: catch-all safety net — see inline comment for context)
        pass
    # stx-allow: fallback (reason: os.uname() is unavailable on some platforms (e.g. Windows); hostname-derived names are best-effort and the loopback set is still complete)
    try:
        nn = os.uname().nodename
        if nn:
            names.add(nn)
            names.add(_short(nn))
    except Exception:  # stx-allow: fallback (reason: catch-all safety net — see inline comment for context)
        pass
    return names


def _load_file_aliases() -> set[str]:
    """Read aliases from ``~/.scitex/host-identity.yaml``."""
    if not HOST_IDENTITY_PATH.exists():
        return set()
    try:
        data = yaml.safe_load(HOST_IDENTITY_PATH.read_text()) or {}
    except (
        yaml.YAMLError
    ) as exc:  # stx-allow: fallback (reason: expected failure — see inline comment)
        raise RuntimeError(f"Invalid YAML in {HOST_IDENTITY_PATH}: {exc}") from exc
    if not isinstance(data, dict):
        raise RuntimeError(
            f"{HOST_IDENTITY_PATH} must be a YAML mapping, got {type(data).__name__}"
        )
    raw = data.get("aliases") or []
    if not isinstance(raw, list):
        raise RuntimeError(
            f"{HOST_IDENTITY_PATH}: 'aliases' must be a list, got {type(raw).__name__}"
        )
    return {str(a).strip() for a in raw if a is not None and str(a).strip()}


def _load_resource_aliases() -> set[str]:
    """Aliases declared in scitex-resource's machine config."""
    from scitex_dev import try_import_optional

    scitex_resource = try_import_optional(
        "scitex_resource", pkg="scitex-agent-container"
    )
    if scitex_resource is None:
        return set()
    get_machine_config = scitex_resource.get_machine_config
    get_machine_name = scitex_resource.get_machine_name
    out: set[str] = set()
    name = (get_machine_name() or "").strip()
    if name:
        out.add(name)
    cfg = get_machine_config()
    for a in cfg.get("aliases") or []:
        if isinstance(a, str) and a.strip():
            out.add(a.strip())
    return out


def get_local_identities() -> set[str]:
    """Return the set of names (lower-cased) this host answers to.

    Sources unioned: scitex-resource canonical/aliases, legacy
    ``host-identity.yaml`` (for back-compat), socket-derived names.
    """
    global _CACHE
    if _CACHE is not None:
        return _CACHE
    names = _auto_aliases() | _load_resource_aliases() | _load_file_aliases()
    _CACHE = {n.lower() for n in names if n}
    return _CACHE


def is_local_host(name: str | None) -> bool:
    """Return True if ``name`` refers to the current host."""
    if name is None:
        return True
    n = name.strip().lower()
    if not n:
        return True
    return n in get_local_identities()


def _reset_cache_for_tests() -> None:
    global _CACHE
    _CACHE = None
