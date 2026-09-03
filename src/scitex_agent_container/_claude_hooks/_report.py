"""The read-only answer to "what does this container actually enforce".

Returns the CROSS-PACKAGE STANDARD health shape — the same one
``scitex_cards._health.health`` emits, so the fleet has ONE place and ONE shape
to look at rather than a second, differently-shaped health surface that makes
the answer harder to find::

    {
      "package": "scitex-agent-container",
      "ok": <bool: true iff NO check FAILED>,
      "checks": [ {"name", "ok", "detail", "hint"}, ... ],
      "summary": <str>,
    }

Contract, inherited verbatim from that doctor:

* a check's ``ok`` is THREE-VALUED — ``True`` / ``False`` / ``None`` (UNKNOWN,
  "I could not measure"). ``None`` never fails the run and is never a pass; it
  is NAMED in ``summary`` so it cannot read as a silent green;
* every failing AND every unknown check carries an ACTIONABLE ``hint``;
* :func:`hooks_health` NEVER raises — a check that errors internally becomes
  ``ok=false`` with the error in its ``hint``.

The four checks
---------------
``measurement_site``      whose hooks did we just count? (UNKNOWN off-agent)
``hooks_root_readable``   is ``$HOME/.claude/hooks`` there and listable?
``required_hooks_declared`` does the spec state a floor at all? (UNKNOWN when
                          it does not — undeclared is not a pass, and it is not
                          a failure either; nothing is being enforced)
``required_hooks_present``  is every declared hook armed? (the refusal check)
"""

from __future__ import annotations

from typing import Any, Callable

from ._floor import (
    declared_floor,
    evaluate_floor,
    expected_source_hint,
    flatten_floor,
    measurement_site,
    unknown_event_dirs,
)
from ._inventory import HOOK_EVENT_DIRS, inventory_hooks

PACKAGE = "scitex-agent-container"


def _run_check(name: str, fn: Callable[[], "dict[str, Any]"]) -> "dict[str, Any]":
    """Run one check into the standard record; preserve UNKNOWN; never raise."""
    try:
        res = fn()
        raw = res.get("ok")
        ok = None if raw is None else bool(raw)
        detail = str(res.get("detail", ""))
        hint = res.get("hint")
    except Exception as exc:  # noqa: BLE001 — health must NEVER raise out
        ok = False
        detail = f"{name} check errored: {type(exc).__name__}: {exc}"
        hint = f"internal error in the {name} check: {exc}"
    if ok is not True and not hint:
        verdict = "could not be evaluated" if ok is None else "failed"
        hint = f"{name} {verdict}: {detail}"
    return {"name": name, "ok": ok, "detail": detail, "hint": hint}


def _check_root(inv) -> "dict[str, Any]":
    if inv.root_error is not None:
        return {
            "ok": None,
            "detail": f"{inv.root} unreadable: {inv.root_error}",
            "hint": (
                f"no hook inventory could be taken at {inv.root}; if this is a "
                "bare-host shell that is expected — run it inside the agent's "
                "container instead"
            ),
        }
    counts = ", ".join(f"{k}={v}" for k, v in sorted(inv.counts.items())) or "none"
    detail = f"{inv.root}: total={inv.total} ({counts})"
    if inv.unreadable_dirs:
        return {
            "ok": None,
            "detail": f"{detail}; unreadable: {sorted(inv.unreadable_dirs)}",
            "hint": (
                "some hook directories could not be listed, so any hook they "
                f"would arm is UNKNOWN, not absent: {inv.unreadable_dirs}"
            ),
        }
    return {"ok": True, "detail": detail, "hint": None}


def _check_declared(config) -> "dict[str, Any]":
    floor = declared_floor(config)
    if floor is None:
        return {
            "ok": None,
            "detail": "spec declares no 'required_claude_hooks' — nothing is enforced",
            "hint": (
                "declare the hooks this agent must not run without, e.g.\n"
                "  required_claude_hooks:\n"
                "    pre-tool-use: [enforce_git_dash_C.sh]\n"
                "until then its hook set is whatever the cascade happens to "
                "deliver, which measured 15-71 across the fleet on 2026-08-10"
            ),
        }
    bogus = unknown_event_dirs(floor)
    required = flatten_floor(floor)
    if bogus:
        return {
            "ok": False,
            "detail": f"declares {len(required)} hook(s); unknown event dirs: {bogus}",
            "hint": (
                f"{bogus} are not Claude Code hook directories, so nothing can "
                "ever satisfy them. Valid names: "
                f"{sorted(HOOK_EVENT_DIRS)}"
            ),
        }
    return {
        "ok": True,
        "detail": f"declares {len(required)} required hook(s) across {len(floor)} dir(s)",
        "hint": None,
    }


