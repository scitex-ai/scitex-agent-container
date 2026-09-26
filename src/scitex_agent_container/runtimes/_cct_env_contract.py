"""Canonical environment boundary for SAC-managed CCT processes."""

from __future__ import annotations

from collections.abc import MutableMapping, Sequence

from ._apptainer_env_dedup import env_pair_at

RETIRED_CCT_ENV_NAMES = frozenset(
    {
        "CLAUDE_CODE_TELEGRAMMER_TELEGRAM_ALLOWED_USERS",
        "CLAUDE_CODE_TELEGRAMMER_TELEGRAM_BOT_TOKEN",
    }
)


def scrub_retired_cct_env(env: MutableMapping[str, str]) -> None:
    """Remove retired CCT aliases from a process environment in place."""
    for name in RETIRED_CCT_ENV_NAMES:
        env.pop(name, None)


def remove_retired_cct_env_flags(argv: Sequence[str]) -> list[str]:
    """Remove retired CCT ``--env`` declarations from an Apptainer argv."""
    source = list(argv)
    kept: list[str] = []
    index = 0
    while index < len(source):
        found = env_pair_at(source, index)
        if found is None:
            kept.append(source[index])
            index += 1
            continue
        key, _value, width = found
        if key not in RETIRED_CCT_ENV_NAMES:
            kept.extend(source[index : index + width])
        index += width
    return kept


__all__ = [
    "RETIRED_CCT_ENV_NAMES",
    "remove_retired_cct_env_flags",
    "scrub_retired_cct_env",
]
