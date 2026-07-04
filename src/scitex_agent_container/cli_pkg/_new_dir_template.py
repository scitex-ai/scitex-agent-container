#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Directory-template support for ``sac agents create``.

Beyond the two inline string templates (``minimal`` / ``full``), the
operator ships *directory* templates that live in the agents root,
named ``_template_<kind>/`` (e.g. ``_template_python_developer/``,
``_template_researcher/``, ``_template_generalist/``). Each is a real
directory carrying ``spec.yaml`` + ``to_home/`` and whatever else the
kind needs, with literal placeholder tokens of the form
``SAC_PLACEHOLDER_<NAME>`` baked into the files (workdir paths, install
targets, labels, STATE_DB names, …).

``sac agents create <name> --template <kind>`` instantiates such a template
by *copying the whole tree* to ``<base-dir>/<name>/`` and substituting
the placeholder tokens. The substitution is fail-loud: if ANY
``SAC_PLACEHOLDER_*`` token survives, the partial output directory is
removed and a non-zero error is raised naming every unfilled token, the
file(s)/line(s) it appears in, and the flag that would fill it.

Discovery is dynamic: dropping a new ``_template_foo/`` dir into the
agents root surfaces ``foo`` as a ``--template`` choice with NO code
change here.
"""

from __future__ import annotations

import re
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List

# Literal token shape baked into the dir-templates. The trailing name is
# captured so we can report exactly which placeholders are unfilled and
# hint at the matching CLI flag.
_PLACEHOLDER_PREFIX = "SAC_PLACEHOLDER_"
_PLACEHOLDER_RE = re.compile(r"SAC_PLACEHOLDER_[A-Z0-9_]+")

# Dir-template directories carry this prefix; the suffix becomes the
# ``--template`` choice (``_template_python_developer`` → ``python_developer``).
_DIR_TEMPLATE_PREFIX = "_template_"


class DirTemplateError(Exception):
    """Raised when a dir-template cannot be instantiated cleanly.

    Carries a human-readable message already formatted for stderr; the
    CLI converts it into a ``click.ClickException`` so the exit code is
    non-zero.
    """


def discover_dir_templates(agents_root: Path) -> Dict[str, Path]:
    """Return ``{kind: dir}`` for every ``_template_*`` subdir of ``agents_root``.

    The map key is the suffix after ``_template_`` (the ``--template``
    choice); the value is the absolute directory path. Discovery is
    dynamic — a directory dropped in afterwards is found with no code
    change. Missing / non-dir roots yield an empty map (callers fall
    back to the inline templates only).
    """
    out: Dict[str, Path] = {}
    if not agents_root.is_dir():
        return out
    for sub in sorted(agents_root.iterdir()):
        if not sub.is_dir():
            continue
        if not sub.name.startswith(_DIR_TEMPLATE_PREFIX):
            continue
        kind = sub.name[len(_DIR_TEMPLATE_PREFIX) :]
        if not kind:
            continue
        out[kind] = sub
    return out


def _flag_hint(token: str) -> str:
    """Return the CLI flag that fills ``token`` (an exact ``SAC_PLACEHOLDER_*``).

    ``SAC_PLACEHOLDER_PROJECT`` → ``--project``;
    ``SAC_PLACEHOLDER_AGENT_ID`` → ``--agent-id``;
    anything else → ``--set <SUFFIX>=...``.
    """
    suffix = token[len(_PLACEHOLDER_PREFIX) :]
    if suffix == "PROJECT":
        return "--project <value>"
    if suffix == "AGENT_ID":
        return "--agent-id <value>"
    return f"--set {suffix}=<value>"


def _build_substitutions(
    project: str | None,
    agent_id: str,
    extra: Dict[str, str],
) -> Dict[str, str]:
    """Map full placeholder tokens → fill values.

    ``--project``/``--agent-id`` are sugar for the two well-known tokens;
    ``--set KEY=VALUE`` fills ``SAC_PLACEHOLDER_<KEY>`` by exact name.
    ``project`` may be ``None`` (operator omitted ``--project``); the
    token is simply not in the map, so the post-scan reports it as
    unfilled with a clear hint.
    """
    subs: Dict[str, str] = {}
    if project is not None:
        subs[_PLACEHOLDER_PREFIX + "PROJECT"] = project
    subs[_PLACEHOLDER_PREFIX + "AGENT_ID"] = agent_id
    for key, value in extra.items():
        subs[_PLACEHOLDER_PREFIX + key] = value
    return subs


def parse_set_pairs(pairs: tuple[str, ...]) -> Dict[str, str]:
    """Parse repeatable ``--set KEY=VALUE`` into ``{KEY: VALUE}``.

    KEY is upper-cased so ``--set extra=v`` fills ``SAC_PLACEHOLDER_EXTRA``
    (the tokens are upper-case by convention). A pair without ``=`` is a
    usage error.
    """
    out: Dict[str, str] = {}
    for pair in pairs:
        if "=" not in pair:
            raise DirTemplateError(
                f"--set expects KEY=VALUE, got {pair!r} (no '=')."
            )
        key, value = pair.split("=", 1)
        key = key.strip().upper()
        if not key:
            raise DirTemplateError(f"--set expects a non-empty KEY, got {pair!r}.")
        out[key] = value
    return out


@dataclass
class _Remaining:
    token: str
    rel_path: str
    lineno: int


def _scan_remaining(agent_dir: Path) -> List[_Remaining]:
    """Return every surviving ``SAC_PLACEHOLDER_*`` occurrence under ``agent_dir``.

    Walks all regular files; binary / undecodable files are skipped (the
    templates are text). Reports file-relative path + 1-based line number
    so the error can point the operator at the exact spot.
    """
    out: List[_Remaining] = []
    for path in sorted(agent_dir.rglob("*")):
        if not path.is_file():
            continue
        try:
            text = path.read_text()
        except (UnicodeDecodeError, OSError):
            continue
        for lineno, line in enumerate(text.splitlines(), start=1):
            for match in _PLACEHOLDER_RE.findall(line):
                out.append(
                    _Remaining(
                        token=match,
                        rel_path=str(path.relative_to(agent_dir)),
                        lineno=lineno,
                    )
                )
    return out


def _format_remaining_error(remaining: List[_Remaining]) -> str:
    """Build the fail-loud message naming each unfilled token + a fill hint."""
    by_token: Dict[str, List[_Remaining]] = {}
    for item in remaining:
        by_token.setdefault(item.token, []).append(item)

    lines = [
        "Unfilled placeholder token(s) remain after substitution — "
        "refusing to leave a half-written agent.",
    ]
    for token in sorted(by_token):
        locations = ", ".join(
            f"{r.rel_path}:{r.lineno}" for r in by_token[token]
        )
        lines.append(f"  {token}")
        lines.append(f"    at: {locations}")
        lines.append(f"    fill with: {_flag_hint(token)}")
    return "\n".join(lines)


def _apply_substitutions(agent_dir: Path, subs: Dict[str, str]) -> None:
    """Substitute every known token across all text files under ``agent_dir``."""
    for path in sorted(agent_dir.rglob("*")):
        if not path.is_file():
            continue
        try:
            text = path.read_text()
        except (UnicodeDecodeError, OSError):
            continue
        new_text = text
        for token, value in subs.items():
            new_text = new_text.replace(token, value)
        if new_text != text:
            path.write_text(new_text)


def instantiate_dir_template(
    template_dir: Path,
    agent_dir: Path,
    *,
    project: str | None,
    agent_id: str,
    extra: Dict[str, str],
    force: bool,
) -> None:
    """Copy ``template_dir`` → ``agent_dir`` and fill placeholder tokens.

    Fail-loud contract: on ANY surviving ``SAC_PLACEHOLDER_*`` token the
    freshly-created ``agent_dir`` is removed (so no half-written agent is
    left behind) and :class:`DirTemplateError` is raised with a message
    naming each unfilled token, its location(s), and the fill flag.

    ``force`` controls whether an existing ``agent_dir`` is replaced.
    """
    spec_path = agent_dir / "spec.yaml"
    if spec_path.exists() and not force:
        raise DirTemplateError(
            f"Refusing to overwrite existing spec at {spec_path}. "
            "Re-run with --force to replace, or pick a different name."
        )

    # Track whether we created the dir so cleanup only removes our own
    # output (never a pre-existing dir the operator passed via --force).
    pre_existing = agent_dir.exists()
    if pre_existing and force:
        shutil.rmtree(agent_dir)

    shutil.copytree(template_dir, agent_dir)

    subs = _build_substitutions(project, agent_id, extra)
    _apply_substitutions(agent_dir, subs)

    remaining = _scan_remaining(agent_dir)
    if remaining:
        # Remove the partial output so a failed run leaves nothing behind.
        shutil.rmtree(agent_dir, ignore_errors=True)
        raise DirTemplateError(_format_remaining_error(remaining))


__all__ = [
    "DirTemplateError",
    "discover_dir_templates",
    "instantiate_dir_template",
    "parse_set_pairs",
]
