"""Parser for ``spec.a2a`` (Agent-to-Agent transport)."""

from __future__ import annotations


def parse_a2a(spec: dict) -> "A2ASpec":  # noqa: F821
    """Parse ``spec.a2a`` into an :class:`A2ASpec`.

    Port semantics (mirrors the dataclass docstring):

      * **Unset / no a2a block** → ``port="auto"`` (default).
        Auto-allocate at agent_start. This is the new default — was
        ``None`` (sidecar disabled) before this commit.
      * **``port: auto``** → ``port="auto"`` sentinel.
      * **``port: <int>``** → operator-pinned; cast to int.
      * **``port: null``** → ``port=None``; sidecar explicitly disabled.

    Unknown string values raise ``ValueError`` rather than silently
    falling back — the validator surfaces this to the operator.
    """
    from .._types import A2ASpec

    raw = spec.get("a2a", {}) or {}
    host = str(raw.get("host", "127.0.0.1"))

    # Distinguish "key absent" (default to auto) from "key present
    # with value None" (explicit disable). ``raw`` is always a dict
    # here; check key membership rather than getting the default.
    if "port" not in raw:
        return A2ASpec(host=host, port="auto")
    val = raw["port"]
    if val is None:
        return A2ASpec(host=host, port=None)
    if isinstance(val, str):
        if val == "auto":
            return A2ASpec(host=host, port="auto")
        raise ValueError(
            f"spec.a2a.port: unknown string {val!r}; expected 'auto' or an int"
        )
    return A2ASpec(host=host, port=int(val))
