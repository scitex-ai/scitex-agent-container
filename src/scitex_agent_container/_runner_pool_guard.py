"""Guard: a workflow may not FREEZE its runner pool into the YAML.

The incident this exists to prevent, stated once
================================================
PR #1006 gave five sub-minute single-core jobs their own "light lane" and
wrote the destination as a literal label list::

    runs-on: ["self-hosted", "Linux", "X64", "spartan-cpu"]

The measurement behind it was real and is not in dispute (a 12-second job
was holding one of four 32-core machines on a pool at 93-94% utilisation).
What was wrong was the SEAM, and its own comment said so out loud —
"PINNED PER WORKFLOW rather than through vars.CI_RUNS_ON so the routing is
visible in a diff".

Six days later the ``spartan-cpu`` runners went offline. Five checks —
``ruff``, ``rtd-sphinx-build``, ``import-smoke``, ``no-hosted-runners``,
``scitex-dev-quality-audit`` — queued forever on every pull request while
four ``scitex-org-cpu`` runners sat online and idle. Fifteen PRs could not
merge, and re-pointing them required a code change, review and a merge
through the very gate that was jammed.

So: routing intent is legitimate, and it belongs in DATA. A destination
named by a repository variable is redirected in one click. A destination
frozen in YAML needs a PR through a blocked queue.

What this checks, precisely
===========================
For every job that this repo's other guard classifies as self-hosted, if
``runs-on`` names a POOL-SELECTING label — anything beyond GitHub's own
automatic labels (``self-hosted``, the OS, the architecture) — then the
``runs-on`` text must READ A VARIABLE (``vars.<NAME>``). It is the seam
that is mandatory, not any particular variable.

The canonical spellings::

    runs-on: ${{ fromJSON(vars.CI_RUNS_ON || '["self-hosted","Linux","X64","scitex-ci"]') }}
    runs-on: ${{ fromJSON(vars.LIGHT_RUNS_ON || vars.CI_RUNS_ON || '["self-hosted","Linux","X64","scitex-ci"]') }}

The literal fallback stays REQUIRED, and is enforced elsewhere twice over:
``_hosted_runner_guard`` refuses an unresolvable ``runs-on`` (SAC-CI002),
and scitex-dev's PS-224 reports a bare ``${{ vars.X }}`` as an error for
the same reason. A bare variable reference is not the fix — it trades a
frozen destination for an invisible one.

What this deliberately does NOT check
=====================================
WHICH pool the variable points at. That is a repository-settings value no
static reader can see, and it is precisely the thing that should be
changeable without a commit. This guard enforces only the weaker,
statically-decidable property that makes redirection possible at all.

The exceptions are real, so they are a mechanism
================================================
Two pins in this repo are correct and must survive:

* the Spartan capacity canary, whose ENTIRE PURPOSE is to interrogate one
  named pool — reading the variable would make it test whichever pool is
  already configured, i.e. the question we already know the answer to;
* the CI-verdict job, pinned to the single machine that hosts ``sac
  listen`` and the card store, because its loopback premise is true on
  exactly that box.

Both live in ``.github/runner-pin-allowlist.yaml`` with a mandatory
``reason:``, and a stale entry fails the guard — same shape, and same
argument, as the hosted-runner allowlist next to it: an exception that
lives only in a chat message is a fact written in one place and believed
in another.

Entry points
============
* ``python -m scitex_agent_container._runner_pool_guard [REPO_ROOT]``
  — exit 0 clean, exit 1 on any violation.
* :func:`check_repo` — pure function; returns violations, raises nothing.

Promoting this upstream (the pin is a fleet convention, not a sac quirk)
=======================================================================
Measured 2026-08-12: five sibling repos — scitex-cards, scitex-todo,
scitex-plt, scitex-scholar, scitex-ssh — carry the same literal
``spartan-cpu`` pin and are armed to freeze on their next push. sac lands
first as the proof; the rule belongs in scitex-dev's project auditor after
that, so this module is written to move rather than to stay:

* every check is a PURE function of a ``repo: Path`` — no import-time state,
  no sac-specific knowledge beyond three module constants
  (``_ALLOWLIST_RELPATH``, the ``SAC-CI0xx`` codes, the message text);
* :func:`pool_labels` and :func:`reads_a_variable` are the whole rule and are
  independently callable;
* the ``runs-on`` parsing is already duplicated upstream as
  ``scitex_dev._cli.audit._project._runs_on_parsing.resolve_destination`` —
  a promoted version should call THAT and drop the import below.

What a promotion must NOT do is relax the seam into "must read
``vars.CI_RUNS_ON``": scitex-dev's own PS-224 reports a BARE
``${{ vars.X }}`` as an error, so the only spelling both rules bless is the
fleet idiom WITH its literal fallback. Requiring the variable and requiring
it to be readable are two rules, and each is load-bearing.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import yaml

# TWO IMPORT PATHS FOR ONE MODULE, AND BOTH ARE EXERCISED.
#
# The `runs-on` parser lives in `_hosted_runner_guard` and is reused here
# verbatim — two guards reading one line must read it the SAME way, or they
# will eventually disagree about what a job requested, and the disagreement
# will surface as one of them being quietly wrong.
#
# But the sibling hook runs these guards BY PATH (`python
# src/scitex_agent_container/_runner_pool_guard.py`) so a bare checkout
# without sac installed still gets the check. Executed that way there is no
# package context and the relative import cannot resolve — while `sys.path[0]`
# IS this file's directory, so the sibling is importable by bare name. Neither
# spelling covers both cases; this is not a fallback that hides an error, it
# is the two ways a module can legitimately be entered.
try:  # normal import: `from scitex_agent_container import _runner_pool_guard`
    from ._hosted_runner_guard import (
        HOSTED,
        Violation,
        _flatten,
        _iter_jobs,
        _runner_labels,
        classify_runs_on,
    )
except ImportError:  # executed by path (pre-commit) — no package context
    from _hosted_runner_guard import (  # type: ignore[no-redef]
        HOSTED,
        Violation,
        _flatten,
        _iter_jobs,
        _runner_labels,
        classify_runs_on,
    )

# GitHub applies these to every runner automatically; none of them SELECTS a
# pool. `self-hosted` says "ours, not GitHub's"; the OS and architecture
# labels narrow by capability, not by machine group. A job carrying only
# these reaches whatever self-hosted runner is free, which is the opposite of
# a pin — so it is not this guard's business.
_GENERIC_LABELS = frozenset(
    {
        "self-hosted",
        "linux",
        "windows",
        "macos",
        "x64",
        "x86",
        "arm",
        "arm64",
    }
)

# An allowlist entry must ARGUE its case; a stub ("legacy", "needed") is not
# an argument. Same floor as the hosted-runner allowlist, on purpose.
_MIN_REASON_CHARS = 40

_ALLOWLIST_RELPATH = Path(".github") / "runner-pin-allowlist.yaml"
_WORKFLOWS_RELDIR = Path(".github") / "workflows"

# Violation codes. Numbered on from the hosted guard's SAC-CI001..004 so the
# two guards never collide in a log a human is skimming.
CODE_FROZEN_POOL = "SAC-CI005"  # pool frozen in YAML, no variable seam
CODE_ALLOWLIST_NO_REASON = "SAC-CI006"  # pin allowlisted without an argument
CODE_ALLOWLIST_STALE = "SAC-CI007"  # pin-allowlist entry that no longer applies

_CANONICAL_RUNS_ON = (
    "runs-on: ${{ fromJSON(vars.CI_RUNS_ON || "
    '\'["self-hosted","Linux","X64","scitex-ci"]\') }}'
)


def _raw_runs_on_text(job: dict) -> str:
    """Every ``runs-on`` token of one job, concatenated for substring reads.

    The mapping form (``{group:, labels:}``) and the list form both flatten
    here, so a ``vars.`` reference is found wherever it was written.
    """
    runs_on = job.get("runs-on")
    if isinstance(runs_on, dict):
        tokens = _flatten(runs_on.get("labels")) + _flatten(runs_on.get("group"))
    else:
        tokens = _flatten(runs_on)
    return "\n".join(tokens)


def reads_a_variable(job: dict) -> bool:
    """True when ``runs-on`` resolves its destination through ``vars.``.

    Substring, not a parse, and that is deliberate: every spelling GitHub
    accepts (``vars.X``, ``vars . X`` is not legal, ``fromJSON(vars.X || ...)``,
    a ternary over two variables) contains the token, and a false POSITIVE
    here can only be produced by writing ``vars.`` into a runner label — a
    string no runner carries.
    """
    return "vars." in _raw_runs_on_text(job)


def pool_labels(job: dict) -> list[str]:
    """The pool-SELECTING labels of one job, in declaration order.

    Empty when the job names only GitHub's automatic labels — a job that
    pins nothing has nothing to redirect.
    """
    labels, _ = _runner_labels(job)
    return [
        label.strip()
        for label in labels
        if label and label.strip() and label.strip().lower() not in _GENERIC_LABELS
    ]


def _allowed_jobs(entry: dict) -> set[str] | None:
    """Job ids this entry covers; ``None`` means every job in the file."""
    jobs = entry.get("jobs")
    if jobs is None:
        return None
    return {str(job) for job in _flatten(jobs)}


def load_allowlist(repo: Path) -> tuple[dict[str, dict], list[Violation]]:
    """Parse the pin allowlist; enforce that every entry ARGUES its case.

    Returns ``({workflow_filename: entry}, violations)``. A missing file is
    not an error — a repo with no deliberate pins needs no allowlist.
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
                f"pin allowlist is unreadable / not valid YAML: {exc}",
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
                    f"pin allowlist entry is not a mapping: {entry!r}",
                )
            )
            continue
        workflow = str(entry.get("workflow") or "").strip()
        if not workflow:
            violations.append(
                Violation(
                    CODE_ALLOWLIST_NO_REASON,
                    str(_ALLOWLIST_RELPATH),
                    f"pin allowlist entry has no `workflow:` key: {entry!r}",
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
                        "pin allowlist entry needs a `reason:` of at least "
                        f"{_MIN_REASON_CHARS} characters explaining why THIS "
                        "job must name its pool literally instead of reading a "
                        "variable. The bar is high on purpose: the reason must "
                        "survive that pool going offline, because that is the "
                        "day someone reads it "
                        f"(got {len(reason)} chars)."
                    ),
                )
            )
            continue
        allow[workflow] = entry
    return allow, violations


