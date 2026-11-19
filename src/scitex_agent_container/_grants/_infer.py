"""Infer the ``grants:`` block that reproduces an agent's LEGACY authority,
and prove it.

Inference is by rendering, not by re-deriving each authority's conditions
(which would be a second copy of the logic that can drift from the first):

1. render the spec with NO ``grants:`` key — the legacy surface;
2. render it again, identically — anything that differs between two
   identical renders (timestamps, fresh tokens) is VOLATILE and excluded;
3. for every grant (and every part of a list grant), render the spec with
   ALL grants except that one. If the surface changes, the agent holds that
   authority today, so the grant is inferred.

When the legacy render is itself refused (a pre-existing spec problem), the
authorities past the refusal cannot be observed, so inference falls back to
the complete block — the legacy default by construction — and the result is
reported INCONCLUSIVE rather than pass.

``host_exec`` is the one grant with no launch surface (it is decided by the
listen server per request), so it is inferred from the same fleet-group
eligibility the server applies, and needs ``sac_control`` to be reachable.

:func:`check` then renders the spec + the inferred block and diffs it
against the legacy surface: zero diff = the block reproduces today's
authority exactly.
"""

from __future__ import annotations

import copy
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

from ._render import LaunchSurface, render_surface

__all__ = ["GrantsCheck", "check", "infer", "load_variant"]


def load_variant(raw: Mapping, path: Path, block: Mapping | None) -> Any:
    """Load ``raw`` with ``spec.grants`` replaced by ``block`` (None = absent).

    Goes through validation + the loader, so load-time grant effects (the
    sac-builtin injection) and contradictions are the real ones.
    """
    from ..config import load_v3
    from ..config._validation import validate_raw

    doc = copy.deepcopy(dict(raw))
    spec = dict(doc.get("spec") or {})
    spec.pop("grants", None)
    if block is not None:
        spec["grants"] = dict(block)
    doc["spec"] = spec
    errors = validate_raw(doc, str(path))
    if errors:
        raise ValueError("; ".join(errors))
    return load_v3(doc, path)


def _surface(
    raw: Mapping, path: Path, block: Mapping | None, root: Path
) -> LaunchSurface:
    try:
        config = load_variant(raw, path, block)
    except Exception as exc:  # stx-allow: fallback (an invalid variant IS a difference)
        return LaunchSurface(error=f"invalid: {exc}")
    return render_surface(config, root=root)


_HOME_KEY = re.compile(r"^(.*?)(?: sha256:| -> | \[)")


def _home_key(line: str) -> str:
    match = _HOME_KEY.match(line)
    return match.group(1) if match else line


def _volatile(a: LaunchSurface, b: LaunchSurface) -> dict[str, set]:
    """What differs between two IDENTICAL renders, keyed by identity:
    argv by position, home by path, env / error by line."""
    argv_idx: set[int] = set()
    if len(a.argv) == len(b.argv):
        argv_idx = {i for i, (x, y) in enumerate(zip(a.argv, b.argv)) if x != y}
    home_a = {_home_key(x): x for x in a.home}
    home_b = {_home_key(x): x for x in b.home}
    return {
        "argv": argv_idx,
        "home": {k for k in home_a.keys() | home_b.keys() if home_a.get(k) != home_b.get(k)},
        "env": set(a.env_names) ^ set(b.env_names),
        "error": {a.error, b.error} - {None} if a.error != b.error else set(),
    }


def _mask(surface: LaunchSurface, volatile: Mapping[str, set]) -> dict[str, list[str]]:
    """The comparable surface with volatile entries neutralised."""
    vol_argv = volatile.get("argv", set())
    vol_home = volatile.get("home", set())
    lines = surface.lines()
    return {
        "argv": ["<volatile>" if i in vol_argv else x for i, x in enumerate(lines["argv"])],
        "env": [x for x in lines["env"] if x not in volatile.get("env", set())],
        "home": [
            f"{_home_key(x)} <volatile>" if _home_key(x) in vol_home else x
            for x in lines["home"]
        ],
        "error": [x for x in lines["error"] if x not in volatile.get("error", set())],
    }


def _diff(a: dict[str, list[str]], b: dict[str, list[str]]) -> list[str]:
    lines: list[str] = []
    for key in a:
        left, right = a[key], b[key]
        changed = [f"- {key}: {item}" for item in sorted(set(left) - set(right))]
        changed += [f"+ {key}: {item}" for item in sorted(set(right) - set(left))]
        if not changed and left != right:
            changed.append(f"~ {key}: same entries, different order")
        lines += changed
    return lines


@dataclass
class GrantsCheck:
    """The inferred block, and the proof that it reproduces the legacy surface."""

    block: dict[str, Any]
    diff: list[str] = field(default_factory=list)
    volatile: int = 0
    legacy_error: str | None = None

    @property
    def conclusive(self) -> bool:
        """False when the legacy render itself is refused: the surface then
        stops at the refusal, so a 0 diff covers only what was rendered."""
        return self.legacy_error is None

    @property
    def passed(self) -> bool:
        return not self.diff and self.conclusive


def infer(
    raw: Mapping, path: Path, *, root: Path
) -> tuple[dict[str, Any], LaunchSurface, dict]:
    """Return ``(block, legacy_surface, volatile)`` for spec ``raw`` at ``path``."""
    from ..config._grants import BOOL_GRANTS, LIST_GRANTS, all_granted, host_exec_state

    legacy = _surface(raw, path, None, root)
    # Straddle a clock second so second-resolution stamps (e.g. a generated
    # section header) show up as volatile instead of as a grant difference.
    time.sleep(1.1)
    volatile = _volatile(legacy, _surface(raw, path, None, root))
    base = _mask(legacy, volatile)
    full = all_granted().to_block()
    if (raw.get("spec") or {}).get("sandbox") is not None:
        full["host_network"] = False  # a sealed run's network is its allowlist

    def needed(block: Mapping) -> bool:
        return _mask(_surface(raw, path, block, root), volatile) != base

    def eligible() -> bool:
        try:
            return host_exec_state(load_variant(raw, path, None))[0]
        except Exception:  # stx-allow: fallback (an invalid spec holds nothing)
            return False

    if legacy.error:
        # The legacy render is itself refused, so authorities past the refusal
        # cannot be observed. Infer CONSERVATIVELY — every grant (the legacy
        # default by construction) — rather than withhold what was not seen.
        return (
            {**full, "host_exec": eligible()},
            legacy,
            volatile,
        )
    block: dict[str, Any] = {}
    for key in BOOL_GRANTS:
        if key == "host_exec":
            continue
        block[key] = needed({**full, key: False, "host_exec": False})
    for key, parts in LIST_GRANTS.items():
        block[key] = [
            part
            for part in parts
            if needed(
                {**full, key: [p for p in parts if p != part], "host_exec": False}
            )
        ]
    block["host_exec"] = bool(block.get("sac_control")) and eligible()
    from ..config._grants import GRANT_KEYS

    return {key: block[key] for key in GRANT_KEYS}, legacy, volatile


def check(raw: Mapping, path: Path, *, root: Path) -> GrantsCheck:
    """Infer, then diff legacy vs spec+inferred block (volatile lines excluded)."""
    block, legacy, volatile = infer(raw, path, root=root)
    granted = _surface(raw, path, block, root)
    diff = _diff(_mask(legacy, volatile), _mask(granted, volatile))
    return GrantsCheck(
        block=block,
        diff=diff,
        volatile=sum(len(v) for v in volatile.values()),
        legacy_error=legacy.error,
    )
