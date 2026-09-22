"""Per-host checkout manifest + cross-host diff (`sac fleet sync-code`).

Two pure functions, mirroring :mod:`spec_manifest`:

  * :func:`build_checkout_manifest` records, for one host, the git HEAD
    (+ dirty flag) of ``~/.dotfiles`` and every ``~/proj/scitex-*``
    checkout, plus the installed ``sac`` version string. Used locally and
    on each peer (invoked via ssh as ``sac fleet sync-code --collect``).

  * :func:`diff_checkout_manifests` collates one manifest per host and
    emits a structured drift report. NEVER auto-merges. Same contract as
    spec sync: "report every disagreement; let the operator pick the
    authoritative copy."

Why a separate manifest from ``spec_manifest``:

  * Different subject (code checkouts, not agent specs) and different
    record shape (git SHAs + dirty flags, not file hashes).
  * The diff semantics differ: a dirty tree or an ahead-of-origin HEAD
    is a *blocker description*, not just a content mismatch — the report
    must say WHY a host can't fast-forward, not merely THAT it differs.

Manifest shape (locked in tests/_state/test_checkout_manifest.py)::

    {
      "host": "<canonical name>",
      "dotfiles": {"sha": "<40-hex>" | None, "present": bool},
      "sac_version": "<version string>" | None,
      "checkouts": {
        "<pkg>": {
          "present": True,
          "branch": "<branch>" | None,
          "sha": "<40-hex>",
          "dirty": bool,     # tracked-dirty (untracked files ignored)
          "ahead": int,      # commits ahead of origin/<branch>
          "behind": int,     # commits behind origin/<branch>
        },
      },
      "errors": []
    }

Diff shape (locked in tests)::

    {
      "ok": bool,
      "fleet": [host, ...],
      "dotfiles": {"ok": bool, "per_host": {host: sha}, "diverged_hosts": [...]},
      "sac_version": {"ok": bool, ...},
      "packages": {
        "<pkg>": {
          "ok": bool,
          "conflicts": [
            {
              "kind": "sha_mismatch" | "missing_on_host"
                    | "dirty_on_host" | "ahead_of_origin"
                    | "branch_mismatch" | "behind_origin",
              "per_host": {host: {...}},
              "diverged_hosts": [...]
            }
          ]
        }
      }
    }

Kind precedence per package: ``missing_on_host`` > ``branch_mismatch`` >
``dirty_on_host`` / ``ahead_of_origin`` (both block FF — reported
together, not one hiding the other) > ``sha_mismatch`` > ``behind_origin``
(a plain lag, fixable by pull).
"""

from __future__ import annotations

from typing import Any

__all__ = ["build_checkout_manifest", "diff_checkout_manifests"]


# ---------------------------------------------------------------------------
# Manifest build (per host).
#
# NOTE: the per-host record collection (git plumbing per checkout) lives in
# ``cli_pkg/_fleet_sync_code_collect.py`` because it shells out to git.
# This module stays pure: it takes already-collected per-checkout records
# and assembles the manifest envelope.
# ---------------------------------------------------------------------------


def build_checkout_manifest(
    *,
    host: str,
    dotfiles_sha: str | None,
    sac_version: str | None,
    checkouts: dict[str, dict[str, Any]],
    errors: list[str] | None = None,
) -> dict[str, Any]:
    """Assemble one host's checkout manifest from collected records.

    ``checkouts`` maps package name -> record with keys ``branch``,
    ``sha``, ``dirty``, ``ahead``, ``behind``. Records are validated
    shallowly (required keys present) — deep git semantics belong to the
    collector, not the envelope.
    """
    required = {"branch", "sha", "dirty", "ahead", "behind"}
    clean: dict[str, dict[str, Any]] = {}
    errs = list(errors) if errors else []
    for name in sorted(checkouts):
        rec = checkouts[name]
        missing = required - set(rec.keys())
        if missing:
            errs.append(f"checkout {name!r} missing keys: {sorted(missing)}")
            continue
        clean[name] = {
            "present": True,
            "branch": rec["branch"],
            "sha": rec["sha"],
            "dirty": bool(rec["dirty"]),
            "ahead": int(rec["ahead"]),
            "behind": int(rec["behind"]),
        }
    return {
        "host": host,
        "dotfiles": {"present": dotfiles_sha is not None, "sha": dotfiles_sha},
        "sac_version": sac_version,
        "checkouts": clean,
        "errors": errs,
    }


