"""Text helpers for the to_home/ materialization pipeline.

Extracted from :mod:`_to_home` to keep that module under the 512-line
file-size cap (2026-06-15 — see ``GITIGNORED/REFACTORING.md``).
Contains the marker constants, marker-invariant validator, user-tail
extractor, and ``${VAR}`` / ``${metadata.*}`` interpolators used by
:func:`_deploy_marker_protected` and :func:`_deploy_plain_file` in
the orchestrator module.
"""

from __future__ import annotations

import logging
import os
import re
from pathlib import Path

from ..config import AgentConfig
from ._to_home_errors import WorkspaceCLAUDEMarkerError

try:  # pragma: no cover - trivial import shim
    import tomllib  # Python >= 3.11 stdlib
except ModuleNotFoundError:  # pragma: no cover - py3.10 fallback
    try:
        import tomli as tomllib  # type: ignore[no-redef]
    except ModuleNotFoundError:  # pragma: no cover - tomli not installed either
        tomllib = None  # type: ignore[assignment]

logger = logging.getLogger(__name__)

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
# DEPRECATED — pending migration (INCIDENT 2026-07-05, operator /incident):
# hardcoding OTHER packages' exact env-var names here is a separation-of-
# concerns violation — sac has no business knowing scitex-todo's or
# claude-code-telegrammer's internal identity-var naming. The generic,
# non-hardcoded replacement is :func:`_load_project_runtime_only_vars`,
# which reads a per-project opt-in manifest from that project's OWN
# ``pyproject.toml`` (``[tool.sac] runtime_only_env_vars = [...]``). This
# frozenset is kept as-is (additive-only fix) so nothing regresses for
# packages that haven't added the manifest yet — DO NOT delete entries from
# here until the owning package (scitex-todo, etc.) has shipped the
# ``[tool.sac]`` table AND a deploy cycle has run against it. Once every
# known consumer has migrated, a follow-up cleanup PR can shrink/delete this
# set.
_RUNTIME_ONLY_VARS = frozenset(
    {
        # scitex-todo >= 0.7.30 names
        "SCITEX_TODO_AGENT_ID",
        "SCITEX_TODO_TASKS_YAML_SHARED",
        # Legacy pre-0.7.30 names — kept as a GUARD only (never injected by
        # sac anymore): a stale deployer shell exporting the old names must
        # still not bake them into materialized files.
        "SCITEX_TODO_AGENT",
        "SCITEX_TODO_TASKS",
        "SAC_NAME",
        "CLAUDE_AGENT_ID",
        "CLAUDE_AGENT_ROLE",
    }
)


def _load_project_runtime_only_vars(workdir: str | None) -> frozenset[str]:
    """Read a project's own opt-in runtime-only env-var manifest.

    Generic replacement for the hardcoded :data:`_RUNTIME_ONLY_VARS`
    (INCIDENT 2026-07-05): instead of sac's source knowing OTHER packages'
    exact identity-var names, each downstream package declares its own list
    in ITS OWN ``pyproject.toml``::

        [tool.sac]
        runtime_only_env_vars = ["SCITEX_TODO_AGENT_ID", "SCITEX_TODO_TASKS_YAML_SHARED"]

    ``workdir`` is the agent's project root (``AgentConfig.expanded_workdir``
    — the same path the apptainer runtime ``cwd``s into, see
    ``_apptainer_argv_guard.py``). Missing ``workdir``, missing
    ``pyproject.toml``, a missing/malformed ``[tool.sac]`` table, or no TOML
    parser available are all EXPECTED, common states for a package that
    hasn't migrated yet — never raised loud, just logged at DEBUG and
    treated as an empty contribution.
    """
    if not workdir or tomllib is None:
        return frozenset()
    pyproject_path = Path(workdir).expanduser() / "pyproject.toml"
    if not pyproject_path.is_file():
        return frozenset()
    try:
        with open(pyproject_path, "rb") as fh:
            doc = tomllib.load(fh)
    except (
        OSError,
        tomllib.TOMLDecodeError,
    ) as exc:  # stx-allow: fallback (reason: not-yet-migrated project is expected)
        logger.debug(
            "to_home: could not parse %s for [tool.sac]: %s", pyproject_path, exc
        )
        return frozenset()
    tool_table = doc.get("tool", {})
    sac_table = tool_table.get("sac", {}) if isinstance(tool_table, dict) else {}
    if not isinstance(sac_table, dict):
        logger.debug(
            "to_home: %s has non-table [tool.sac]; ignoring", pyproject_path
        )
        return frozenset()
    names = sac_table.get("runtime_only_env_vars", [])
    if not isinstance(names, list):
        logger.debug(
            "to_home: %s [tool.sac].runtime_only_env_vars is not a list; ignoring",
            pyproject_path,
        )
        return frozenset()
    return frozenset(n for n in names if isinstance(n, str))


def _is_runtime_only_var(
    name: str, extra_runtime_only_vars: frozenset[str] = frozenset()
) -> bool:
    """True when ``name`` is per-agent identity → keep as ``${VAR}`` literal.

    Any ``CCT_*`` var is runtime-only (identity + telegram secrets — this
    prefix rule is a candidate for its own future manifest key, e.g.
    ``[tool.sac] runtime_only_env_prefixes = ["CCT_"]``, but that
    generalization is out of scope here), plus the explicit members of
    :data:`_RUNTIME_ONLY_VARS` (deprecated hardcoded fallback) and
    ``extra_runtime_only_vars`` (per-project manifest, see
    :func:`_load_project_runtime_only_vars`).
    """
    return (
        name.startswith("CCT_")
        or name in _RUNTIME_ONLY_VARS
        or name in extra_runtime_only_vars
    )


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


def interpolate_env(text: str, config: AgentConfig | None = None) -> str:
    """Substitute ``${VAR}`` with ``os.environ[VAR]``, leaving unknown
    refs untouched (so an unset env var becomes a visible artefact
    rather than silently collapsing to empty string).

    Per-agent IDENTITY vars (see :data:`_RUNTIME_ONLY_VARS`, the ``CCT_``
    prefix rule, and — when ``config`` is supplied — that agent project's
    own ``[tool.sac].runtime_only_env_vars`` manifest via
    :func:`_load_project_runtime_only_vars`) are NEVER substituted here —
    they stay as literal ``${VAR}`` for RUNTIME expansion from the agent's
    own env, regardless of whether they happen to be present in the
    deployer's ``os.environ``. This is the fix for the 2026-07-02
    wrong-identity incident (see the module header on
    ``_RUNTIME_ONLY_VARS``); the ``config`` parameter is the 2026-07-05
    follow-up that lets a project declare its own runtime-only vars instead
    of sac hardcoding them.
    """
    extra_runtime_only_vars = (
        _load_project_runtime_only_vars(getattr(config, "expanded_workdir", None))
        if config is not None
        else frozenset()
    )

    def _replace(m: re.Match) -> str:
        name = m.group(1)
        if _is_runtime_only_var(name, extra_runtime_only_vars):
            return m.group(0)  # keep ${VAR} literal for runtime expansion
        return os.environ.get(name, m.group(0))

    return re.sub(r"\$\{(\w+)\}", _replace, text)


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
    "extract_user_tail",
    "interpolate_env",
    "interpolate_metadata",
    "validate_marker_invariants",
]
