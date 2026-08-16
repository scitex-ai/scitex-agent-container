"""Guard: no GitHub-HOSTED runner may appear in ``.github/workflows/``.

Operator directive, 2026-07-14 (condensed):

    Never use GitHub-hosted runners. MAKE IT AN ERROR via linter/hook.
    Mandatory, no exceptions. If Spartan (the self-hosted pool) breaks,
    that is OUR problem — never escape back to a hosted runner.

PR #694 migrated sac's 11 jobs onto the self-hosted Spartan pool, but the
GUARD was never built: nothing stopped the next workflow from quietly
landing on ``ubuntu-latest`` again, and we would not have noticed until
it had. This module is that guard.

Why this is not a ``grep ubuntu-latest``
========================================
Three ways the naive grep is wrong, all of them live in this repo today:

1. It MISSES ``ubuntu-24.04`` / ``macos-14`` / ``windows-2022``. We match
   the runner-image FAMILY (``ubuntu-`` / ``macos-`` / ``windows-``), not
   one literal.
2. It FALSE-FLAGS our own migrated files. Both the workflow FILENAMES
   (``rtd-sphinx-build-on-ubuntu-latest.yml``) and the job ``name:`` fields
   (``ruff-on-ubuntu-latest``) still carry the legacy string. Only a job's
   ``runs-on:`` decides where it executes, so we parse YAML and read
   ``runs-on`` — nothing else.
3. It cannot resolve ``runs-on: ${{ fromJSON(vars.CI_RUNS_ON ||
   '["self-hosted","Linux","X64","scitex-ci"]') }}`` — the expression our
   11 migrated jobs actually use.

Three verdicts, never two
=========================
A boolean here would collapse "I cannot tell" into one of the poles, and
whichever pole it picks is a bug: assume-hosted false-REDs a healthy job,
assume-self-hosted opens a silent bypass (``runs-on: ${{ vars.ANYTHING }}``
proves nothing). So :func:`classify_runs_on` returns one of:

* ``SELF_HOSTED``   — we can PROVE it lands on our hardware.
* ``HOSTED``        — we can PROVE it lands on GitHub's.
* ``UNRESOLVABLE``  — we can prove NEITHER.

``UNRESOLVABLE`` fails the guard, on purpose: a guard that cannot see
cannot guard, and an unreadable ``runs-on`` is exactly the shape a bypass
would take. It is reported under its own code so the message stays honest
— it says "cannot prove", not "is hosted".

Known limitation (stated, not hidden)
=====================================
``vars.CI_RUNS_ON`` is a GitHub repo/org Variable resolved at run time. A
static reader cannot see its value; we check the INLINE DEFAULT that ships
in the repo. If someone re-points that Variable at a hosted image, this
guard cannot see it — that is a GitHub-settings change, not a code change,
and belongs to org/branch protection.

Entry points
============
* ``python -m scitex_agent_container._hosted_runner_guard [REPO_ROOT]``
  — exit 0 clean, exit 1 on any violation. Wired as (a) a CI job on the
  SELF-HOSTED pool and (b) a ``pre-commit`` hook.
* :func:`check_repo` — pure function; returns violations, raises nothing.
"""

from __future__ import annotations

import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

import yaml

# Runner-image families GitHub hosts. Prefix match, so `ubuntu-latest`,
# `ubuntu-24.04`, `ubuntu-22.04-arm`, `macos-13-xlarge`, `windows-2022`
# and the larger-runner variants (`ubuntu-latest-4-cores`) all match.
_HOSTED_PREFIXES = ("ubuntu-", "macos-", "windows-")

# The label GitHub puts on every self-hosted runner. Its presence is
# positive proof the job lands on our hardware.
_SELF_HOSTED_LABEL = "self-hosted"

# An allowlist entry must ARGUE its case. A stub ("legacy", "needed") is
# not an argument; this floor makes the reason a sentence, not a shrug.
_MIN_REASON_CHARS = 40

_ALLOWLIST_RELPATH = Path(".github") / "hosted-runner-allowlist.yaml"
_WORKFLOWS_RELDIR = Path(".github") / "workflows"

# Verdicts.
SELF_HOSTED = "SELF_HOSTED"
HOSTED = "HOSTED"
UNRESOLVABLE = "UNRESOLVABLE"