# ---------------------------------------------------------------------------
# Diff (cross-host).
# ---------------------------------------------------------------------------


def _minority_hosts(per_host_value: dict[str, Any]) -> list[str]:
    """Hosts in the smallest equivalence class (informational only).

    Same no-winner semantics as spec_manifest: on a tie every host is
    \"diverged\". The operator picks the authoritative copy; sac never
    does.
    """

    def _key(v: Any) -> Any:
        if isinstance(v, dict):
            return tuple(sorted((k, _key(vv)) for k, vv in v.items()))
        if isinstance(v, list):
            return tuple(v)
        return v

    from collections import Counter

    counts = Counter(_key(v) for v in per_host_value.values())
    if not counts:
        return []
    max_count = max(counts.values())
    smallest = min(counts.values())
    if smallest == max_count:
        return sorted(per_host_value.keys())
    minority_keys = {k for k, c in counts.items() if c < max_count}
    return sorted(h for h, v in per_host_value.items() if _key(v) in minority_keys)


def _diff_sha_field(
    *,
    label: str,
    per_host: dict[str, dict[str, Any] | None],
    sha_of: Any,
) -> list[dict[str, Any]]:
    """Shared sha/presence diff used for dotfiles + sac_version + pkg SHAs."""
    conflicts: list[dict[str, Any]] = []
    present = {h: r for h, r in per_host.items() if r is not None}
    absent = [h for h, r in per_host.items() if r is None]
    if absent and present:
        conflicts.append(
            {
                "file": label,
                "kind": "missing_on_host",
                "per_host": {
                    h: ({"present": True, **r} if r is not None else {"present": False})
                    for h, r in per_host.items()
                },
                "diverged_hosts": sorted(absent) if len(absent) <= len(present) else sorted(present),
            }
        )
        return conflicts
    if not present:
        return conflicts
    shas = {h: sha_of(r) for h, r in present.items()}
    if len(set(shas.values())) > 1:
        conflicts.append(
            {
                "file": label,
                "kind": "sha_mismatch",
                "per_host": {h: {"present": True, **r} for h, r in present.items()},
                "diverged_hosts": _minority_hosts(shas),
            }
        )
    return conflicts


