"""The ``spec.grants.host_exec`` gate for ``POST /v1/host_exec``.

``host_exec`` already refuses callers outside the eligible groups. A spec
that carries an explicit ``spec.grants`` block (deny-by-default, see
``config._grants``) must ALSO grant ``host_exec: true``; a legacy spec (no
block) is unaffected, so the group gate alone decides exactly as before.

The spec is read from FLEET scope only — the same resolver the group gate
uses (``config._group_authority._default_spec_path``), never project scope,
so a file inside a repository the agent edits cannot grant it host exec.
The YAML is read directly: an authority answer must not depend on the
whole spec validating.
"""

from __future__ import annotations

from typing import Callable, Mapping, NamedTuple

__all__ = ["HostExecGrant", "host_exec_grant"]


class HostExecGrant(NamedTuple):
    """``allowed`` plus the sentence the deny response / audit line carries."""

    allowed: bool
    reason: str


def _read_spec_block(path: str) -> Mapping | None:
    import yaml

    with open(path, "r", encoding="utf-8") as fh:
        doc = yaml.safe_load(fh)
    if not isinstance(doc, Mapping):
        return None
    spec = doc.get("spec")
    return spec if isinstance(spec, Mapping) else {}


def host_exec_grant(
    name: str,
    *,
    spec_path_resolver: Callable[[str], str | None] | None = None,
) -> HostExecGrant:
    """Decide the grant half of the host_exec gate for caller ``name``.

    * no fleet spec visible → allowed (legacy: the group gate decides);
    * spec without ``grants`` → allowed (legacy default);
    * ``grants.host_exec: true`` → allowed;
    * anything else — including an unreadable spec, whose grants cannot be
      known — → refused, naming why.
    """
    if spec_path_resolver is None:
        from ..config._group_authority import _default_spec_path

        spec_path_resolver = _default_spec_path
    path = spec_path_resolver(name)
    if not path:
        return HostExecGrant(True, "no fleet spec visible; group gate decides")
    try:
        spec = _read_spec_block(path)
    except Exception as exc:  # stx-allow: fallback (refuse: grants unknowable)
        return HostExecGrant(
            False, f"spec for {name!r} at {path} is unreadable ({exc}); refusing"
        )
    if spec is None:
        return HostExecGrant(False, f"spec for {name!r} at {path} is not a mapping")
    grants = spec.get("grants")
    if grants is None:
        return HostExecGrant(True, "legacy spec (no spec.grants); group gate decides")
    if isinstance(grants, Mapping) and grants.get("host_exec") is True:
        return HostExecGrant(True, "spec.grants.host_exec: true")
    return HostExecGrant(
        False,
        f"spec.grants of {name!r} does not grant host_exec (deny-by-default)",
    )