# Violation codes.
CODE_HOSTED = "SAC-CI001"  # hosted runner, not allowlisted
CODE_UNRESOLVABLE = "SAC-CI002"  # runs-on cannot be proven either way
CODE_ALLOWLIST_NO_REASON = "SAC-CI003"  # allowlist entry without an argument
CODE_ALLOWLIST_STALE = "SAC-CI004"  # allowlist entry that no longer applies

_EXPR_RE = re.compile(r"\$\{\{(.+?)\}\}", re.DOTALL)
_QUOTED_RE = re.compile(r"'([^']*)'|\"([^\"]*)\"")
_MATRIX_REF_RE = re.compile(r"matrix\.([A-Za-z0-9_-]+)")

_CANONICAL_RUNS_ON = (
    "runs-on: ${{ fromJSON(vars.CI_RUNS_ON || "
    '\'["self-hosted","Linux","X64","scitex-ci"]\') }}'
)


@dataclass(frozen=True)
class Violation:
    """One reason the guard failed. ``where`` is repo-relative."""

    code: str
    where: str
    message: str

    def render(self) -> str:
        return f"  {self.code}  {self.where}\n      {self.message}"


def _flatten(value: Any) -> list[str]:
    """Collapse a scalar / list / nested list into a flat list of strings."""
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, (list, tuple)):
        out: list[str] = []
        for item in value:
            out.extend(_flatten(item))
        return out
    return [str(value)]


def _matrix_values(job: dict, key: str) -> list[str]:
    """Every value ``matrix.<key>`` can take, incl. ``include:`` entries."""
    strategy = job.get("strategy")
    if not isinstance(strategy, dict):
        return []
    matrix = strategy.get("matrix")
    if not isinstance(matrix, dict):
        return []
    out = _flatten(matrix.get(key))
    include = matrix.get("include")
    if isinstance(include, list):
        for entry in include:
            if isinstance(entry, dict) and key in entry:
                out.extend(_flatten(entry[key]))
    return out


def _labels_from_expression(expr: str, job: dict) -> list[str]:
    """Best-effort static resolution of a ``${{ ... }}`` runs-on expression.

    Two sources, both static:

    * quoted literals — a bare ``'self-hosted'`` or an embedded JSON array
      such as ``'["self-hosted","Linux","X64","scitex-ci"]'`` (the inline
      default of our canonical ``fromJSON(vars.CI_RUNS_ON || ...)`` form);
    * ``matrix.<key>`` references — resolved against the job's own
      ``strategy.matrix`` (so a matrix fanning out over ``[ubuntu-latest,
      macos-latest]`` is caught, not waved through).
    """
    labels: list[str] = []
    for match in _QUOTED_RE.finditer(expr):
        literal = match.group(1) if match.group(1) is not None else match.group(2)
        stripped = literal.strip()
        if stripped.startswith("["):
            try:
                labels.extend(_flatten(json.loads(stripped)))
                continue
            except ValueError:
                pass  # not JSON after all — fall through and take it whole
        if stripped:
            labels.append(stripped)
    for match in _MATRIX_REF_RE.finditer(expr):
        labels.extend(_matrix_values(job, match.group(1)))
    return labels


def _runner_labels(job: dict) -> tuple[list[str], bool]:
    """Return ``(labels, saw_unresolvable_expression)`` for one job.

    ``runs-on`` may be a scalar, a list of labels, or the runner-group
    mapping (``{group: ..., labels: [...]}``). Any of those may itself be
    — or contain — a ``${{ }}`` expression.
    """
    runs_on = job.get("runs-on")
    raw: list[str] = []
    if isinstance(runs_on, dict):
        raw.extend(_flatten(runs_on.get("labels")))
        raw.extend(_flatten(runs_on.get("group")))
    else:
        raw.extend(_flatten(runs_on))

    labels: list[str] = []
    unresolvable = False
    for token in raw:
        expressions = _EXPR_RE.findall(token)
        if not expressions:
            labels.append(token)
            continue
        for expr in expressions:
            resolved = _labels_from_expression(expr, job)
            if resolved:
                labels.extend(resolved)
            else:
                unresolvable = True
    return labels, unresolvable


def classify_runs_on(job: dict) -> str:
    """Return ``SELF_HOSTED`` / ``HOSTED`` / ``UNRESOLVABLE`` for one job.

    Positive proof wins: a label set carrying ``self-hosted`` runs on our
    hardware even if a sibling label happens to name an OS image.
    """
    labels, unresolvable = _runner_labels(job)
    lowered = [label.strip().lower() for label in labels if label and label.strip()]

    if any(label == _SELF_HOSTED_LABEL for label in lowered):
        return SELF_HOSTED
    if any(label.startswith(_HOSTED_PREFIXES) for label in lowered):
        return HOSTED
    if unresolvable or not lowered:
        return UNRESOLVABLE
    # Bare labels naming neither a hosted image nor `self-hosted`
    # (e.g. `[Linux, X64, scitex-ci]`) target our own pool by label.
    return SELF_HOSTED


