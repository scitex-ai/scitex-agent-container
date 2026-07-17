"""Twin spec DERIVATION — the command-time half of twin spawning.

PURE spec-doc transforms that ``sac agents twin`` (CLI) and the ``agent_twin``
MCP tool run BEFORE the spawn POST: the parent's spec verbatim
(repo/workdir/image/binds/model) with the twin's name, ``session: continue``,
lifetime, a fresh ``a2a.port``, and the IDENTITY-SPLIT env overridden. Nothing
here touches the host's runtime state — that is the sibling :mod:`._twin_seed`.

Full design: docs/adr/0019 (+ its 2026-07-17 amendment) and the
``33_twin-spawning`` skill.

IDENTITY SPLIT — safety-critical:
  * author = the TWIN — ``SCITEX_TODO_AGENT_ID = <twin>`` in its env block,
    so scitex-todo writes attribute to the twin (the operator's ask). Written
    to ``spec.apptainer.env`` (the v3 home) and additionally SCRUBBED from any
    inherited ``raw_args``, because raw_args are appended AFTER the curated
    ``--env`` and would otherwise re-assert the PARENT's identity.
  * owner = the PARENT — but scitex-todo has NO env knob for the default
    card owner (``add_task`` fails loud without an explicit ``assignee``;
    ``SCITEX_TODO_AGENT_ID`` feeds ONLY the author path — verified against
    scitex_todo._store). So owner=parent CANNOT be enforced from env; it is
    a HARD CONVENTION — the twin passes ``assignee=<parent>`` (==
    ``$SAC_TWIN_PARENT``, injected here) on EVERY card write. WHY: an
    ephemeral twin that owns cards then exits orphans them (the drift
    incident that stranded 75 cards). The boot-kick + skill state the rule.
"""

from __future__ import annotations

import copy
import logging
from typing import Any

from ._twin_identity import (
    SELF_NAME_ENV,
    TODO_AGENT_ENV,
    TWIN_PARENT_ENV,
    TwinSeedError,
    resolve_twin_name,
)

logger = logging.getLogger(__name__)

# Dropped from an inherited twin so two agents don't fight one Telegram bot's
# getUpdates long-poll slot (HTTP 409). The twin stays reachable via the a2a
# bus (``server:sac``) instead.
_TELEGRAMMER_CHANNEL = "server:claude-code-telegrammer"

__all__ = [
    "build_twin_boot_kick",
    "derive_twin_spec",
    "prepare_twin_spawn",
]


def build_twin_boot_kick(
    twin_name: str,
    parent_name: str,
    task: str | None,
) -> str:
    """First user message fed to the twin AFTER it resumes the parent's session.

    Carries the two things a freshly-forked twin must know: that it has
    inherited context and diverged, and the IDENTITY-SPLIT rule (author =
    twin, owner = parent). The ownership rule is stated HERE — not only in
    the skill — because scitex-todo cannot enforce owner=parent from env,
    so the boot-kick is the deterministic delivery of the hard rule.
    """
    lines = [
        f"You are {twin_name}, a TWIN forked from {parent_name}'s live session.",
        f"You have INHERITED {parent_name}'s conversation context up to this "
        "moment and now DIVERGE — your future turns are yours alone and are "
        f"NOT shared back to {parent_name}.",
        "",
        "IDENTITY CONTRACT (hard rule — do not deviate):",
        f"  - Your scitex-todo writes are attributed to YOU ({twin_name}); that "
        "is intended.",
        f"  - But card OWNERSHIP must stay with {parent_name}. On EVERY "
        f"add_task / reassign, pass assignee={parent_name} (also available as "
        f"$SAC_TWIN_PARENT). NEVER leave a card owned by {twin_name}: if you "
        "exit, a card you own lands in an inbox nobody drains.",
        f"  - Coordinate results back to {parent_name} via a2a / a shared card "
        f"owned by {parent_name}, not by holding state only you can see.",
    ]
    if task and task.strip():
        lines += ["", f"Your task: {task.strip()}"]
    else:
        lines += [
            "",
            "No task was supplied at spawn — confirm you have inherited context "
            f"and stand by for {parent_name}'s direction.",
        ]
    return "\n".join(lines)


