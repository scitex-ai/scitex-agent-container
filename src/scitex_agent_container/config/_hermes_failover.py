"""Explicit account pools and engine failover for a Hermes session."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Mapping

from ._harness_lookup import canonical_harness
from ._harness_types import resolve_spec_harness


@dataclass
class HermesFailoverSpec:
    accounts: dict[str, list[str]] = field(default_factory=dict)
    engines: list[str] = field(default_factory=list)
    strategy: str = "fill_first"


def parse_selected_hermes_failover(spec: Mapping) -> HermesFailoverSpec:
    if canonical_harness(resolve_spec_harness(spec)) != "hermes":
        return HermesFailoverSpec()
    harnesses = spec.get("available_harnesses", {})
    if not isinstance(harnesses, Mapping):
        return HermesFailoverSpec()
    entry = next(
        (
            v
            for k, v in harnesses.items()
            if canonical_harness(str(k)) == "hermes" and isinstance(v, Mapping)
        ),
        {},
    )
    raw = entry.get("failover", {})
    if not isinstance(raw, Mapping) or set(raw) - {"accounts", "engines", "strategy"}:
        raise ValueError(
            "Hermes failover must be a mapping with accounts, engines and strategy"
        )
    accounts = raw.get("accounts", {})
    engines = raw.get("engines", [])
    strategy = raw.get("strategy", "fill_first")
    if strategy not in ("fill_first", "round_robin"):
        raise ValueError("Hermes failover.strategy must be fill_first or round_robin")
    if strategy != "fill_first" and not accounts:
        raise ValueError("Hermes balanced selection requires declared account pools")
    if not isinstance(accounts, Mapping):
        raise ValueError(
            "Hermes failover.accounts must map engine keys to env-name lists"
        )
    if (
        not isinstance(engines, list)
        or any(not isinstance(k, str) or not k.strip() for k in engines)
        or len(set(engines)) != len(engines)
    ):
        raise ValueError(
            "Hermes failover.engines must contain unique non-empty engine keys"
        )
    for key, names in accounts.items():
        if (
            not isinstance(key, str)
            or not key.strip()
            or not isinstance(names, list)
            or not names
        ):
            raise ValueError(
                "Hermes failover.accounts requires an engine key and a non-empty list"
            )
        if any(
            not isinstance(n, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", n)
            for n in names
        ):
            raise ValueError(
                "Hermes failover accounts must name environment variables, "
                "never key values"
            )
        if len(set(names)) != len(names):
            raise ValueError(f"Hermes failover.accounts.{key} repeats an env name")
    return HermesFailoverSpec(
        accounts={k: list(v) for k, v in accounts.items()},
        engines=list(engines),
        strategy=strategy,
    )