def _check_present(config, verdict) -> "dict[str, Any]":
    if not verdict.declared:
        return {
            "ok": None,
            "detail": "no floor declared — nothing to satisfy",
            "hint": "declare spec.required_claude_hooks to make this measurable",
        }
    if verdict.satisfied is True:
        return {
            "ok": True,
            "detail": f"all {verdict.required_total} declared hook(s) armed",
            "hint": None,
        }
    if verdict.satisfied is None:
        named = ", ".join(f"{d}/{n}" for d, n in verdict.unknown) or "all of them"
        return {
            "ok": None,
            "detail": f"could not determine whether these are armed: {named}",
            "hint": (
                "this is UNKNOWN, not satisfied — re-run where the agent's own "
                "$HOME is visible (inside its container) before concluding "
                "anything about it"
            ),
        }
    named = ", ".join(f"{d}/{n}" for d, n in verdict.missing)
    hints = "; ".join(
        expected_source_hint(config, d, n) for d, n in verdict.missing[:3]
    )
    return {
        "ok": False,
        "detail": f"{len(verdict.missing)} declared hook(s) NOT armed: {named}",
        "hint": hints,
    }


def hooks_health(
    config,
    *,
    agent_name: "str | None" = None,
    home: "str | None" = None,
) -> "dict[str, Any]":
    """The four hook checks, in the cross-package standard report shape.

    ``config`` may be a real :class:`~..config.AgentConfig` or ``None`` (the
    spec was not resolvable here) — a ``None`` config simply reads as an
    undeclared floor, which is UNKNOWN rather than a fabricated pass.
    """
    site = measurement_site(agent_name)
    inv = inventory_hooks(home=home)
    verdict = evaluate_floor(config, inventory=inv, site_ok=site["ok"])

    checks = [
        _run_check("measurement_site", lambda: site),
        _run_check("hooks_root_readable", lambda: _check_root(inv)),
        _run_check("required_hooks_declared", lambda: _check_declared(config)),
        _run_check("required_hooks_present", lambda: _check_present(config, verdict)),
    ]
    failing = [c["name"] for c in checks if c["ok"] is False]
    unknown = [c["name"] for c in checks if c["ok"] is None]
    n_ok = sum(1 for c in checks if c["ok"] is True)
    summary = f"{n_ok}/{len(checks)} checks passed"
    if failing:
        summary += "; failing: " + ", ".join(failing)
    if unknown:
        summary += "; unknown: " + ", ".join(unknown)
    return {
        "package": PACKAGE,
        "ok": not failing,
        "checks": checks,
        "summary": summary,
        # Beyond the standard four keys: the raw inventory + verdict, so a
        # caller asking "what does this container enforce" gets the LIST, not
        # only a verdict about it. Extra keys are additive — every consumer of
        # the standard shape reads the four above and ignores the rest.
        "inventory": inv.to_dict(),
        "floor": verdict.to_dict(),
    }


def render_hooks_text(report: "dict[str, Any]") -> str:
    """Human rendering: the inventory, then one line per check."""
    inv = report["inventory"]
    lines = ["HOOKS", f"  root       {inv['root']}"]
    if inv["root_error"]:
        lines.append(f"  error      {inv['root_error']}")
    for name, count in sorted(inv["counts"].items()):
        names = ", ".join(inv["dirs"].get(name, [])[:4])
        tail = ", ..." if count > 4 else ""
        lines.append(f"  {name:<24} {count:>3}  {names}{tail}")
    if inv["missing_dirs"]:
        lines.append(f"  (absent dirs: {', '.join(inv['missing_dirs'])})")
    lines.append("CHECKS")
    mark = {True: "[ OK ]", False: "[FAIL]", None: "[????]"}
    for check in report["checks"]:
        lines.append(f"  {mark[check['ok']]} {check['name']}: {check['detail']}")
        if check["ok"] is not True and check["hint"]:
            for row in str(check["hint"]).splitlines():
                lines.append(f"         hint: {row}" if row else "")
    lines.append(f"SUMMARY  {report['summary']}")
    return "\n".join(lines)


__all__ = ["PACKAGE", "hooks_health", "render_hooks_text"]