def derive_twin_spec(
    parent_doc: dict[str, Any],
    *,
    twin_name: str,
    parent_name: str,
    persist: bool,
    role: str | None = None,
    task: str | None = None,
    to_home: str | None = None,
) -> dict[str, Any]:
    """Return the twin's inline spec document derived from the parent's.

    Pure — deep-copies ``parent_doc`` and overrides only what a twin must
    change, inheriting repo / workdir / image / binds / model verbatim:

      * ``spec.claude.session = "continue"`` and ``resume_id`` cleared — the
        HOST seeds the twin's session marker from the parent's CURRENT uuid
        + copies that transcript at FIRST start (:func:`seed_twin_from_parent`),
        so it inherits the freshest context; on later restarts ``continue``
        resumes the twin's OWN diverged session (not a re-fork of the parent).
      * ``spec.env`` — ``SCITEX_TODO_AGENT_ID = <twin>`` (author = twin),
        ``SAC_TWIN_PARENT = <parent>`` (owner-convention value + twin
        trigger); any inherited ``SAC_NAME`` is dropped (``listen_env_flags``
        injects it from the twin's own name).
      * ``spec.restart.policy`` — ``always`` when ``persist`` else ``never``
        (ephemeral default: a stopped twin does not come back).
      * ``spec.a2a.port = "auto"`` — a fresh sidecar port, never the
        parent's (a pinned inherited port would collide).
      * telegrammer channel dropped — two agents must not fight one bot's
        getUpdates slot; the twin stays reachable via ``server:sac``.
      * ``spec.startup_prompts`` — replaced with the twin boot-kick (the
        identity contract + optional ``task``).
      * ``metadata.labels.role`` — set when ``role`` is given.

    The twin's NAME is NOT written into the document: the host materialises
    the inline spec at ``agents/<twin_name>/spec.yaml`` and the loader
    derives the name from that directory (dir-as-SSoT).
    """
    doc = copy.deepcopy(parent_doc)
    if not isinstance(doc, dict):
        raise TwinSeedError(
            f"parent spec of {parent_name!r} did not parse to a mapping "
            f"(got {type(parent_doc).__name__!r}); cannot derive a twin."
        )
    spec = doc.setdefault("spec", {})
    if not isinstance(spec, dict):
        raise TwinSeedError(
            f"parent spec of {parent_name!r} has a non-mapping 'spec' block; "
            "cannot derive a twin."
        )

    claude = spec.setdefault("claude", {})
    if isinstance(claude, dict):
        # ``continue`` (not ``resume``): the host seeds the twin's session_id
        # marker from the parent's live uuid on FIRST boot and copies that
        # transcript in, so ``continue`` resumes it. On every SUBSEQUENT boot
        # ``continue`` resumes the twin's OWN (now-diverged) latest session —
        # a pinned ``resume``/``resume_id`` would instead re-fork from the
        # parent each restart, discarding the twin's history.
        claude["session"] = "continue"
        claude["resume_id"] = ""
        channels = claude.get("channels")
        if isinstance(channels, list):
            claude["channels"] = [
                c for c in channels if str(c).strip() != _TELEGRAMMER_CHANNEL
            ]

    # Identity env lands in ``spec.apptainer.env`` — the v3 home for engine
    # env ("promoted from top-level spec.env per §3"). Writing the top-level
    # ``spec.env`` instead makes the derived spec UNLOADABLE: the v3 validator
    # rejects it outright ("spec.env is no longer accepted at the top level"),
    # so the twin never starts, never gets SAC_TWIN_PARENT, and therefore never
    # seeds. ``spec.apptainer.env`` reaches BOTH the host-side ``config.env``
    # (via the loader's merged_env — how the twin trigger + the boot gate see
    # it) and the container itself (``--env KEY=VAL`` in build_run_argv).
    apptainer = spec.setdefault("apptainer", {})
    if not isinstance(apptainer, dict):
        raise TwinSeedError(
            f"parent spec of {parent_name!r} has a non-mapping "
            "'spec.apptainer' block; cannot derive a twin's identity env."
        )
    env = apptainer.setdefault("env", {})
    if isinstance(env, dict):
        env[TODO_AGENT_ENV] = twin_name
        env[TWIN_PARENT_ENV] = parent_name
        env.pop(SELF_NAME_ENV, None)
    _scrub_inherited_identity_raw_args(apptainer, parent_name)

    restart = spec.setdefault("restart", {})
    if isinstance(restart, dict):
        restart["policy"] = "always" if persist else "never"

    a2a = spec.setdefault("a2a", {})
    if isinstance(a2a, dict):
        a2a["port"] = "auto"

    # Reuse the PARENT's to_home tree (skills / hooks / .mcp.json) verbatim
    # via its absolute host path, so the twin has the SAME capabilities and
    # MCP wiring as the parent. Per-agent identity in those files is
    # runtime-only (``${SCITEX_TODO_AGENT_ID}`` etc.) and expands from the
    # twin's OWN container env at boot, so sharing the tree is correct — the
    # author still resolves to the twin. Left unset (parent default) when the
    # caller could not resolve the parent's to_home dir.
    if to_home:
        spec["to_home"] = to_home

    if role:
        metadata = doc.setdefault("metadata", {})
        if isinstance(metadata, dict):
            labels = metadata.setdefault("labels", {})
            if isinstance(labels, dict):
                labels["role"] = role

    spec["startup_prompts"] = [build_twin_boot_kick(twin_name, parent_name, task)]
    return doc


