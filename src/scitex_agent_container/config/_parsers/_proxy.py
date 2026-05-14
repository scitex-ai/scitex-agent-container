"""Parser for ``spec.proxy`` (``kind: AgentProxy`` only).

The proxy block is meaningful exclusively when the top-level ``kind``
is ``AgentProxy``. The validator gates the kind+block coupling; this
parser is strict about the *shape* of the block once it's present:

* ``upstream``     REQUIRED — must look like an http(s) URL.
* ``trust``        optional; one of ``untrusted`` / ``local-mesh`` /
                   ``trusted`` (default ``untrusted``).
* ``redact``       optional list of substring tokens.
* ``timeout_s``    optional positive float (default 30.0).

Raises :class:`ValueError` on a malformed block so the loader surfaces
problems at boot time rather than at first turn.
"""

from __future__ import annotations

from typing import Any


def _is_url_like(value: str) -> bool:
    return value.startswith("http://") or value.startswith("https://")


def parse_proxy(spec: dict, *, kind: str = "Agent") -> Any:
    """Parse ``spec.proxy`` into a :class:`ProxySpec`.

    When ``kind != AgentProxy``, the block is ignored entirely (returns
    ``None``) — the validator is the one enforcing the kind+block
    coupling; this parser only owns the block's internal shape.
    """
    from .._proxy_types import ProxySpec, is_valid_trust

    if kind != "AgentProxy":
        return None

    raw = spec.get("proxy")
    if raw is None:
        # Required-when-kind-is-AgentProxy is enforced by the validator;
        # here we just refuse to invent defaults for a missing block.
        raise ValueError(
            "spec.proxy is required when kind: AgentProxy (no upstream to forward to)."
        )
    if not isinstance(raw, dict):
        raise ValueError(f"spec.proxy must be a mapping; got {type(raw).__name__}.")

    upstream = raw.get("upstream", "")
    if not isinstance(upstream, str) or not upstream:
        raise ValueError(
            "spec.proxy.upstream is required (kind: AgentProxy has no "
            "destination without it)."
        )
    if not _is_url_like(upstream):
        raise ValueError(
            f"spec.proxy.upstream must start with http:// or https:// "
            f"(got {upstream!r})."
        )

    trust = raw.get("trust", "untrusted")
    if not isinstance(trust, str) or not is_valid_trust(trust):
        raise ValueError(
            f"spec.proxy.trust must be one of untrusted/local-mesh/trusted "
            f"(got {trust!r})."
        )

    redact_raw = raw.get("redact", []) or []
    if not isinstance(redact_raw, list) or not all(
        isinstance(t, str) for t in redact_raw
    ):
        raise ValueError("spec.proxy.redact must be a list of strings.")
    redact = list(redact_raw)

    timeout_raw = raw.get("timeout_s", 30.0)
    try:
        timeout_s = float(timeout_raw)
    except (TypeError, ValueError) as exc:
        raise ValueError(
            f"spec.proxy.timeout_s must be numeric (got {timeout_raw!r})."
        ) from exc
    if timeout_s <= 0:
        raise ValueError(f"spec.proxy.timeout_s must be > 0 (got {timeout_s}).")

    return ProxySpec(
        upstream=upstream,
        trust=trust,
        redact=redact,
        timeout_s=timeout_s,
    )


__all__ = ["parse_proxy"]