def _iter_jobs(doc: Any) -> Iterable[tuple[str, dict]]:
    jobs = doc.get("jobs") if isinstance(doc, dict) else None
    if not isinstance(jobs, dict):
        return
    for job_id, job in jobs.items():
        if isinstance(job, dict):
            yield str(job_id), job


def _allowed_jobs(entry: dict) -> set[str] | None:
    """Job ids this entry covers; ``None`` means every job in the file."""
    jobs = entry.get("jobs")
    if jobs is None:
        return None
    return {str(job) for job in _flatten(jobs)}


def load_allowlist(repo: Path) -> tuple[dict[str, dict], list[Violation]]:
    """Parse the allowlist; enforce that every entry ARGUES its case.

    Returns ``({workflow_filename: entry}, violations)``. The mandatory
    ``reason`` is enforced HERE, in the mechanism — an entry without a real
    argument fails the guard exactly like an un-allowlisted hosted runner
    would. That is the whole point: an operator-approved exception that
    lives only in a chat message is a fact written in one place and
    believed in another; the next agent greps, finds the exception, reads
    it as an oversight, and "fixes" it — reopening the hole with the best
    of intentions.
    """
    path = repo / _ALLOWLIST_RELPATH
    if not path.is_file():
        return {}, []
    try:
        doc = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as exc:
        return {}, [
            Violation(
                CODE_ALLOWLIST_NO_REASON,
                str(_ALLOWLIST_RELPATH),
                f"allowlist is unreadable / not valid YAML: {exc}",
            )
        ]

    entries = doc.get("allow") if isinstance(doc, dict) else None
    allow: dict[str, dict] = {}
    violations: list[Violation] = []
    if not isinstance(entries, list):
        return {}, violations

    for entry in entries:
        if not isinstance(entry, dict):
            violations.append(
                Violation(
                    CODE_ALLOWLIST_NO_REASON,
                    str(_ALLOWLIST_RELPATH),
                    f"allowlist entry is not a mapping: {entry!r}",
                )
            )
            continue
        workflow = str(entry.get("workflow") or "").strip()
        if not workflow:
            violations.append(
                Violation(
                    CODE_ALLOWLIST_NO_REASON,
                    str(_ALLOWLIST_RELPATH),
                    f"allowlist entry has no `workflow:` key: {entry!r}",
                )
            )
            continue
        reason = str(entry.get("reason") or "").strip()
        if len(reason) < _MIN_REASON_CHARS:
            violations.append(
                Violation(
                    CODE_ALLOWLIST_NO_REASON,
                    f"{_ALLOWLIST_RELPATH} -> {workflow}",
                    (
                        "allowlist entry needs a `reason:` of at least "
                        f"{_MIN_REASON_CHARS} characters explaining WHY this "
                        "workflow may run on GitHub's hardware. An exception "
                        "without its argument attached is precisely the bug "
                        f"this guard exists to prevent (got {len(reason)} chars)."
                    ),
                )
            )
            continue
        allow[workflow] = entry
    return allow, violations


def _hosted_violation(rel: str, job_id: str, labels: list[str]) -> Violation:
    return Violation(
        CODE_HOSTED,
        f"{rel} -> job `{job_id}`",
        (
            f"runs on a GitHub-HOSTED runner ({', '.join(labels)}). "
            "Move it to the self-hosted pool:\n"
            f"        {_CANONICAL_RUNS_ON}\n"
            "      Hosted runners are FORBIDDEN in this repo (operator, "
            "2026-07-14: mandatory, no exceptions). If the self-hosted pool "
            "is broken, fix the pool — do not escape to a hosted runner. A "
            f"genuine security exception goes in {_ALLOWLIST_RELPATH}, WITH "
            "its argument attached."
        ),
    )


def _unresolvable_violation(rel: str, job_id: str) -> Violation:
    return Violation(
        CODE_UNRESOLVABLE,
        f"{rel} -> job `{job_id}`",
        (
            "`runs-on` cannot be proven to target the self-hosted pool. The "
            "guard reads only what ships in the repo, so an unreadable runner "
            "target is indistinguishable from a deliberate bypass. Use a "
            "literal label list, or the canonical form whose inline default "
            f"names `self-hosted`:\n        {_CANONICAL_RUNS_ON}"
        ),
    )


