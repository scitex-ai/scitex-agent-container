"""Shared fold of a DECLARED provider value into a :class:`ProviderSpec`.

ONE parser for the inference-backend value, used by BOTH surfaces that
can carry it:

  * ``spec.claude.provider`` — the single-backend surface
    (``_parsers._claude._parse_provider`` delegates here), and
  * ``spec.engines.<key>.provider`` — one entry of the multi-backend
    surface (``_engine_types``).

Extracted rather than copied: the engine block must accept EXACTLY the
provider vocabulary the single-backend surface already accepts, and a
second implementation is how the two would drift into meaning
different things by the same word — the failure ``_harness_types``
exists to document.

It lives in its own module (not in ``_parsers``) because
``_engine_types`` is imported BY ``_types``, and ``_parsers._claude``
imports ``_types``: putting the shared fold in ``_parsers`` would close
that loop into an import cycle.
"""

from __future__ import annotations

from typing import Mapping

from ._provider_registry import resolve_provider
from ._provider_types import ProviderSpec

__all__ = ["parse_provider_value", "provider_identity"]


def parse_provider_value(block: object) -> ProviderSpec | None:
    """Fold a declared provider value into a ``ProviderSpec`` or ``None``.

    Accepts the two shapes the provider axis has always accepted:

    * **string** — a registered name from
      :mod:`config._provider_registry`. A registered name whose entry
      carries no ``base_url`` (the ``anthropic`` "no override"
      sentinel) folds to ``None``, and so does an UNREGISTERED name —
      deliberately, because the "unknown provider" diagnostic belongs
      to :func:`_provider_validation.validate_provider`, which owns the
      known-names list. Callers that must distinguish "no override"
      from "unknown name" read the RAW value, not this return.
    * **dict** ``{base_url, auth_token_env, allowed_tools}`` — forwarded
      verbatim; the validator enforces the two required fields.
    * anything else (absent, null, list, ...) → ``None``.
    """
    if isinstance(block, str):
        entry = resolve_provider(block.strip())
        if entry is None:
            return None
        base_url = str(entry.get("base_url") or "")
        token_env = str(entry.get("auth_token_env") or "")
        if not base_url and not token_env:
            return None
        return ProviderSpec(base_url=base_url, auth_token_env=token_env)
    if not isinstance(block, Mapping):
        return None
    raw_allowed = block.get("allowed_tools")
    allowed_tools: list[str] = []
    if isinstance(raw_allowed, list):
        allowed_tools = [t for t in raw_allowed if isinstance(t, str) and t]
    return ProviderSpec(
        base_url=str(block.get("base_url", "") or ""),
        auth_token_env=str(block.get("auth_token_env", "") or ""),
        allowed_tools=list(allowed_tools),
    )


def provider_identity(block: object) -> tuple[str, str] | None:
    """A COMPARABLE identity for a declared provider value.

    Returns ``(base_url, auth_token_env)`` so that a registry NAME and a
    dict that copy-pasted that registry entry compare EQUAL — which is
    what makes "the legacy block and the default engine agree" a
    decidable question rather than a string match on two spellings of
    one backend.

    ``None`` means the value declares no opinion (absent / null / the
    ``anthropic`` sentinel). An UNREGISTERED name returns a distinctive
    ``("<unregistered:NAME>", "")`` identity rather than ``None``: it
    states an opinion sac cannot honour, and collapsing it to "no
    opinion" would let an unknown name silently agree with anything.
    """
    if isinstance(block, str):
        name = block.strip()
        if not name:
            return None
        entry = resolve_provider(name)
        if entry is None:
            return (f"<unregistered:{name}>", "")
        base_url = str(entry.get("base_url") or "")
        token_env = str(entry.get("auth_token_env") or "")
        if not base_url and not token_env:
            return None
        return (base_url, token_env)
    if isinstance(block, Mapping):
        return (
            str(block.get("base_url", "") or ""),
            str(block.get("auth_token_env", "") or ""),
        )
    return None
