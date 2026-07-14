#!/usr/bin/env python3
# File: src/scitex_agent_container/cli_pkg/provenance_cmds.py

"""``sac provenance`` — prove which code is loaded, and whether it lies.

The heavy half of the version story. ``sac --version`` stays terse and
~0.5 ms because it is typed constantly and shelled by scripts; the checks
that cost real time (hashing the loaded tree is ~35 ms, and worse on a
SIF's compressed squashfs) live here, where you only pay for them when you
are actually asking the question.

``--strict`` exits 1 when anything is off, so a deploy step or CI gate can
refuse to proceed against a shadowed, duplicated, patched, or fossilised
install instead of discovering it hours later.
"""

from __future__ import annotations

import json as _json

import click

CONTEXT_SETTINGS = {"help_option_names": ["-h", "--help"]}

_LABELS = (
    ("version", "version (declared)"),
    ("commit", "commit"),
    ("commit_source", "commit read from"),
    ("install", "install kind"),
    ("origin", "loaded from"),
    ("repo_root", "repo root"),
    ("built_at", "built at"),
    ("code_hash", "code hash (at build)"),
    ("live_code_hash", "code hash (on disk)"),
    ("python", "python"),
)


def _render(report: dict) -> str:
    lines = []
    for key, label in _LABELS:
        value = report.get(key)
        if value:
            lines.append(f"  {label:22s} {value}")

    dists = report.get("dist_infos") or []
    lines.append(f"  {'distributions':22s} {len(dists)}")
    for path in dists:
        lines.append(f"  {'':22s} - {path}")

    anomalies = report.get("anomalies") or []
    if not anomalies:
        lines.append("")
        lines.append("  OK — the loaded code is the installed code.")
        return "\n".join(lines)

    lines.append("")
    for item in anomalies:
        lines.append(f"  PROBLEM [{item['code']}]")
        lines.append(f"    {item['detail']}")
    return "\n".join(lines)


@click.command("provenance", context_settings=CONTEXT_SETTINGS)
@click.option("--json", "as_json", is_flag=True, default=False, help="Output as JSON.")
@click.option(
    "--strict",
    is_flag=True,
    default=False,
    help="Exit 1 if the install is shadowed, duplicated, patched, or fossilised.",
)
def provenance(as_json: bool, strict: bool) -> None:
    """Report the identity of the LOADED code and audit the install.

    Answers the question ``--version`` cannot: not "what does this claim
    to be" but "which code is actually running, and is it really the code
    that is installed?".

    \b
    Detects:
      shadowed         imports resolve somewhere other than the installed dist
                       (a bare pytest importing site-packages, not your worktree)
      duplicate-dist   two .dist-info dirs — one is a fossil
      patched          site-packages .py bytes no longer match the build
      version-mismatch .dist-info version != the version baked into the code

    \b
    Examples:
      $ sac provenance                  # human-readable
      $ sac provenance --json           # machine-readable
      $ sac provenance --strict         # exit 1 on any anomaly (CI / deploy gate)
    """
    from .._provenance import audit

    report = audit()
    if as_json:
        click.echo(_json.dumps(report, indent=2))
    else:
        click.echo(_render(report))

    if strict and not report["ok"]:
        raise SystemExit(1)


__all__ = ["provenance"]

# EOF
