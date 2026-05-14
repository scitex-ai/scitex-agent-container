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


def _default_identity_path() -> Path:
    """Resolve the legacy host-identity YAML path at call time.

    Read at call time (not import time) so tests can redirect by setting
    ``$HOME`` or ``$SAC_HOST_IDENTITY_PATH``. Env override wins so the
    test isolation matches the env-driven pattern used elsewhere
    (SAC_HUB_URL, SCITEX_AGENT_CONTAINER_YAML_DIRS, ...).
    """
    env = os.environ.get("SAC_HOST_IDENTITY_PATH")
    if env:
        return Path(env)
    return Path.home() / ".scitex" / "host-identity.yaml"


# Back-compat re-export. Module consumers that read this attribute get
# the path at import time; the function above is the authoritative source.
HOST_IDENTITY_PATH = _default_identity_path()

_UNIVERSAL_LOOPBACK: set[str] = {"localhost", "127.0.0.1", "::1"}

_CACHE: set[str] | None = None


def _short(name: str) -> str:
    return name.split(".", 1)[0] if name else name


def _auto_aliases(
    *,
    hostname: str | None = None,
    nodename: str | None = None,
) -> set[str]:
    """Names this host always answers to, derived from the OS.

    Parameters are an injection point for tests — pass ``hostname`` /
    ``nodename`` to bypass the live ``socket`` / ``os.uname`` calls.
    """
    names: set[str] = set(_UNIVERSAL_LOOPBACK)
    if hostname is None:
        # stx-allow: fallback (reason: socket.gethostname() can fail in restricted container environments; loopback names are still returned as a safe baseline)
        try:
            hostname = socket.gethostname()
        except Exception:  # stx-allow: fallback (reason: catch-all safety net — see inline comment for context)
            hostname = ""
    if hostname:
        names.add(hostname)
        names.add(_short(hostname))
    if nodename is None:
        # stx-allow: fallback (reason: os.uname() is unavailable on some platforms (e.g. Windows); hostname-derived names are best-effort and the loopback set is still complete)
        try:
            nodename = os.uname().nodename
        except Exception:  # stx-allow: fallback (reason: catch-all safety net — see inline comment for context)
            nodename = ""
    if nodename:
        names.add(nodename)
        names.add(_short(nodename))
    return names


def _load_file_aliases(path: Path | None = None) -> set[str]:
    """Read aliases from the host-identity YAML.

    ``path`` defaults to ``_default_identity_path()`` so the env override
    is honoured at call time, not module-import time.
    """
    p = path if path is not None else _default_identity_path()
    if not p.exists():
        return set()
    try:
        data = yaml.safe_load(p.read_text()) or {}
    except (
        yaml.YAMLError
    ) as exc:  # stx-allow: fallback (reason: expected failure — see inline comment)
        raise RuntimeError(f"Invalid YAML in {p}: {exc}") from exc
    if not isinstance(data, dict):
        raise RuntimeError(f"{p} must be a YAML mapping, got {type(data).__name__}")
    raw = data.get("aliases") or []
    if not isinstance(raw, list):
        raise RuntimeError(f"{p}: 'aliases' must be a list, got {type(raw).__name__}")
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


def compute_identities(
    *,
    hostname: str | None = None,
    nodename: str | None = None,
    file_aliases: set[str] | None = None,
    resource_aliases: set[str] | None = None,
) -> set[str]:
    """Pure computation of the local-identity set from explicit inputs.

    The production wrapper ``get_local_identities`` calls this with
    values collected from the OS, the legacy YAML, and scitex-resource.
    Tests call it directly with controlled inputs — no module-attribute
    patching required.
    """
    auto = _auto_aliases(hostname=hostname, nodename=nodename)
    resources = resource_aliases if resource_aliases is not None else set()
    files = file_aliases if file_aliases is not None else set()
    return {n.lower() for n in (auto | resources | files) if n}


def get_local_identities() -> set[str]:
    """Return the set of names (lower-cased) this host answers to.

    Sources unioned: scitex-resource canonical/aliases, legacy
    ``host-identity.yaml`` (for back-compat), socket-derived names.
    """
    global _CACHE
    if _CACHE is not None:
        return _CACHE
    _CACHE = compute_identities(
        file_aliases=_load_file_aliases(),
        resource_aliases=_load_resource_aliases(),
    )
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