def _stale_violations(
    repo: Path, allow: dict[str, dict], used: set[tuple[str, str]]
) -> list[Violation]:
    """A dead exception is a live loophole — it silently pre-approves
    whatever that workflow does next. Every entry must still be EARNING
    its place."""
    out: list[Violation] = []
    wf_dir = repo / _WORKFLOWS_RELDIR
    for workflow in allow:
        if not (wf_dir / workflow).is_file():
            out.append(
                Violation(
                    CODE_ALLOWLIST_STALE,
                    f"{_ALLOWLIST_RELPATH} -> {workflow}",
                    (
                        "allowlisted workflow does not exist — delete the stale "
                        "entry. A dead exception is a standing pre-approval for "
                        "whatever takes that filename next."
                    ),
                )
            )
            continue
        if not any(name == workflow for name, _ in used):
            out.append(
                Violation(
                    CODE_ALLOWLIST_STALE,
                    f"{_ALLOWLIST_RELPATH} -> {workflow}",
                    (
                        "allowlisted workflow no longer needs the exception (no "
                        "hosted job left in it) — delete the entry. Keeping it "
                        "leaves a standing exception nobody needs, which the "
                        "next hosted job added to that file would inherit for "
                        "free."
                    ),
                )
            )
    return out


def check_repo(repo: Path) -> list[Violation]:
    """Scan ``<repo>/.github/workflows/``. Pure; never raises."""
    violations: list[Violation] = []
    allow, allow_violations = load_allowlist(repo)
    violations.extend(allow_violations)

    wf_dir = repo / _WORKFLOWS_RELDIR
    if not wf_dir.is_dir():
        return violations

    used: set[tuple[str, str]] = set()

    for path in sorted(wf_dir.iterdir()):
        if not path.is_file() or path.suffix not in {".yml", ".yaml"}:
            continue
        rel = str(path.relative_to(repo))
        try:
            doc = yaml.safe_load(path.read_text(encoding="utf-8"))
        except (OSError, yaml.YAMLError) as exc:
            violations.append(
                Violation(CODE_UNRESOLVABLE, rel, f"cannot parse workflow: {exc}")
            )
            continue

        entry = allow.get(path.name)
        exempt_jobs = _allowed_jobs(entry) if entry is not None else set()

        for job_id, job in _iter_jobs(doc):
            # A `uses:` job calls a reusable workflow and declares no runner
            # of its own — there is no runs-on decision to police here.
            if "runs-on" not in job and job.get("uses"):
                continue

            verdict = classify_runs_on(job)
            if verdict == SELF_HOSTED:
                continue

            if entry is not None and (exempt_jobs is None or job_id in exempt_jobs):
                used.add((path.name, job_id))
                continue

            if verdict == HOSTED:
                labels, _ = _runner_labels(job)
                violations.append(_hosted_violation(rel, job_id, labels))
            else:
                violations.append(_unresolvable_violation(rel, job_id))

    violations.extend(_stale_violations(repo, allow, used))
    return violations


def format_report(repo: Path, violations: list[Violation]) -> str:
    if not violations:
        return (
            f"no-hosted-runners: OK — every job in {_WORKFLOWS_RELDIR} targets "
            "the self-hosted pool (or is allowlisted WITH a stated reason)."
        )
    lines = [
        "",
        "=" * 72,
        "NO-HOSTED-RUNNERS GUARD FAILED",
        "=" * 72,
        "",
        f"{len(violations)} violation(s) in {repo}:",
        "",
    ]
    lines.extend(violation.render() for violation in violations)
    lines.extend(
        [
            "",
            "GitHub-hosted runners are forbidden in this repo (operator "
            "directive, 2026-07-14: mandatory, no exceptions).",
            "If the self-hosted pool is broken, that is OUR problem to fix — "
            "escaping back to a hosted runner is not an option.",
            "=" * 72,
            "",
        ]
    )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    repo = Path(args[0]).resolve() if args else Path.cwd()
    violations = check_repo(repo)
    stream = sys.stderr if violations else sys.stdout
    print(format_report(repo, violations), file=stream)
    return 1 if violations else 0


if __name__ == "__main__":  # pragma: no cover - thin CLI shim
    raise SystemExit(main())
