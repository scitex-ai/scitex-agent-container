"""The DECLARED FLOOR: which hooks a spec says must be armed, and whether they are.

A count is not a guarantee
--------------------------
Measured 2026-08-10, union of both home layers, the fleet spans **15 to 71**
pre-tool-use hooks. compute-04 has two clean tiers (70 for twelve agents, 39
for four); the laptop spans ten distinct values. Nothing declared which number
was correct, and nothing detected the difference — so a floor expressed as a
COUNT would have been satisfiable by 71 of the wrong hooks. The floor is
therefore a set of NAMES, per event directory::

    spec:
      required_claude_hooks:
        pre-tool-use:
          - enforce_git_dash_C.sh
          - force_background_bash.sh
        post-tool-use:
          - log_post_tool_use.sh

Three outcomes, never two
-------------------------
:class:`FloorVerdict` is three-valued, following ``scitex-cards``' ``health``
doctor (its ``delivery_confirmed`` check reports UNKNOWN rather than pass or
fail when no receipt was ever recorded). "Satisfied", "not satisfied" and "I
could not determine whether it is satisfied" are three different answers, and
the two wrong conclusions published on 2026-08-10 both came from a check that
could not say the third one. Concretely:

* every required hook found              -> ``satisfied=True``
* some required hook definitely absent   -> ``satisfied=False`` (+ ``missing``)
* the hooks root / an event dir could not be read, or we are not measuring
  this agent's home at all                -> ``satisfied=None`` (+ ``unknown``)

The MEASUREMENT SITE is part of the verdict, not context around it. Run on the
bare host, ``$HOME/.claude/hooks`` is the OPERATOR's — a real directory, fully
readable, and the wrong one. A check that reads it and answers True/False is
the 67-vs-71 undercount with a new name, so :func:`measurement_site` reports
UNKNOWN whenever it cannot show that this process is the agent it was asked
about.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field

from ._inventory import HOOK_EVENT_DIRS, HookInventory, inventory_hooks

#: One required hook: ``(event dir, script name)``.
RequiredHook = "tuple[str, str]"


@dataclass(frozen=True)
class FloorVerdict:
    """Whether the declared floor is met, and — when not — exactly which hooks.

    ``satisfied`` is ``True`` / ``False`` / ``None`` (unknown); see the module
    docstring. ``declared`` is ``False`` when the spec declares no floor, which
    is NOT a finding: most specs are undeclared today and must keep booting.
    """

    declared: bool
    satisfied: "bool | None"
    inventory: HookInventory
    missing: "list[tuple[str, str]]" = field(default_factory=list)
    unknown: "list[tuple[str, str]]" = field(default_factory=list)
    required_total: int = 0

    @property
    def is_refusal(self) -> bool:
        """A declared floor that is DEFINITELY unmet — the only refusing state."""
        return self.declared and self.satisfied is False

    def to_dict(self) -> dict:
        return {
            "declared": self.declared,
            "satisfied": self.satisfied,
            "required_total": self.required_total,
            "missing": [f"{d}/{n}" for d, n in self.missing],
            "unknown": [f"{d}/{n}" for d, n in self.unknown],
        }


def declared_floor(config) -> "dict[str, list[str]] | None":
    """``spec.required_claude_hooks`` off a config, or ``None`` when undeclared.

    Read via ``getattr`` so a config object built before this field existed
    (or a lightweight stand-in) degrades to "undeclared" rather than raising —
    same defensive read ``_layers_preflight`` uses for ``to_home_layers``.
    """
    return getattr(config, "required_claude_hooks", None)


def flatten_floor(floor: "dict[str, list[str]] | None") -> "list[tuple[str, str]]":
    """``{event: [names]}`` -> a sorted ``[(event, name)]`` list."""
    if not floor:
        return []
    out: "list[tuple[str, str]]" = []
    for event_dir in sorted(floor):
        for name in floor[event_dir]:
            out.append((event_dir, name))
    return sorted(set(out))


def measurement_site(agent_name: "str | None" = None) -> dict:
    """Are we measuring the hooks of the agent we were asked about?

    Returns a standard check record. ``ok`` is:

    * ``True``  — this process IS the agent (``$SAC_NAME`` matches, or no agent
      was named so "myself" is the question), so ``$HOME`` is its home;
    * ``None``  — UNKNOWN: no ``$SAC_NAME`` (a bare-host shell), or it names a
      DIFFERENT agent. Either way ``$HOME/.claude/hooks`` is somebody else's,
      and the honest report is that we could not measure the subject — not a
      confident verdict about the wrong directory.

    There is deliberately no ``False``: "I read the wrong home" is never a
    finding ABOUT the agent, and reporting it as a failure would put a red mark
    on an agent that may be perfectly configured.
    """
    self_name = (os.environ.get("SAC_NAME") or "").strip()
    in_container = bool(
        os.environ.get("APPTAINER_CONTAINER") or os.environ.get("SINGULARITY_CONTAINER")
    )
    where = f"$HOME={os.environ.get('HOME', '?')} SAC_NAME={self_name or '<unset>'}"
    hint = (
        "run this INSIDE the agent's container (it is on the agent's own boot "
        "path, and `sac agents send <agent> 'sac agents hooks'` reaches it) — a "
        "host-side read measures the operator's ~/.claude, which on 2026-08-10 "
        "reported 67 pre-tool-use hooks where the same agent's container had 71"
    )
    if not self_name:
        return {
            "ok": None,
            "detail": f"not running as a sac agent ({where}, container={in_container})",
            "hint": hint,
        }
    if agent_name and agent_name != self_name:
        return {
            "ok": None,
            "detail": (
                f"asked about agent {agent_name!r} but this process is "
                f"{self_name!r} ({where})"
            ),
            "hint": hint,
        }
    return {
        "ok": True,
        "detail": f"measuring this agent's own home ({where}, container={in_container})",
        "hint": None,
    }


def evaluate_floor(
    config,
    *,
    home: "str | os.PathLike | None" = None,
    inventory: "HookInventory | None" = None,
    site_ok: "bool | None" = True,
) -> FloorVerdict:
    """Compare the spec's declared floor against the hooks actually visible.

    ``site_ok`` is :func:`measurement_site`'s verdict. When it is not ``True``
    the floor result is forced to UNKNOWN: we did read A directory, but not the
    one the question was about, and a confident answer from the wrong home is
    exactly the failure this whole check exists to end.
    """
    floor = declared_floor(config)
    inv = inventory if inventory is not None else inventory_hooks(home=home)
    required = flatten_floor(floor)
    if floor is None:
        return FloorVerdict(declared=False, satisfied=None, inventory=inv)

    if site_ok is not True:
        return FloorVerdict(
            declared=True,
            satisfied=None,
            inventory=inv,
            unknown=required,
            required_total=len(required),
        )

    missing: "list[tuple[str, str]]" = []
    unknown: "list[tuple[str, str]]" = []
    for event_dir, name in required:
        present = inv.has(event_dir, name)
        if present is None:
            unknown.append((event_dir, name))
        elif not present:
            missing.append((event_dir, name))
    if missing:
        satisfied: "bool | None" = False
    elif unknown:
        satisfied = None
    else:
        satisfied = True
    return FloorVerdict(
        declared=True,
        satisfied=satisfied,
        inventory=inv,
        missing=missing,
        unknown=unknown,
        required_total=len(required),
    )


def expected_source_hint(config, event_dir: str, name: str) -> str:
    """WHERE a missing hook should have come from — the actionable half.

    Every failing check in the cross-package health shape carries a hint that
    names the next step, so "missing enforce_git_dash_C.sh" is not enough: the
    reader needs the path to create. Hooks reach ``$HOME/.claude/hooks`` through
    the ``to_home`` cascade, so the answer is that agent's cascade layer dirs.
    Best-effort by design — a resolver hiccup must degrade to generic advice,
    never replace a precise complaint with a stack trace about something else.
    """
    rel = f".claude/hooks/{event_dir}/{name}"
    # stx-allow: fallback (reason: this only enriches an error message; a
    # resolver failure must not mask the missing-hook complaint itself)
    try:
        from ..runtimes._to_home_resolve import settings_layer_dirs

        dirs = [str(path) for _n, path in settings_layer_dirs(config) if path]
    except Exception as exc:
        return (
            f"add {rel} to one of this agent's to_home cascade layers "
            f"(layer dirs unresolvable here: {type(exc).__name__}: {exc})"
        )
    if not dirs:
        return f"add {rel} to this agent's to_home/ dir, then restart it"
    candidates = " or ".join(f"{d}/{rel}" for d in dirs)
    return f"expected at {candidates} — add it there, then restart the agent"


def unknown_event_dirs(floor: "dict[str, list[str]] | None") -> "list[str]":
    """Declared event dirs that are not real Claude Code hook dirs.

    A misspelt ``pre_tool_use`` matches no directory, so the floor it declares
    can never be satisfied AND never be meaningfully missing — the same silent
    no-op ``UnknownToHomeLayer`` exists to refuse. Surfaced separately so the
    message can say "this is a typo" instead of "this hook is missing".
    """
    if not floor:
        return []
    return sorted(name for name in floor if name not in HOOK_EVENT_DIRS)


__all__ = [
    "FloorVerdict",
    "RequiredHook",
    "declared_floor",
    "evaluate_floor",
    "expected_source_hint",
    "flatten_floor",
    "measurement_site",
    "unknown_event_dirs",
]
