"""Per-host agent-spec manifest + cross-host diff (`sac fleet sync`).

Two pure functions:

  * :func:`build_manifest` walks an agents directory and produces a
    flat (filepath -> {sha256, size, mode}) map per agent. Used both
    on the lead host (locally) and on each peer (invoked via ssh as
    ``sac fleet sync --collect`` — the worker mode).

  * :func:`diff_manifests` collates one manifest per host and emits a
    structured diff. NEVER auto-merges. The contract is "report every
    disagreement; let the operator pick the authoritative copy."

Why this lives under ``_state`` and not ``cli_pkg``:

  * Pure functions, no Click, no subprocess — testable on tmp_path
    fixtures without any ssh fan-out.
  * The CLI layer (`cli_pkg/_fleet_sync.py`) wraps these in the
    cross-host orchestration that ssh's per-peer.

Manifest shape (locked in tests/_state/test_spec_manifest.py):

    {
      "host": "<canonical name>",
      "agents_dir": "<abs path>",
      "agents": {
        "<agent>": {
          "present": True | False,
          "files": {
            "spec.yaml":              {"sha256": "...", "size": int, "mode": "0644"},
            "to_home/CLAUDE.md":      {"sha256": "...", "size": int, "mode": "0644"},
            "to_home/.claude/x.md":   {...},
          },
        }
      },
      "errors": []
    }

Diff shape (locked in tests):

    {
      "ok": bool,
      "fleet": ["host-a", "host-b", ...],
      "agents": {
        "<agent>": {
          "ok": bool,
          "conflicts": [
            {
              "file":           "spec.yaml" | "to_home/..." | "<agent-dir>",
              "kind":           "sha256_mismatch" | "missing_on_host"
                              | "agent_missing_on_host" | "mode_mismatch",
              "per_host":       {host: {"present": bool, "sha256": str, ...}},
              "diverged_hosts": [host, ...]
            }
          ]
        }
      }
    }
"""

from __future__ import annotations

import hashlib
from collections import Counter
from pathlib import Path
from typing import Any, Iterable

__all__ = ["build_manifest", "diff_manifests"]


# ---------------------------------------------------------------------------
# Manifest build (per host).
# ---------------------------------------------------------------------------


def _file_record(p: Path) -> dict[str, Any]:
    data = p.read_bytes()
    return {
        "sha256": hashlib.sha256(data).hexdigest(),
        "size": len(data),
        "mode": f"{p.stat().st_mode & 0o777:04o}",
    }


def _walk_to_home(to_home: Path) -> dict[str, dict[str, Any]]:
    """Return ``{"to_home/<rel>": {sha256, size, mode}, ...}`` for every regular file."""
    out: dict[str, dict[str, Any]] = {}
    if not to_home.is_dir():
        return out
    for f in sorted(to_home.rglob("*")):
        if not f.is_file():
            continue
        rel = f.relative_to(to_home).as_posix()
        out[f"to_home/{rel}"] = _file_record(f)
    return out


def build_manifest(
    *,
    host: str,
    agents_dir: Path,
    only: Iterable[str] | None = None,
) -> dict[str, Any]:
    """Walk ``agents_dir`` and build a per-host manifest.

    An agent is recognized iff ``<agents_dir>/<name>/spec.yaml`` exists.
    Stray subdirs without spec.yaml are skipped (not silently merged
    into the fleet view — they aren't agents).

    ``only`` restricts the scan to a specific name set. Names in
    ``only`` that aren't present on disk are recorded with
    ``present: False`` so the diff layer can still surface them.
    """
    agents_dir = Path(agents_dir)
    agents: dict[str, dict[str, Any]] = {}
    errors: list[str] = []

    only_set = set(only) if only is not None else None

    discovered: set[str] = set()
    if agents_dir.is_dir():
        for sub in sorted(agents_dir.iterdir()):
            if not sub.is_dir():
                continue
            spec = sub / "spec.yaml"
            if not spec.is_file():
                continue
            name = sub.name
            if only_set is not None and name not in only_set:
                continue
            discovered.add(name)
            files: dict[str, dict[str, Any]] = {
                "spec.yaml": _file_record(spec),
            }
            files.update(_walk_to_home(sub / "to_home"))
            agents[name] = {"present": True, "files": files}

    if only_set is not None:
        for name in only_set - discovered:
            agents[name] = {"present": False, "files": {}}

    return {
        "host": host,
        "agents_dir": str(agents_dir),
        "agents": agents,
        "errors": errors,
    }


# ---------------------------------------------------------------------------
# Diff (cross-host).
# ---------------------------------------------------------------------------


def _minority_hosts(per_host_value: dict[str, Any]) -> list[str]:
    """Hosts whose value is in the smallest equivalence class.

    On a tie (every value is unique, or all classes equal-sized), every
    host is "diverged" — sac NEVER picks a winner. The list is
    informational only; the operator decides the authoritative copy.
    """
    # Normalise unhashable dicts to a deterministic tuple-key.
    def _key(v: Any) -> Any:
        if isinstance(v, dict):
            return tuple(sorted(v.items()))
        return v

    counts = Counter(_key(v) for v in per_host_value.values())
    if not counts:
        return []
    max_count = max(counts.values())
    classes_by_size = sorted(counts.items(), key=lambda kv: kv[1])
    smallest = classes_by_size[0][1]
    if smallest == max_count:
        # tie — sac picks no winner; every disagreeing host is "diverged"
        return sorted(per_host_value.keys())
    minority_keys = {k for k, c in counts.items() if c < max_count}
    return sorted(
        h for h, v in per_host_value.items() if _key(v) in minority_keys
    )


