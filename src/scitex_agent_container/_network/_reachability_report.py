"""The ANSWER SHAPE of ``sac a2a reachability`` — rows, report, exit codes, file.

Split out of :mod:`._reachability` (the probe) so the shape a consumer parses
is one module and the leg that produces it is another. Everything here is
re-exported from :mod:`._reachability`, so ``from ._reachability import
HostReachability`` keeps resolving.

THREE VALUES, NEVER TWO
    :attr:`HostReachability.reachable` is ``True`` / ``False`` / ``None``.
    ``None`` is UNKNOWN — the probe could not be run at all — and it is NEVER
    counted as reachable and NEVER as unreachable. A pass in which every host
    is unknown measured nothing, and :func:`exit_code_for` says so with its
    own code (:data:`EXIT_NOTHING_MEASURABLE`) rather than 0.

THE ``local`` FLAG
    Exactly one row per pass is THIS machine, and which one is decided ONCE,
    at resolution (:func:`._reachability.resolve_targets`, against the
    caller's ``local_names``). The decision is carried here as
    :attr:`HostReachability.local` so every consumer — the alarm above all —
    skips the self row by the same decision instead of re-deriving it from
    a host name that may be spelled differently (``DXP480TPLUS-994`` is
    ``scitex-nas-03``; ``canonical_host()`` may say either).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping

__all__ = [
    "EXIT_ALL_REACHABLE",
    "EXIT_NOTHING_MEASURABLE",
    "EXIT_UNREACHABLE",
    "REPORT_FILENAME",
    "TRANSPORT_NONE",
    "TRANSPORT_SSH",
    "HostReachability",
    "ReachabilityReport",
    "default_report_path",
    "exit_code_for",
    "read_report",
    "write_report",
]

#: ``transport`` values. ``ssh`` = the probe ran (or was refused only for a
#: missing token) over the forwarder's ssh leg; ``none`` = there is no leg to
#: run — no alias, or the host is this machine.
TRANSPORT_SSH = "ssh"
TRANSPORT_NONE = "none"

#: Exit codes. Documented in the verb's help; 2 is deliberately NOT used, it
#: stays Click's usage-error code and carries no domain meaning here.
EXIT_ALL_REACHABLE = 0
EXIT_UNREACHABLE = 1
EXIT_NOTHING_MEASURABLE = 3

#: Where ``--record`` lands the report, relative to sac's runtime root.
REPORT_FILENAME = "a2a-reachability.json"


@dataclass(frozen=True)
class HostReachability:
    """One host's verdict. The fixed answer shape ``--json`` emits per host.

    ``reachable`` is three-valued (see the module docstring); ``elapsed_ms``
    is ``None`` whenever nothing was dispatched; ``error`` is ``None`` only
    for ``reachable=True`` and otherwise names what stopped the probe, with
    the file or value to fix when there is one. ``local`` is True for the
    one row that is THIS machine — decided once, at resolution, and carried
    here so no consumer re-derives it from the host's spelling.
    """

    host: str
    ssh_alias: str | None
    transport: str
    reachable: bool | None
    elapsed_ms: int | None
    error: str | None
    local: bool = False

    def __post_init__(self) -> None:
        if self.transport not in (TRANSPORT_SSH, TRANSPORT_NONE):
            raise ValueError(
                f"HostReachability({self.host!r}).transport must be "
                f"{TRANSPORT_SSH!r} or {TRANSPORT_NONE!r}, got {self.transport!r}"
            )
        if self.reachable is not None and not isinstance(self.reachable, bool):
            raise ValueError(
                f"HostReachability({self.host!r}).reachable must be True, False "
                f"or None, got {self.reachable!r}"
            )
        if self.transport == TRANSPORT_NONE and self.reachable is not None:
            raise ValueError(
                f"HostReachability({self.host!r}): transport 'none' cannot carry "
                f"a measured verdict ({self.reachable!r}) — nothing was dispatched"
            )
        if self.reachable is None and not self.error:
            raise ValueError(
                f"HostReachability({self.host!r}): an UNKNOWN row must say why"
            )
        if self.local and self.transport != TRANSPORT_NONE:
            raise ValueError(
                f"HostReachability({self.host!r}): this host has no leg to probe, "
                f"so a local row cannot carry transport {self.transport!r}"
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "host": self.host,
            "ssh_alias": self.ssh_alias,
            "transport": self.transport,
            "reachable": self.reachable,
            "elapsed_ms": self.elapsed_ms,
            "error": self.error,
            "local": self.local,
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "HostReachability":
        return cls(
            host=str(raw["host"]),
            ssh_alias=raw.get("ssh_alias"),
            transport=str(raw["transport"]),
            reachable=raw.get("reachable"),
            elapsed_ms=raw.get("elapsed_ms"),
            error=raw.get("error"),
            local=bool(raw.get("local", False)),
        )


def exit_code_for(rows: Iterable[HostReachability]) -> int:
    """Map a pass's rows onto the three documented exit codes.

    * :data:`EXIT_UNREACHABLE` (1) — at least one host measured ``False``.
    * :data:`EXIT_NOTHING_MEASURABLE` (3) — no host measured anything: every
      row is UNKNOWN, or there are no rows. This is NOT success; a pass
      that could not look must not read as a pass that looked and found
      nothing wrong.
    * :data:`EXIT_ALL_REACHABLE` (0) — every MEASURED host is ``True``.
      UNKNOWN rows alongside measured ones do not turn 0 into 3: the
      measured hosts are still a real answer, and the unknown ones are
      listed by name in the report.
    """
    measured = [row.reachable for row in rows if row.reachable is not None]
    if not measured:
        return EXIT_NOTHING_MEASURABLE
    if any(value is False for value in measured):
        return EXIT_UNREACHABLE
    return EXIT_ALL_REACHABLE


@dataclass(frozen=True)
class ReachabilityReport:
    """The whole pass: every row plus where and when it was taken from.

    This is what ``--record`` persists and ``--last`` reads back: the FULL
    per-pass picture, every host every pass. The event log deliberately does
    not carry that (it records transitions — see :mod:`._reachability_alarm`),
    so "what did the last pass see?" is answered here, not there.
    """

    probed_from: str
    port: int
    started_at_utc: str
    elapsed_ms: int
    rows: tuple[HostReachability, ...]

    @property
    def exit_code(self) -> int:
        return exit_code_for(self.rows)

    def counts(self) -> dict[str, int]:
        return {
            "hosts": len(self.rows),
            "reachable": sum(1 for r in self.rows if r.reachable is True),
            "unreachable": sum(1 for r in self.rows if r.reachable is False),
            "unknown": sum(1 for r in self.rows if r.reachable is None),
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "probed_from": self.probed_from,
            "port": self.port,
            "started_at_utc": self.started_at_utc,
            "elapsed_ms": self.elapsed_ms,
            "exit_code": self.exit_code,
            "counts": self.counts(),
            "hosts": [row.to_dict() for row in self.rows],
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "ReachabilityReport":
        return cls(
            probed_from=str(raw["probed_from"]),
            port=int(raw["port"]),
            started_at_utc=str(raw["started_at_utc"]),
            elapsed_ms=int(raw["elapsed_ms"]),
            rows=tuple(HostReachability.from_dict(r) for r in raw.get("hosts", [])),
        )


def default_report_path() -> Path:
    """``runtime_root()/a2a-reachability.json`` — resolved per call."""
    from .._state.state_paths import runtime_root

    return runtime_root() / REPORT_FILENAME


def write_report(report: ReachabilityReport, *, path: Path | None = None) -> Path:
    """Persist ``report`` atomically (tmp + rename). Raises on failure.

    Deliberately NOT fail-open: ``--record`` is the scheduled job's whole
    reason to run, and a report that silently did not land would leave the
    consumer reading a stale one that says the fleet was fine.
    """
    target = Path(path) if path is not None else default_report_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(target.name + ".tmp")
    tmp.write_text(json.dumps(report.to_dict(), indent=2), encoding="utf-8")
    tmp.replace(target)
    return target


def read_report(*, path: Path | None = None) -> ReachabilityReport | None:
    """The last recorded report, or ``None`` when none has been recorded.

    A file that exists but does not parse raises — a corrupt record is not
    "no record", and pretending otherwise would hide a broken writer.
    """
    target = Path(path) if path is not None else default_report_path()
    if not target.is_file():
        return None
    raw = json.loads(target.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError(f"{target} does not hold a reachability report object")
    return ReachabilityReport.from_dict(raw)
