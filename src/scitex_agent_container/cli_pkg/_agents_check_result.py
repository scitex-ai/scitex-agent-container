"""Validated, secret-free result records for ``sac agents check``.

The records are deliberately plain dataclasses rather than Pydantic models.
They are produced entirely inside this process (there is no untrusted parsing
boundary), and their explicit ``to_dict`` methods are the JSON contract. A
second schema dependency would not make that boundary safer.

The verdict vocabulary comes from :mod:`scitex_dev.status`; this command does
not invent another spelling for OK / NOT-OK / UNKNOWN. ``severity`` is a
separate presentation and exit-policy concern: a NOT-OK warning remains
non-fatal, while a NOT-OK error blocks deploy.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Iterable

from scitex_dev.status import Verdict


class Severity(Enum):
    """How strongly one check affects the command outcome."""

    INFO = "info"
    WARNING = "warning"
    ERROR = "error"


@dataclass(frozen=True, slots=True, kw_only=True)
class CheckFeedback:
    """One stable preflight finding with a shape that never varies."""

    code: str
    path: str | None
    subject: str
    verdict: Verdict
    severity: Severity
    message: str
    observed: str | None = None
    expected: str | None = None
    remedy: str | None = None

    def __post_init__(self) -> None:
        for field_name in ("code", "subject", "message"):
            value = getattr(self, field_name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{field_name} must be non-empty text")
        if not isinstance(self.verdict, Verdict):
            raise TypeError("verdict must be a scitex_dev.status.Verdict")
        if not isinstance(self.severity, Severity):
            raise TypeError("severity must be a Severity")
        if self.verdict in (Verdict.NOT_OK, Verdict.UNKNOWN) and not self.remedy:
            raise ValueError("not-ok and unknown checks must provide a remedy")

    @property
    def blocks_deploy(self) -> bool:
        return self.verdict is not Verdict.OK and self.severity is Severity.ERROR

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "path": self.path,
            "subject": self.subject,
            "status": self.verdict.value,
            "severity": self.severity.value,
            "message": self.message,
            "observed": self.observed,
            "expected": self.expected,
            "remedy": self.remedy,
        }


@dataclass(frozen=True, slots=True, kw_only=True)
class CheckResult:
    """Complete, ordered answer from ``sac agents check``."""

    subject: str
    path: str | None
    checks: tuple[CheckFeedback, ...]

    @classmethod
    def from_checks(
        cls,
        *,
        subject: str,
        path: str | None,
        checks: Iterable[CheckFeedback],
    ) -> "CheckResult":
        return cls(subject=subject, path=path, checks=tuple(checks))

    @property
    def ok(self) -> bool:
        return not any(check.blocks_deploy for check in self.checks)

    @property
    def status(self) -> str:
        if not self.ok:
            return "error"
        if any(
            check.severity is Severity.WARNING or check.verdict is Verdict.UNKNOWN
            for check in self.checks
        ):
            return "warning"
        return "ok"

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "status": self.status,
            "subject": self.subject,
            "path": self.path,
            "checks": [check.to_dict() for check in self.checks],
        }


__all__ = ["CheckFeedback", "CheckResult", "Severity"]