def _diff_one_file(
    *,
    file_label: str,
    per_host_record: dict[str, dict[str, Any] | None],
) -> list[dict[str, Any]]:
    """Compare one file across hosts. Returns 0 or 1 conflict dicts (a
    file produces at most one conflict per kind — sha256 OR missing OR
    mode. We return whichever fires; precedence: missing > sha > mode)."""
    conflicts: list[dict[str, Any]] = []

    # Presence first.
    present_hosts = [h for h, rec in per_host_record.items() if rec is not None]
    absent_hosts = [h for h, rec in per_host_record.items() if rec is None]
    if absent_hosts and present_hosts:
        per_host: dict[str, dict[str, Any]] = {}
        for h, rec in per_host_record.items():
            if rec is None:
                per_host[h] = {"present": False}
            else:
                per_host[h] = {"present": True, **rec}
        # Diverged set is "whichever class is smaller" — never silent.
        diverged = _minority_hosts({h: rec is not None for h, rec in per_host_record.items()})
        conflicts.append(
            {
                "file": file_label,
                "kind": "missing_on_host",
                "per_host": per_host,
                "diverged_hosts": diverged,
            }
        )
        return conflicts

    if not present_hosts:
        return conflicts  # file absent on every host -> nothing to report

    # All present: check sha256 then mode.
    shas = {h: per_host_record[h]["sha256"] for h in present_hosts}
    if len(set(shas.values())) > 1:
        per_host = {
            h: {"present": True, **per_host_record[h]} for h in present_hosts
        }
        conflicts.append(
            {
                "file": file_label,
                "kind": "sha256_mismatch",
                "per_host": per_host,
                "diverged_hosts": _minority_hosts(shas),
            }
        )
        return conflicts

    modes = {h: per_host_record[h]["mode"] for h in present_hosts}
    if len(set(modes.values())) > 1:
        per_host = {
            h: {"present": True, **per_host_record[h]} for h in present_hosts
        }
        conflicts.append(
            {
                "file": file_label,
                "kind": "mode_mismatch",
                "per_host": per_host,
                "diverged_hosts": _minority_hosts(modes),
            }
        )
        return conflicts

    return conflicts


def diff_manifests(
    per_host_manifest: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    """Cross-host diff. Returns the conflict envelope; never mutates inputs.

    A single-host input is trivially ``ok: True`` — one host can't
    disagree with itself.

    Loudness contract: every file present on at least one host but with
    any sha/size/mode disagreement OR with any host-side absence is
    surfaced. No fallback, no majority-wins rewrite.
    """
    fleet = list(per_host_manifest.keys())
    if len(fleet) <= 1:
        return {"ok": True, "fleet": fleet, "agents": {}}

    # Gather union of agent names across all hosts.
    all_agents: set[str] = set()
    for m in per_host_manifest.values():
        all_agents.update(m["agents"].keys())

    agents_out: dict[str, dict[str, Any]] = {}
    overall_ok = True

    for agent in sorted(all_agents):
        per_host_presence = {
            h: per_host_manifest[h]["agents"].get(agent, {"present": False})
            for h in fleet
        }
        presence_bools = {h: rec.get("present", False) for h, rec in per_host_presence.items()}

        conflicts: list[dict[str, Any]] = []

        if len(set(presence_bools.values())) > 1:
            # Whole-agent presence disagreement.
            conflicts.append(
                {
                    "file": "<agent-dir>",
                    "kind": "agent_missing_on_host",
                    "per_host": {h: {"present": v} for h, v in presence_bools.items()},
                    "diverged_hosts": _minority_hosts(presence_bools),
                }
            )

        # For every file path appearing on any host where the agent IS
        # present, build a per-host record (None where absent on that
        # host AND the agent was nominally present — that's a real
        # missing_on_host conflict). We deliberately ignore files for
        # hosts where the whole agent is absent — those are already
        # captured by `agent_missing_on_host`; double-flagging every
        # file would drown the operator.
        present_on_hosts = [h for h in fleet if presence_bools[h]]
        if len(present_on_hosts) >= 2:
            file_union: set[str] = set()
            for h in present_on_hosts:
                file_union.update(per_host_presence[h]["files"].keys())
            for file_label in sorted(file_union):
                per_host_record: dict[str, dict[str, Any] | None] = {}
                for h in present_on_hosts:
                    rec = per_host_presence[h]["files"].get(file_label)
                    per_host_record[h] = rec  # None if absent on this host
                conflicts.extend(
                    _diff_one_file(
                        file_label=file_label, per_host_record=per_host_record
                    )
                )

        agent_ok = not conflicts
        if not agent_ok:
            overall_ok = False
        agents_out[agent] = {"ok": agent_ok, "conflicts": conflicts}

    return {
        "ok": overall_ok,
        "fleet": fleet,
        "agents": agents_out,
    }


# EOF