def diff_checkout_manifests(
    per_host_manifest: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    """Cross-host checkout diff. Returns the drift envelope; never mutates."""
    fleet = list(per_host_manifest.keys())
    if len(fleet) <= 1:
        return {"ok": True, "fleet": fleet, "dotfiles": {"ok": True}, "packages": {}}

    overall_ok = True

    # ---- dotfiles HEAD ----
    dot_per_host = {
        h: (
            {"sha": m["dotfiles"]["sha"]}
            if m["dotfiles"].get("present")
            else None
        )
        for h, m in per_host_manifest.items()
    }
    dot_conflicts = _diff_sha_field(
        label="dotfiles", per_host=dot_per_host, sha_of=lambda r: r["sha"]
    )
    dotfiles_out: dict[str, Any] = {
        "ok": not dot_conflicts,
        "per_host": {h: m["dotfiles"]["sha"] for h, m in per_host_manifest.items()},
        "diverged_hosts": dot_conflicts[0]["diverged_hosts"] if dot_conflicts else [],
    }
    if dot_conflicts:
        overall_ok = False

    # ---- sac version ----
    ver_per_host: dict[str, dict[str, Any] | None] = {
        h: ({"version": m["sac_version"]} if m.get("sac_version") else None)
        for h, m in per_host_manifest.items()
    }
    ver_conflicts = _diff_sha_field(
        label="sac_version", per_host=ver_per_host, sha_of=lambda r: r["version"]
    )
    sac_out: dict[str, Any] = {
        "ok": not ver_conflicts,
        "per_host": {h: m.get("sac_version") for h, m in per_host_manifest.items()},
        "diverged_hosts": ver_conflicts[0]["diverged_hosts"] if ver_conflicts else [],
    }
    if ver_conflicts:
        overall_ok = False

    # ---- per-package ----
    all_pkgs: set[str] = set()
    for m in per_host_manifest.values():
        all_pkgs.update(m["checkouts"].keys())

    packages_out: dict[str, dict[str, Any]] = {}
    for pkg in sorted(all_pkgs):
        per_host_rec: dict[str, dict[str, Any] | None] = {
            h: per_host_manifest[h]["checkouts"].get(pkg) for h in fleet
        }
        conflicts: list[dict[str, Any]] = []

        present_hosts = [h for h, r in per_host_rec.items() if r is not None]
        absent_hosts = [h for h, r in per_host_rec.items() if r is None]
        if absent_hosts and present_hosts:
            conflicts.append(
                {
                    "kind": "missing_on_host",
                    "per_host": {
                        h: ({"present": True, **r} if r is not None else {"present": False})
                        for h, r in per_host_rec.items()
                    },
                    "diverged_hosts": (
                        sorted(absent_hosts)
                        if len(absent_hosts) <= len(present_hosts)
                        else sorted(present_hosts)
                    ),
                }
            )
        elif len(present_hosts) >= 2:
            recs = {h: per_host_rec[h] for h in present_hosts}  # type: ignore[index]

            branches = {h: r["branch"] for h, r in recs.items()}  # type: ignore[index]
            if len(set(branches.values())) > 1:
                conflicts.append(
                    {
                        "kind": "branch_mismatch",
                        "per_host": {h: {"present": True, **r} for h, r in recs.items()},  # type: ignore[misc]
                        "diverged_hosts": _minority_hosts(branches),
                    }
                )

            dirty_hosts = [h for h, r in recs.items() if r["dirty"]]  # type: ignore[index]
            if dirty_hosts:
                conflicts.append(
                    {
                        "kind": "dirty_on_host",
                        "per_host": {
                            h: {"present": True, "dirty": r["dirty"]}  # type: ignore[index]
                            for h, r in recs.items()
                        },
                        "diverged_hosts": (
                            sorted(dirty_hosts)
                            if len(dirty_hosts) <= len(recs) / 2
                            else sorted(set(recs) - set(dirty_hosts))
                        ),
                    }
                )

            ahead_hosts = [h for h, r in recs.items() if r["ahead"] > 0]  # type: ignore[index]
            if ahead_hosts:
                conflicts.append(
                    {
                        "kind": "ahead_of_origin",
                        "per_host": {
                            h: {"present": True, "ahead": r["ahead"]}  # type: ignore[index]
                            for h, r in recs.items()
                        },
                        "diverged_hosts": sorted(ahead_hosts),
                    }
                )

            shas = {h: r["sha"] for h, r in recs.items()}  # type: ignore[index]
            if len(set(shas.values())) > 1:
                conflicts.append(
                    {
                        "kind": "sha_mismatch",
                        "per_host": {h: {"present": True, **r} for h, r in recs.items()},  # type: ignore[misc]
                        "diverged_hosts": _minority_hosts(shas),
                    }
                )

            behind_hosts = [h for h, r in recs.items() if r["behind"] > 0]  # type: ignore[index]
            if behind_hosts:
                conflicts.append(
                    {
                        "kind": "behind_origin",
                        "per_host": {
                            h: {"present": True, "behind": r["behind"]}  # type: ignore[index]
                            for h, r in recs.items()
                        },
                        "diverged_hosts": sorted(behind_hosts),
                    }
                )

        pkg_ok = not conflicts
        if not pkg_ok:
            overall_ok = False
        packages_out[pkg] = {"ok": pkg_ok, "conflicts": conflicts}

    return {
        "ok": overall_ok,
        "fleet": fleet,
        "dotfiles": dotfiles_out,
        "sac_version": sac_out,
        "packages": packages_out,
    }


# EOF
