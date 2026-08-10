"""The REFUSAL: a declared hook floor that is not met stops the agent booting.

Operator ruling 2026-08-10, the same one PR #949 implemented for
``to_home_layers`` and spec-source drift: 「ワーニングだと誰も直さない」— nobody
fixes warnings — so a spec whose guarantee is not met REFUSES to start, with an
explicit NAMED override rather than a blanket ``--force``. This module mirrors
that shape deliberately, down to the ERROR-level bypass notice, so an operator
who has read one refusal can read this one.

Where this runs, and why it is not in ``agent_start``
-----------------------------------------------------
``agent_start`` runs on the BARE HOST. At that moment the agent's ``$HOME`` is
two unmerged directories on disk, and reading either one is the undercount this
whole feature exists to end (67 vs 71 on 2026-08-10). A gate placed there would
have measured the operator's own ``~/.claude`` and answered confidently about
the wrong machine.

So the gate runs on the CONTAINER's own startup path instead:
``runtimes._apptainer_inner_argv.build_inner_argv`` emits one unconditional
in-container step — ``sac agents hooks <name>`` — BEFORE ``exec``ing the
runner, but ONLY for a spec that declares a floor. An unmet floor exits
non-zero, bash aborts before ``exec``, the container dies, ``runtime.start``
returns False, and the existing ``_start_failure_diag.raise_start_failure``
path carries the refusal text back to the operator. A spec with no declaration
gets no step at all — no refusal, and no warning to scroll past.

Three outcomes, and only ONE of them refuses
--------------------------------------------
* floor undeclared        -> proceed silently
* floor satisfied         -> proceed silently
* floor UNKNOWN           -> proceed, logged at ERROR naming what could not be
  measured. Refusing on "I could not check" would ground the fleet on an
  unreadable mount, exactly as ``_drift._local`` declines to refuse on
  NOT_A_REPO / UNREACHABLE. Crucially it is not a pass either: the report keeps
  ``ok=None`` and the summary names it.
* floor DEFINITELY unmet  -> :class:`MissingRequiredHooks`, naming each missing
  hook and where it should have come from.
"""

from __future__ import annotations

import logging

from ._errors import MissingRequiredHooks

logger = logging.getLogger(__name__)

#: The named override. One flag per condition, never a blanket ``--force``:
#: a generic override skips every check at once, leaves no record of WHICH
#: check was bypassed, and gets reused by the next person for an unrelated
#: failure. ``git --no-verify`` is the cautionary example, banned by hook here.
ALLOW_FLAG = "--allow-missing-hooks"
ALLOW_ENV = "SAC_ALLOW_MISSING_HOOKS"

_TRUTHY = ("1", "true", "yes", "on")

#: The check inside the standard report that carries the refusal verdict.
FLOOR_CHECK = "required_hooks_present"


def _env_flag(name: str, default: bool) -> bool:
    """Read a sac env flag (either prefix), falling back to ``default``."""
    from .._env import getenv as _sac_env

    raw = (_sac_env(name.removeprefix("SAC_"), "") or "").strip().lower()
    if not raw:
        return default
    return raw in _TRUTHY


def _resolve_allow_missing(allow: "bool | None") -> bool:
    """Effective override state: explicit arg wins, else the env, else off.

    ``None`` means "no instruction" and MUST NOT be read as ``False`` meaning
    "be strict" or as ``True`` meaning "be lenient". PR #949 shipped exactly
    that confusion at the click seam — a flag's natural ``False`` reached a
    resolver that read an explicit ``False`` as a demand for leniency — so the
    CLI here converts an absent flag to ``None``, never ``False``.
    """
    if allow is not None:
        return allow
    return _env_flag(ALLOW_ENV, False)


def _named_check(report: dict, name: str) -> dict:
    for check in report.get("checks", ()):
        if check.get("name") == name:
            return check
    return {"name": name, "ok": None, "detail": "check absent", "hint": None}


def floor_check(report: dict) -> dict:
    """The ``required_hooks_present`` record out of a standard report."""
    return _named_check(report, FLOOR_CHECK)


def missing_hooks_lines(report: dict, *, bypassed: bool = False) -> "list[str]":
    """The banner for an unmet floor — the refusal, or the bypass notice.

    ERROR in both forms. A bypass is not a milder event than the refusal it
    replaced: it is the same defect, deliberately admitted.
    """
    check = floor_check(report)
    floor = report.get("floor") or {}
    inventory = report.get("inventory") or {}
    missing = floor.get("missing") or []
    bar = "!" * 72
    verdict = (
        f"sac-hooks BYPASSED: starting anyway because {ALLOW_FLAG} / "
        f"{ALLOW_ENV} was given."
        if bypassed
        else "sac-hooks ERROR: refusing to start."
    )
    lines = [
        bar,
        verdict,
        f"  the spec declares {floor.get('required_total', 0)} required hook(s); "
        f"{len(missing)} are NOT armed in this container.",
        f"  hooks root: {inventory.get('root')}  (total armed: "
        f"{inventory.get('total')})",
        "  missing:",
    ]
    lines.extend(f"    - {name}" for name in missing)
    # A misspelt event dir ALSO reads as "hook not armed", because nothing can
    # ever be armed under a directory Claude Code does not load from. Showing
    # only the missing-hook fix would send the reader off to create that bogus
    # directory, so the declaration complaint comes FIRST when it fired.
    declared = _named_check(report, "required_hooks_declared")
    if declared.get("ok") is False and declared.get("hint"):
        lines.append(f"  first:      {declared['hint']}")
    if check.get("hint"):
        lines.append(f"  fix:        {check['hint']}")
    lines.append(f"  override:   {ALLOW_FLAG}   /   {ALLOW_ENV}=1")
    lines.append(bar)
    return lines


def check_required_hooks(
    report: dict,
    *,
    allow_missing: "bool | None" = None,
) -> bool:
    """Refuse (or loudly permit) a container whose declared hook floor is unmet.

    Returns ``True`` when the agent may proceed — floor undeclared, satisfied,
    unknown, or definitely unmet BUT overridden. Raises
    :class:`MissingRequiredHooks` for the one remaining case.
    """
    check = floor_check(report)
    if check.get("ok") is True:
        return True
    if check.get("ok") is None:
        floor = report.get("floor") or {}
        if floor.get("declared"):
            # UNKNOWN is not a pass. It does not refuse — but it is said out
            # loud, because a floor nobody could measure is indistinguishable
            # from a floor nobody set unless somebody says so.
            logger.error(
                "sac-hooks UNKNOWN: a hook floor is declared but could not be "
                "measured here (%s). Hint: %s",
                check.get("detail"),
                check.get("hint"),
            )
        return True

    allow = _resolve_allow_missing(allow_missing)
    lines = missing_hooks_lines(report, bypassed=allow)
    for line in lines:
        logger.error("%s", line)
    if allow:
        return True
    raise MissingRequiredHooks("\n".join(lines))


__all__ = [
    "ALLOW_ENV",
    "ALLOW_FLAG",
    "FLOOR_CHECK",
    "MissingRequiredHooks",
    "check_required_hooks",
    "floor_check",
    "missing_hooks_lines",
]
