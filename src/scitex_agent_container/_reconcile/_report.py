"""One agent's line in a reconcile report.

Extracted from :mod:`._pass` so that :mod:`._perform` — which BUILDS these —
can import the type without importing the pass that calls it. A shared value
type belongs below both of its users, not inside one of them.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ._rule import Verdict

__all__ = ["AgentReport"]


@dataclass(frozen=True)
class AgentReport:
    """One agent's line in the report. ``detail`` is ALWAYS printed."""

    name: str
    verdict: Verdict
    reason: str
    detail: str
    policy: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "verdict": self.verdict.value,
            "reason": self.reason,
            "detail": self.detail,
            "policy": self.policy,
        }
