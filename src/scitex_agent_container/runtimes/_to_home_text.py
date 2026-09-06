"""Text helpers for the to_home/ materialization pipeline.

Extracted from :mod:`_to_home` to keep that module under the 512-line
file-size cap (2026-06-15 — see ``GITIGNORED/REFACTORING.md``).
Contains the marker constants, marker-invariant validator, user-tail
extractor, and ``${VAR}`` / ``${metadata.*}`` interpolators used by
:func:`_deploy_marker_protected` and :func:`_deploy_plain_file` in
the orchestrator module.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

from ..config import AgentConfig
from ._to_home_errors import WorkspaceCLAUDEMarkerError

END_MARKER = "<!-- End of scitex-agent-container generated section -->"
START_MARKER_PREFIX = "<!-- Start of scitex-agent-container generated section"

# Per-agent IDENTITY vars that must NEVER be baked at deploy time.
#
# WHY (INCIDENT 2026-07-02, card
# sac-mcp-json-per-agent-identity-not-ambient-env-...): ``interpolate_env``
# runs host-side inside the ``sac agents start`` process, so it substitutes
# ``${VAR}`` from the LAUNCHING SHELL's ``os.environ``. Running
# ``sac agents start neurovista`` from the sac repo dir (whose ``.envrc``
# exports ``CCT_AGENT_ID=scitex-agent-container`` + that bot's token) baked
# ``CCT_AGENT_ID=scitex-agent-container`` and the wrong bot token into
# neurovista's materialized ``.mcp.json`` — neurovista's telegrammer then
# attached with the wrong identity.
#
# Per-agent identity must ALWAYS come from the agent's OWN runtime env (its
# ``.envrc`` via direnv, working since the ``DIRENV_CONFIG`` fix), never from
# whatever directory ``sac agents start`` was typed in. So we leave these
# refs as literal ``${VAR}`` placeholders for RUNTIME expansion, and we keep
# secrets (bot tokens) out of materialized files on disk. The ``CCT_`` prefix
# rule below covers ``CCT_AGENT_ID`` / ``CCT_BOT_TOKEN`` /
# ``CCT_ALLOWED_USERS`` / ``CCT_STATE_DIR`` and any future ``CCT_*`` var.
#
# DEPRECATED — name-based, pending removal (INCIDENT 2026-07-05, operator
# /incident): hardcoding OTHER packages' exact env-var names here is a
# separation-of-concerns violation — sac has no business knowing
# scitex-todo's or claude-code-telegrammer's internal identity-var naming,
# and this NAME-based list (plus the ``CCT_`` prefix rule) must eventually go
# away entirely. The replacement is the ``${RUNTIME:VAR}`` SYNTAX-based
# escape marker below (see :func:`interpolate_env`): sac recognizes only the
# ``RUNTIME:`` marker SHAPE, never any variable NAME, and the decision of
# which vars are runtime-only shifts to the TEMPLATE AUTHOR (each downstream
# package writes its own ``.mcp.json`` / template files and opts individual
# ``${VAR}`` refs into ``${RUNTIME:VAR}`` itself).
#
# This hardcoded set is kept running IN PARALLEL with the new syntax for
# now — deliberately NOT removed in the same change that introduces
# ``${RUNTIME:VAR}`` — because removing it here, before downstream packages
# have migrated their OWN templates to the new escape syntax, would bake
# these vars as literal values into materialized files (a real regression).
# DO NOT delete entries from here (or the ``CCT_`` prefix rule) until
# scitex-todo and claude-code-telegrammer have both shipped templates using
# ``${RUNTIME:VAR}`` for their identity vars — that migration + removal is a
# separate follow-up PR.
# Legacy pre-0.7.30 identity ALIASES — deprecated names that a downstream
# consumer now HARD-REJECTS if present. The card MCP server refuses any call
# when ``SCITEX_TODO_AGENT`` is set (its message, verbatim at the time: "was
# renamed to ``SCITEX_TODO_AGENT_ID``; unset the old var" — that successor has
# since itself been retired in favour of ``SCITEX_CARDS_AGENT_ID``), so a
# stale copy of one of these baked into a materialized/folded ``.env`` is not
# merely redundant like the current ``_ID`` names — it is FATAL to the
# consumer (live write-outage, INCIDENT 2026-07-05/06). Kept as its OWN named
# subset (unioned into :data:`_RUNTIME_ONLY_VARS` below, so the name list is
# defined ONCE) precisely so the ``.env``-fold guard in :mod:`_envrc` can drop
# ONLY these legacy aliases while the CURRENT identity vars
# (``SCITEX_CARDS_AGENT_ID`` / ``CCT_*`` / ``SAC_NAME``) legitimately REMAIN in
# the ``--env-file`` the container needs at runtime (the materialized
# ``.mcp.json`` expands ``${SCITEX_CARDS_AGENT_ID}`` from that container env).
# Dropping the current vars from the fold too would strip the agent's live
# identity — a second outage — so the fold must NOT use the broad
# :func:`_is_runtime_only_var`.
_LEGACY_IDENTITY_VARS = frozenset(
    {
        "SCITEX_TODO_AGENT",
        "SCITEX_TODO_TASKS",
    }
)

# STORE IDENTITY — the scitex-cards entries below, and WHY a name is being
# ADDED to a list the paragraph above says is shrinking.
#
# WHY (INCIDENT 2026-08-12, card
# sac-cards-db-store-identity-not-baked-20260812): this is the 2026-07-02
# incident again, one variable over. An agent's own container shell and the
# scitex-cards MCP server it talks to disagreed about which database they
# were using — the container env and the agent's ``spec.yaml`` both said
# ``postgresql://scitex_cards@127.0.0.1:55432/scitex_cards`` while the MCP
# server had ``...:5442``. The agent WROTE cards to one postgres database and
# READ them from another; three databases ended up holding fragments of one
# board under a single ``store_uuid``.
#
# MECHANISM: the operator-authored shared template
# ``agents/_shared/to_home/.mcp.json`` carries
# ``"SCITEX_CARDS_DB": "${SCITEX_CARDS_DB}"``, and ``interpolate_env`` runs
# HOST-SIDE inside ``sac agents start`` — so it substituted from the
# LAUNCHING SHELL's environ. The operator's ``~/.bashrc`` exports
# ``SCITEX_CARDS_DB=...:5442``, so starting an agent from that shell baked the
# literal ``:5442`` into the materialized ``runtime/<agent>/home/.mcp.json``.
# Claude Code spreads a server entry's ``env`` block LAST over the inherited
# process env (see :mod:`._mcp_spec_env`), so the baked value WON inside the
# MCP server even though the container env said ``:55432``.
#
# The measured tell was a clean natural experiment: of 19 materialized
# ``.mcp.json`` files under ``runtime/``, 18 still held the literal
# ``${SCITEX_CARDS_DB}`` (those agents were started from shells that did not
# export it, so the ``os.environ.get(name, m.group(0))`` default kept the ref
# intact) and exactly ONE held a hardcoded DSN — the agent that happened to be
# started from the operator's interactive shell. Restated as the invariant it
# violates: WHICHEVER SHELL THE OPERATOR HAPPENED TO TYPE ``sac agents start``
# IN SILENTLY DECIDED WHICH DATABASE THAT AGENT'S MCP SERVER WROTE TO, AND A
# RESTART FROM A DIFFERENT SHELL SILENTLY MOVED THE AGENT TO A DIFFERENT
# STORE. A store target is per-agent IDENTITY in exactly the sense the module
# header means it, so it must come from the agent's OWN runtime env.
#
# ``SCITEX_CARDS_STORE_UUID`` is included for the same reason and is if
# anything sharper: scitex-cards reads it ONLY from the environment — never
# from the database, never derived from a path, deliberately, so the
# store-identity check cannot go circular — and it is the pin that decides
# that check's ACCEPT / ADOPT / REFUSE verdict. A host-baked pin would let a
# launching shell silently declare which store an agent considers legitimate,
# turning a wrong-store launch into an *authorized* one.
#
# WHY NAME-BASED AND NOT ``${RUNTIME:VAR}``: the syntax-based escape is the
# preferred mechanism and would be the right home for these, but it is the
# TEMPLATE AUTHOR's to apply, and the scitex-cards ``.mcp.json`` template has
# NOT migrated — it still writes a plain ``${SCITEX_CARDS_DB}``. Until it
# does, this deprecated name-based fallback is the ONLY mechanism that can
# protect the ref, which is precisely the "kept running IN PARALLEL ... until
# downstream packages have migrated their OWN templates" case the paragraph
# above describes. Adding here is therefore consistent with that paragraph,
# not a reversal of it: the list shrinks by MIGRATION, not by leaving a known
# live identity var unprotected.
#
# REMOVAL CONDITION: delete these two entries once the scitex-cards-authored
# template references ``${RUNTIME:SCITEX_CARDS_DB}`` (and
# ``${RUNTIME:SCITEX_CARDS_STORE_UUID}`` if it ever references the pin) —
# same bar as the scitex-todo / claude-code-telegrammer entries above, and not
# one moment sooner, since removing them first re-bakes the DSN.
#
# NOT in :data:`_LEGACY_IDENTITY_VARS`, deliberately: that subset is what the
# ``.env`` fold in :mod:`._envrc` DROPS. ``SCITEX_CARDS_DB`` must REMAIN in
# the container's ``--env-file`` — it is the value the materialized
# ``.mcp.json``'s surviving ``${SCITEX_CARDS_DB}`` ref expands FROM at
# runtime. Dropping it there would leave the ref unexpanded and take the
# agent's store away entirely.
_CARDS_STORE_IDENTITY_VARS = frozenset(
    {
        "SCITEX_CARDS_DB",
        "SCITEX_CARDS_STORE_UUID",
    }
)

_RUNTIME_ONLY_VARS = (
    frozenset(
        {
            # The CURRENT board identity. Its retired predecessor
            # ``SCITEX_TODO_AGENT_ID`` was listed here from the start and this
            # one never was, so the canonical name was the ONE identity var
            # this guard did not cover (measured 2026-08-22:
            # ``_is_runtime_only_var("SCITEX_CARDS_AGENT_ID")`` was False while
            # the legacy name, SAC_NAME and SCITEX_CARDS_DB were all True).
            #
            # It is a LATENT trap, not a live bug: no template references
            # ``${SCITEX_CARDS_AGENT_ID}`` today, so nothing has been baked. The
            # moment one does — and retiring the legacy key from the baseline
            # ``.mcp.json`` requires exactly that — deploy-time interpolation
            # would substitute the DEPLOYING process's identity into every
            # agent's materialized file. That is the 2026-07-02 wrong-identity
            # incident this whole mechanism exists to prevent, re-entered
            # through the migration meant to close it.
            "SCITEX_CARDS_AGENT_ID",
            # RETIRED board identity — kept here ON PURPOSE. sac no longer
            # WRITES it (specs, twins and `agents create` emit the canonical
            # name only), but the host baseline ``.mcp.json`` copies still
            # reference ``${SCITEX_TODO_AGENT_ID}``; dropping the entry before
            # that baseline is DELIVERED would let deploy-time interpolation
            # bake the deploying process's identity into every materialized
            # file — the 2026-07-02 incident described just above. Remove it
            # with the legacy shim, not before.
            "SCITEX_TODO_AGENT_ID",
            # scitex-todo >= 0.7.30 name
            "SCITEX_TODO_TASKS_YAML_SHARED",
            "SAC_NAME",
            "CLAUDE_AGENT_ID",
            "CLAUDE_AGENT_ROLE",
        }
    )
    # scitex-cards store identity (INCIDENT 2026-08-12) — see the comment on
    # :data:`_CARDS_STORE_IDENTITY_VARS` above.
    | _CARDS_STORE_IDENTITY_VARS
    # Legacy pre-0.7.30 names — kept as a GUARD only (never injected by sac
    # anymore): a stale deployer shell exporting the old names must still not
    # bake them into materialized files.
    | _LEGACY_IDENTITY_VARS
)


def _is_legacy_identity_var(name: str) -> bool:
    """True when ``name`` is a DEPRECATED identity alias the downstream
    consumer hard-rejects → must be DROPPED from a folded ``.env``.

    Narrower than :func:`_is_runtime_only_var` on purpose: the ``.env`` fold
    (:func:`_envrc.eval_envrc` / :func:`_envrc.eval_envrc_cascade`) produces
    the container's ``--env-file``, so the CURRENT identity vars must survive
    there — only the fatal legacy aliases in :data:`_LEGACY_IDENTITY_VARS` are
    stripped. See that constant's comment.
    """
    return name in _LEGACY_IDENTITY_VARS


def _is_runtime_only_var(name: str) -> bool:
    """True when ``name`` is per-agent identity → keep as ``${VAR}`` literal.

    Any ``CCT_*`` var is runtime-only (identity + telegram secrets), plus the
    explicit members of :data:`_RUNTIME_ONLY_VARS`. Both are DEPRECATED
    NAME-based mechanisms kept only as a fallback alongside the new
    SYNTAX-based ``${RUNTIME:VAR}`` escape marker — see the module-level
    comment on :data:`_RUNTIME_ONLY_VARS` and :func:`interpolate_env`.
    """
    return name.startswith("CCT_") or name in _RUNTIME_ONLY_VARS


def validate_marker_invariants(text: str, source_name: str) -> None:
    """Hard-fail if Start/End markers are missing or malformed."""
    start_count = text.count(START_MARKER_PREFIX)
    end_count = text.count(END_MARKER)
    if start_count != 1 or end_count != 1:
        raise WorkspaceCLAUDEMarkerError(
            f"{source_name}: expected exactly 1 Start marker and 1 End "
            f"marker, found Start={start_count} End={end_count}. "
            "Refusing to deploy to avoid data loss. Restore the markers "
            "manually before retrying."
        )
    if text.find(START_MARKER_PREFIX) > text.find(END_MARKER):
        raise WorkspaceCLAUDEMarkerError(
            f"{source_name}: Start marker appears AFTER End marker. "
            "This indicates a corrupted file. Refusing to deploy."
        )


def split_around_generated_section(text: str, source_name: str) -> tuple[str, str]:
    """Split ``text`` into ``(head, tail)`` around the sac generated section.

    * ``head`` — everything BEFORE the Start marker, preserved verbatim. When
      the file has NO generated section yet (0 markers) the ENTIRE content is
      the head: a file that already holds OTHER content — e.g. the
      ``setup_claude_md`` auto agent-section (which uses its own
      ``<!-- agent-container:start/end -->`` marker style), or operator-authored
      text — composes cleanly instead of fatal-ing. This is what lets the
      baseline live at ``.claude/CLAUDE.md`` next to the auto section.
    * ``tail`` — everything AFTER the End marker (preserved operator content).

    Malformed markers (duplicate or swapped Start/End) still fail loud via
    :func:`validate_marker_invariants`.
    """
    if not text.strip():
        return "", ""
    start_count = text.count(START_MARKER_PREFIX)
    end_count = text.count(END_MARKER)
    if start_count == 0 and end_count == 0:
        head = text if text.endswith("\n") else text + "\n"
        return head, ""
    validate_marker_invariants(text, source_name)  # fatals on malformed
    start = text.find(START_MARKER_PREFIX)
    end = text.find(END_MARKER) + len(END_MARKER)
    return text[:start], text[end:]


def extract_generated_body(text: str) -> str:
    """Return the content INSIDE the sac generated section, or ``""``.

    The companion to :func:`split_around_generated_section`, which returns what
    SURROUNDS the section and discards what is in it. The two-pass to_home
    overlay needs the inside: the per-agent layer composes ONTO the baseline
    layer's body, and without reading that body the baseline is dropped.

    Returns ``""`` when the text has no complete section — an absent section is
    an empty body to compose onto, which is exactly the first-layer case.
    """
    start = text.find(START_MARKER_PREFIX)
    end = text.find(END_MARKER)
    if start == -1 or end == -1 or end < start:
        return ""
    body_start = text.find("\n", start)
    if body_start == -1 or body_start > end:
        return ""
    return text[body_start + 1 : end].strip()


def extract_user_tail(workspace_path: Path) -> str:
    """Return content past the End marker in an existing workspace file.

    Empty string when the file is missing, unreadable, or has no End
    marker. Used to preserve user-appended content across a re-deploy
    of marker-protected files (CLAUDE.md / state.md).
    """
    if not workspace_path.exists():
        return ""
    try:
        existing = workspace_path.read_text()
    except OSError:  # stx-allow: fallback (reason: file system operation failure)
        return ""
    idx = existing.rfind(END_MARKER)
    if idx == -1:
        return ""
    return existing[idx + len(END_MARKER) :]


# Marker prefix for the SYNTAX-based runtime-only escape form
# ``${RUNTIME:VAR}`` (INCIDENT 2026-07-05 corrected design — see the
# module-level comment on :data:`_RUNTIME_ONLY_VARS`). This is a pure
# ESCAPE SHAPE: sac's ``interpolate_env`` recognizes the ``RUNTIME:`` marker
# and never inspects, branches on, or needs to know the variable NAME
# wrapped inside it. The decision of WHICH vars are runtime-only lives
# entirely with the TEMPLATE AUTHOR — a downstream package (scitex-todo,
# claude-code-telegrammer, ...) writes its own ``.mcp.json`` / template
# files and opts individual ``${VAR}`` refs into ``${RUNTIME:VAR}`` itself,
# with zero sac-side knowledge of what that var is for. A matched
# ``${RUNTIME:VAR}`` collapses to plain ``${VAR}`` in the materialized
# output — unchanged from what downstream runtime tooling (direnv / the
# agent's own ``.envrc``) already expects to expand at container boot.
_RUNTIME_ESCAPE_PREFIX = "RUNTIME:"

# Matches EITHER the runtime-escape form ``${RUNTIME:VAR}`` (group
# "escaped") OR a plain substitution ref ``${VAR}`` (group "plain"). Order
# matters only in that both alternatives are tried per match; the escape
# form is checked first since it is the more specific shape.
_ENV_REF_RE = re.compile(r"\$\{RUNTIME:(?P<escaped>\w+)\}|\$\{(?P<plain>\w+)\}")


def interpolate_env(text: str) -> str:
    """Substitute ``${VAR}`` with ``os.environ[VAR]``, leaving unknown
    refs untouched (so an unset env var becomes a visible artefact
    rather than silently collapsing to empty string).

    Two mechanisms keep a ref from being substituted here, so it survives
    as a literal ``${VAR}`` for RUNTIME expansion from the agent's own env
    instead (the fix for the 2026-07-02 wrong-identity incident):

    1. **SYNTAX-based (current, preferred)** — the template author writes
       ``${RUNTIME:VAR}`` instead of ``${VAR}``. Recognized purely by
       shape; sac never inspects the variable name. See
       :data:`_RUNTIME_ESCAPE_PREFIX`.
    2. **NAME-based (deprecated fallback, still active)** — :data:`_RUNTIME_ONLY_VARS`
       and the ``CCT_`` prefix rule via :func:`_is_runtime_only_var`. Kept
       running IN PARALLEL with mechanism 1 until every known downstream
       template has migrated to ``${RUNTIME:VAR}``; removal is a separate
       follow-up PR (see the module header on ``_RUNTIME_ONLY_VARS``).
    """

    def _replace(m: re.Match) -> str:
        escaped_name = m.group("escaped")
        if escaped_name is not None:
            return "${" + escaped_name + "}"  # collapse marker to plain ${VAR}
        name = m.group("plain")
        if _is_runtime_only_var(name):
            return m.group(0)  # keep ${VAR} literal for runtime expansion
        return os.environ.get(name, m.group(0))

    return _ENV_REF_RE.sub(_replace, text)


def interpolate_metadata(text: str, config: AgentConfig) -> str:
    """Substitute ``${metadata.name}`` and ``${metadata.labels.<k>}``
    against ``config``. Unknown keys pass through unchanged.
    """

    def _replace(m: re.Match) -> str:
        key = m.group(1)
        if key == "metadata.name":
            return config.name
        if key.startswith("metadata.labels."):
            label = key[len("metadata.labels.") :]
            return config.labels.get(label) or m.group(0)
        return m.group(0)

    return re.sub(r"\$\{([^}]+)\}", _replace, text)


__all__ = [
    "END_MARKER",
    "START_MARKER_PREFIX",
    "extract_generated_body",
    "extract_user_tail",
    "interpolate_env",
    "interpolate_metadata",
    "validate_marker_invariants",
]