def _scrub_inherited_identity_raw_args(apptainer: dict, parent_name: str) -> None:
    """Drop the PARENT's identity ``--env`` pairs from inherited ``raw_args``.

    ``spec.apptainer.raw_args`` is an escape hatch appended VERBATIM AFTER
    every curated ``--env`` (see ``_apptainer_build_argv.build_run_argv``),
    and real specs routinely pin their identity there —
    ``raw_args: [--env, SCITEX_TODO_AGENT_ID=<parent>, ...]``. Deep-copied into
    a twin, that line re-injects the PARENT's identity downstream of the
    twin's own, so the twin would author as its parent: the
    two-agents-one-identity bug of 2026-07-03, arriving through the one
    channel the boot gate cannot see (the gate reads the host-side
    ``config.env``, which correctly says "twin", while the CONTAINER's env
    would say "parent" — a check that could not disagree with the failure it
    exists to catch).

    We DELETE rather than rewrite, and delete rather than reason about
    precedence: sac's curated ``spec.apptainer.env`` is the twin's single
    source of identity, so the inherited duplicate is removed entirely. That
    is correct no matter which duplicate ``--env`` the container engine would
    have honoured — an ambiguity removed beats an ambiguity bet on.

    Only the IDENTITY keys are touched; every other raw_arg (``--userns``,
    ``--overlay``, unrelated ``--env``) is inherited verbatim, as ADR-0019
    requires.
    """
    raw_args = apptainer.get("raw_args")
    if not isinstance(raw_args, list):
        return
    scrubbed: list[Any] = []
    i = 0
    n = len(raw_args)
    while i < n:
        text = str(raw_args[i])
        # ``--env KEY=VAL`` — flag and value are SEPARATE argv entries, so a
        # match drops both. Index-walked (not ``.index()``), which would find
        # the FIRST ``--env`` every time and mis-pair a spec with several.
        if (
            text == "--env"
            and i + 1 < n
            and _is_identity_env_pair(str(raw_args[i + 1]))
        ):
            i += 2
            continue
        # ``--env=KEY=VAL`` — the single-entry spelling.
        if text.startswith("--env=") and _is_identity_env_pair(text[len("--env=") :]):
            i += 1
            continue
        scrubbed.append(raw_args[i])
        i += 1
    if len(scrubbed) != n:
        logger.info(
            "twin: dropped inherited identity --env raw_args from parent %s "
            "(sac sets the twin's identity via spec.apptainer.env)",
            parent_name,
        )
    apptainer["raw_args"] = scrubbed


def _is_identity_env_pair(pair: str) -> bool:
    """True when ``pair`` is a ``KEY=VALUE`` for an identity env var."""
    key = pair.split("=", 1)[0].strip()
    return key in (TODO_AGENT_ENV, TWIN_PARENT_ENV, SELF_NAME_ENV)


