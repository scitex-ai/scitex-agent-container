"""Single source of truth for reading sac-owned environment variables.

Every sac env var has TWO equivalent names:
- ``SAC_<SUFFIX>`` — short form, used by interactive operators
- ``SCITEX_AGENT_CONTAINER_<SUFFIX>`` — long form, matches the package name

Both must always be honoured (sac ships TWO console-scripts, so
operators reach for whichever name matches the binary they typed).
If both are set, they MUST agree — drifted values almost always
indicate a bug in shell rc / agent yaml templating, and silently
preferring one is far worse than failing fast.

Use ``getenv()`` everywhere instead of bare ``os.environ.get(...)``
for sac-owned variables. Foreign vars (``ANTHROPIC_API_KEY``,
``CLAUDE_AGENT_ID``, ``XDG_DATA_HOME`` etc.) keep using
``os.environ.get`` directly.

Audit hook: a future PS-NEW rule will grep for ``os.environ.get("SAC_``
and ``os.environ.get("SCITEX_AGENT_CONTAINER_`` in sac source and
flag any that don't go through this helper.
"""

from __future__ import annotations

import os

_LONG_PREFIX = "SCITEX_AGENT_CONTAINER_"
_SHORT_PREFIX = "SAC_"


class SacEnvConflict(EnvironmentError):
    """Raised when SAC_<X> and SCITEX_AGENT_CONTAINER_<X> are both set
    with different values.

    The fix is in the operator's environment (typically ``~/.bashrc``
    or an agent's ``spec.env`` block): unset one or align the values.
    """


def getenv(suffix: str, default: "str | None" = None) -> "str | None":
    """Read a sac-owned env var by suffix; either prefix is accepted.

    Behaves like ``os.environ.get(name, default)`` but reads
    ``SAC_<suffix>`` AND ``SCITEX_AGENT_CONTAINER_<suffix>``,
    returning whichever is set. Raises if both are set with
    different values.

    Parameters
    ----------
    suffix
        The variable name **without** prefix. e.g. ``"HUB_URL"`` reads
        ``SAC_HUB_URL`` or ``SCITEX_AGENT_CONTAINER_HUB_URL``.
    default
        Returned when neither form is set. Default ``None`` matches
        ``os.environ.get`` semantics.

    Raises
    ------
    SacEnvConflict
        When both forms are set with different values (in either
        ``os.environ``, regardless of whether one is empty).
    """
    short_name = _SHORT_PREFIX + suffix
    long_name = _LONG_PREFIX + suffix
    short_present = short_name in os.environ
    long_present = long_name in os.environ
    if short_present and long_present:
        short_val = os.environ[short_name]
        long_val = os.environ[long_name]
        if short_val != long_val:
            raise SacEnvConflict(
                f"{short_name}={short_val!r} conflicts with "
                f"{long_name}={long_val!r}. "
                "These must agree (or only one should be set). "
                "Check ~/.bashrc, your agent's spec.env, and any wrapper "
                "scripts that export sac env vars."
            )
        return short_val
    if short_present:
        return os.environ[short_name]
    if long_present:
        return os.environ[long_name]
    return default


def setenv(suffix: str, value: str) -> None:
    """Set BOTH forms of a sac-owned env var.

    Use sparingly — most sac code reads env vars; only the runner /
    container dispatch sets them. Setting both forms keeps downstream
    code that happens to read the other form working.
    """
    os.environ[_SHORT_PREFIX + suffix] = value
    os.environ[_LONG_PREFIX + suffix] = value


def aliases(suffix: str) -> tuple[str, str]:
    """Return the two env-var names for a given suffix.

    Useful for error messages and tests that want to assert which
    name is being read.
    """
    return _SHORT_PREFIX + suffix, _LONG_PREFIX + suffix
