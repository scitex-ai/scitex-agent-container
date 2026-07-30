"""Classify ``config.yaml`` by OBSERVED state, so MISSING cannot read as VALID.

INCIDENT 2026-07-30 (reported by scitex-cards, reproduced here): ``sac host
validate`` returned ``{"source": "/home/agent/.scitex/agent-container/
config.yaml", "errors": []}`` while that file did not exist. The operator's real
config — four peers — lives under their own home. Every ``host probe`` then
failed with "peer is not defined in config.yaml" while the validator reported
the configuration clean.

The mechanism is in :func:`..host_config.load`, which maps a missing file onto
the same defaults as a present one::

    if not p.is_file():
        return Config(source_path=p)

So downstream sees a valid-looking Config with zero peers. An absent config has
no schema to violate, which means **the one condition that actually breaks
multi-host is the one condition the check could not fail on** — a gate that
cannot fail is not a gate.

WHY A SEPARATE MODULE: ``host_config.py`` is the loader and is already past the
512-line limit; the diagnostic is a distinct responsibility (classify, do not
parse-into-a-model), so it lands here rather than growing that file.

DESIGN — one branch per observed state, each naming the resolved path. This
deliberately mirrors :func:`.._account.quota_cache.diagnose_quota_cache` so the
codebase has ONE idiom for representing missing state instead of a fresh
invention at each site. States are kept distinct because they warrant different
responses: ``absent`` is an error, whereas ``empty`` (present, no ``peers:``) is
the legitimate single-host install. Collapsing those two is the original defect.
"""

from __future__ import annotations

import os
from pathlib import Path

import yaml

from .host_config import _default_config_path

#: config.yaml is not there at all — peer resolution cannot work.
STATE_ABSENT = "absent"
#: Present but unusable (a directory, bad permissions, unreadable device).
STATE_UNREADABLE = "unreadable"
#: Present and readable but not parseable as a top-level mapping.
STATE_MALFORMED = "malformed"
#: Present and valid, but defines no peers — a legitimate single-host install.
STATE_EMPTY = "empty"
#: Present with at least one peer.
STATE_POPULATED = "populated"


def describe_config_resolution(path: Path | None = None) -> dict[str, str | None]:
    """Report WHY a config path was chosen, so an absent file is explainable.

    ``$HOME`` is included deliberately: it is the discriminator for the failure
    this module exists to make legible. User-scope resolution is HOME-derived,
    and a container's ``$HOME`` is per-container (``/home/agent``), so a config
    written under the operator's home is simply not on the path the cascade
    computes inside a container. Reporting the resolved path alone leaves the
    reader unable to tell a typo from a HOME mismatch — which is exactly the
    ambiguity that cost time on 2026-07-30.

    ``SCITEX_AGENT_CONTAINER_CONFIG`` and ``SCITEX_DIR`` are reported because
    both are honoured by the resolver and neither is injected into agent
    containers today; naming them tells a reader which lever to pull. (Note for
    anyone chasing this: ``SCITEX_AGENT_CONTAINER_HOME`` appears in prose but no
    code reads it — injecting it changes nothing.)
    """
    return {
        "resolved": str(Path(path) if path else _default_config_path()),
        "override_env": "SCITEX_AGENT_CONTAINER_CONFIG",
        "override_value": os.environ.get("SCITEX_AGENT_CONTAINER_CONFIG"),
        "scitex_dir_env": os.environ.get("SCITEX_DIR"),
        "home": os.environ.get("HOME"),
    }


def diagnose_host_config(path: Path | None = None) -> tuple[str, int, Path]:
    """Return ``(state, peer_count, resolved_path)`` for config.yaml.

    ``state`` is one of the ``STATE_*`` constants in this module. ``peer_count``
    is the number of entries under ``peers:`` and is 0 for every state other
    than :data:`STATE_POPULATED`.
    """
    p = Path(path) if path else _default_config_path()
    try:
        raw_text = p.read_text(encoding="utf-8")
    except FileNotFoundError:
        return (STATE_ABSENT, 0, p)
    except OSError:
        return (STATE_UNREADABLE, 0, p)

    try:
        parsed = yaml.safe_load(raw_text)
    except yaml.YAMLError:
        return (STATE_MALFORMED, 0, p)

    if parsed is None:
        # Present but empty file: valid single-host, not a configuration error.
        return (STATE_EMPTY, 0, p)
    if not isinstance(parsed, dict):
        return (STATE_MALFORMED, 0, p)

    peers = parsed.get("peers")
    if peers is None:
        return (STATE_EMPTY, 0, p)
    if not isinstance(peers, dict):
        return (STATE_MALFORMED, 0, p)
    return (STATE_POPULATED if peers else STATE_EMPTY, len(peers), p)


def config_state_problems(
    path: Path | None = None,
) -> tuple[list[str], list[str], dict[str, object]]:
    """Turn a diagnosis into ``(errors, warnings, detail)`` for a CLI caller.

    Split rather than a single list because the exit code depends on it: an
    absent or unparseable config must FAIL, while a present config with no
    peers must not — a single-host install is a supported configuration, and
    failing it would just teach operators to ignore the check.
    """
    state, peer_count, resolved = diagnose_host_config(path)
    resolution = describe_config_resolution(path)
    detail: dict[str, object] = {
        "state": state,
        "peers": peer_count,
        "resolution": resolution,
    }
    errors: list[str] = []
    warnings: list[str] = []

    if state == STATE_ABSENT:
        errors.append(
            f"config.yaml NOT FOUND at {resolved} — peer resolution cannot "
            f"work, so every `sac host probe` will report its peer as "
            f"undefined. This path is HOME-derived (HOME="
            f"{resolution['home']!r}); inside a container HOME is "
            f"per-container, so a config written under another home is not on "
            f"this path. Point at it explicitly with "
            f"SCITEX_AGENT_CONTAINER_CONFIG=/path/to/config.yaml, or set "
            f"SCITEX_DIR to the state root that holds it."
        )
    elif state == STATE_UNREADABLE:
        errors.append(
            f"config.yaml at {resolved} exists but could not be read "
            f"(directory, or permissions)."
        )
    elif state == STATE_MALFORMED:
        errors.append(
            f"config.yaml at {resolved} is not a valid top-level mapping with "
            f"a mapping `peers:` block."
        )
    elif state == STATE_EMPTY:
        warnings.append(
            f"config.yaml at {resolved} defines no peers — correct for a "
            f"single-host install, but multi-host commands will report every "
            f"peer as undefined."
        )

    return errors, warnings, detail