def _resolve_parent_to_home(parent_path: str, parent_doc: dict) -> str | None:
    """Return the parent's to_home tree as an absolute host path, or None.

    Honours ``spec.to_home`` (default ``./to_home`` next to the spec),
    resolved relative to the parent spec dir. Returns the path only when it
    exists on disk — otherwise the twin keeps the default and materialises
    the shared baseline to_home.
    """
    from pathlib import Path

    spec = parent_doc.get("spec", {}) if isinstance(parent_doc, dict) else {}
    raw = (spec.get("to_home") if isinstance(spec, dict) else "") or "./to_home"
    p = Path(str(raw)).expanduser()
    if not p.is_absolute():
        p = Path(parent_path).parent / p
    return str(p) if p.is_dir() else None


def prepare_twin_spawn(
    parent_name: str,
    *,
    twin_name: str | None = None,
    tag: str | None = None,
    task: str | None = None,
    persist: bool = False,
    role: str | None = None,
) -> tuple[str, dict[str, Any]]:
    """Resolve the parent spec + twin name and derive the twin's inline doc.

    The shared front-half of BOTH ``sac agents twin`` and the ``agent_twin``
    MCP tool: reads the parent's on-disk spec (raw YAML), resolves the twin
    name, and returns ``(resolved_twin_name, twin_spec_doc)`` ready to POST
    via :func:`_spawn_client.request_spawn`.

    ``tag`` (preferred) derives the DETERMINISTIC id ``<parent>-forked-<tag>``;
    it is mutually exclusive with ``twin_name`` and is never bumped.

    Fail-loud (:class:`TwinSeedError`) when the parent spec is unresolvable /
    malformed, or when the resolved name is already taken. A taken name is an
    ERROR for BOTH explicit modes, and for ``tag`` that is the POINT: the same
    tag is the same twin, so a re-run must land on the existing one or say so
    — never quietly mint ``-2``. (Only the legacy no-tag/no-name default is
    auto-bumped, preserved for back-compat.)
    """
    import yaml

    from ..config import resolve_config
    from ..config._resolve import enumerate_agent_names

    try:
        parent_path = resolve_config(parent_name)
    except Exception as exc:  # noqa: BLE001 - re-raised as fail-loud TwinSeedError
        raise TwinSeedError(
            f"cannot resolve parent agent {parent_name!r}: {exc}"
        ) from exc
    with open(parent_path, encoding="utf-8") as fh:
        parent_doc = yaml.safe_load(fh)
    if not isinstance(parent_doc, dict):
        raise TwinSeedError(
            f"parent spec {parent_path!r} did not parse to a YAML mapping."
        )

    try:
        existing = enumerate_agent_names()
    except Exception:  # stx-allow: fallback (reason: name enumeration is best-effort; a 409 on spawn is the backstop)
        existing = []
    # Resolve first (validates the tag / the tag-vs-name conflict), then check
    # the clash against the RESOLVED id so a --tag re-run reports the real
    # twin id rather than the tag.
    resolved_name = resolve_twin_name(parent_name, twin_name, existing, tag=tag)
    if (twin_name or tag) and resolved_name in set(existing):
        if tag:
            raise TwinSeedError(
                f"twin {resolved_name!r} already exists — --tag {tag!r} is "
                "deterministic, so this IS that twin, not a new one. Use it "
                f"as-is (`sac agents start {resolved_name}`), retire it "
                f"(`sac agents stop/delete {resolved_name}`), or pick a "
                "different --tag. Re-running will never mint a -2 twin."
            )
        raise TwinSeedError(
            f"twin name {resolved_name!r} is already taken; pick another "
            "name, use --tag for a deterministic id, or omit both to "
            "auto-bump <parent>-twin-N."
        )

    to_home = _resolve_parent_to_home(parent_path, parent_doc)
    doc = derive_twin_spec(
        parent_doc,
        twin_name=resolved_name,
        parent_name=parent_name,
        persist=persist,
        role=role,
        task=task,
        to_home=to_home,
    )
    return resolved_name, doc