def _frozen_pool_violation(rel: str, job_id: str, pools: list[str]) -> Violation:
    return Violation(
        CODE_FROZEN_POOL,
        f"{rel} -> job `{job_id}`",
        (
            f"names its runner pool literally ({', '.join(pools)}) instead of "
            "reading it from a repository variable. Route it through the seam:"
            f"\n        {_CANONICAL_RUNS_ON}\n"
            "      (or `vars.LIGHT_RUNS_ON || vars.CI_RUNS_ON || '[...]'` for "
            "the light lane). Keep the literal JSON fallback — a bare "
            "`${{ vars.X }}` trades a frozen destination for an invisible one "
            "and fails SAC-CI002.\n"
            "      WHY: a pool frozen here can only be re-pointed by a PR "
            "through the gate it just jammed. On 2026-08-12 five light-lane "
            "checks pinned to an offline pool held 15 PRs while four runners "
            "sat idle. A pin that is genuinely correct goes in "
            f"{_ALLOWLIST_RELPATH}, WITH its argument attached."
        ),
    )


def _stale_violations(
    repo: Path, allow: dict[str, dict], used: set[tuple[str, str]]
) -> list[Violation]:
    """A dead exception is a live loophole — it pre-approves whatever that
    workflow pins next."""
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
                        "allowlisted workflow no longer pins a pool literally — "
                        "delete the entry. Keeping it leaves a standing "
                        "exception nobody needs, which the next literal pin "
                        "added to that file would inherit for free."
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
            doc: Any = yaml.safe_load(path.read_text(encoding="utf-8"))
        except (OSError, yaml.YAMLError) as exc:
            violations.append(
                Violation(CODE_FROZEN_POOL, rel, f"cannot parse workflow: {exc}")
            )
            continue

        entry = allow.get(path.name)
        exempt_jobs = _allowed_jobs(entry) if entry is not None else set()

        for job_id, job in _iter_jobs(doc):
            # A `uses:` job calls a reusable workflow and declares no runner
            # of its own — there is no destination to police here.
            if "runs-on" not in job and job.get("uses"):
                continue

            # A GitHub-HOSTED job has no pool to redirect, and the other
            # guard already owns it. Reporting it twice would make the two
            # guards argue about the same line in different words.
            if classify_runs_on(job) == HOSTED:
                continue

            pools = pool_labels(job)
            if not pools or reads_a_variable(job):
                continue

            if entry is not None and (exempt_jobs is None or job_id in exempt_jobs):
                used.add((path.name, job_id))
                continue

            violations.append(_frozen_pool_violation(rel, job_id, pools))

    violations.extend(_stale_violations(repo, allow, used))
    return violations


def format_report(repo: Path, violations: list[Violation]) -> str:
    if not violations:
        return (
            f"no-hardcoded-runner-pool: OK — every job in {_WORKFLOWS_RELDIR} "
            "names its pool through a repository variable (or is allowlisted "
            "WITH a stated reason)."
        )
    lines = [
        "",
        "=" * 72,
        "HARDCODED-RUNNER-POOL GUARD FAILED",
        "=" * 72,
        "",
        f"{len(violations)} violation(s) in {repo}:",
        "",
    ]
    lines.extend(violation.render() for violation in violations)
    lines.extend(
        [
            "",
            "A runner pool frozen into a workflow can only be re-pointed by a "
            "pull request — through the CI gate that the dead pool just "
            "jammed. Name the pool in a repository VARIABLE so a pool going "
            "offline is a settings change, not a code change.",
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
