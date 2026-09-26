"""Write the ``spec.engines`` sweep, and undo it unless nothing moved.

Split out of :mod:`._engines_migration`, which now owns only the PLAN. The
two halves answer different questions — "what would this do?" reads files,
"do it" rewrites them and must be able to put every one back — and the
apply half is where every failure mode with teeth lives:

**THE GATE IS A MEASUREMENT, NOT AN ARGUMENT.** The edit restates the
backend a spec already declares, so it cannot change what an agent starts
on. That is the argument, and an argument has never stopped a bulk edit
from being wrong. Every selected spec is loaded through the production
loader BEFORE the write and again after, and unless the effective backend
is identical for every one, every original is restored. A gate is only
worth its exit code if it measures the fields the edit can actually move —
see :func:`_backend_snapshot`, which was measuring four of six.

**A WRITE EITHER LANDS OR DOES NOT.** ``Path.write_text`` truncates before
it writes, so an interrupted write leaves half a spec. Every write here is
a temp file plus ``os.replace``.

**A FAILED BATCH IS ROLLED BACK, NOT ABANDONED.** Measured: one spec at
mode 444 part-way through a batch raised out of the apply, through the
Click callback, and exited 1 on a traceback — two specs rewritten, four
untouched, the archive taken and never consulted, and no report of either.
"""

from __future__ import annotations

import os
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path

__all__ = ["ApplyResult", "apply_engines_migration"]


@dataclass(frozen=True)
class ApplyResult:
    """What the apply actually did."""

    written: "tuple[str, ...]" = ()
    #: The same writes, as PATHS. ``written`` carries agent NAMES, which is
    #: what a human reads — and a name cannot answer "which root did that
    #: land in?". The default sweep searches several roots, one of which is
    #: the container's own ``$HOME``, so an agent name is not enough to tell
    #: a fleet spec from a stray one under a root nobody meant to write.
    written_paths: "tuple[str, ...]" = ()
    archive_dir: "Path | None" = None
    applied: bool = False
    refused: str = ""
    rolled_back: str = ""
    drift: "tuple[str, ...]" = ()
    #: I/O failures — the write that could not happen, and any restore that
    #: could not undo it. Separate from ``drift`` because "the backend moved"
    #: and "the disk said no" need different responses from a human.
    errors: "tuple[str, ...]" = ()


def _backend_snapshot(path: Path):
    """The effective backend this spec resolves to, through the real loader.

    EVERY FIELD THE EDIT COULD MOVE, not only the ones it means to keep.
    ``claude.model`` alone was measured and it is the ENGINE-RESOLVED value,
    so it came back identical while the top-level ``config.model`` and the
    container-injected ``SCITEX_AGENT_CONTAINER_MODEL`` — both derived from
    the RAW ``spec.claude.model`` this edit empties — flipped to the
    ``sonnet`` default on 117 of 119 specs. The gate certified zero drift
    over a fleet-wide misreport. A gate that measures only what the author
    expected to change is an argument wearing a measurement's clothes.
    """
    from ..config import load_config
    from ..config._parsers import MODEL_ENV_KEY

    config = load_config(path)
    claude = getattr(config, "claude", None)
    provider = getattr(claude, "provider", None)
    endpoint = None
    if provider is not None:
        endpoint = (
            str(getattr(provider, "base_url", "") or ""),
            str(getattr(provider, "auth_token_env", "") or ""),
        )
    env = getattr(config, "env", None) or {}
    return (
        str(getattr(config, "harness", "") or ""),
        str(getattr(claude, "model", "") or ""),
        endpoint,
        str(getattr(claude, "account", "") or ""),
        str(getattr(config, "model", "") or ""),
        str(env.get(MODEL_ENV_KEY, "")),
    )


def _snapshot_all(paths):
    """``(snapshots, unmeasurable)`` — a spec that will not load is named."""
    snapshots: dict[str, object] = {}
    unmeasurable: list[str] = []
    for path in paths:
        try:
            snapshots[str(path)] = _backend_snapshot(path)
        except Exception as exc:  # stx-allow: fallback (reason: a spec the loader rejects is UNMEASURABLE, the honest third value; enumerating loader exception types would turn any new one into a crash mid-sweep)
            unmeasurable.append(f"{path.parent.name}: {type(exc).__name__}: {exc}")
    return snapshots, unmeasurable


def _write_atomically(path: Path, text: str) -> None:
    """Replace ``path``'s contents with ``text`` — all of it, or none of it.

    ``Path.write_text`` TRUNCATES the target before it writes a byte, so an
    ENOSPC or a disconnect part-way leaves HALF a spec on disk and nothing to
    fall back on but the archive. A temp file beside the target plus
    ``os.replace`` means the file is either the old bytes or the new ones.

    ``newline=""`` for the reason ``_engines_migration.read_spec_text``
    states: the editor already chose this file's line endings, and letting
    the writer translate them again rewrites every line of a CRLF spec.
    """
    path = Path(path)
    fd, tmp_name = tempfile.mkstemp(
        dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp"
    )
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        shutil.copymode(path, tmp)
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def _restore(targets, archive_dir: Path) -> "list[str]":
    """Copy every original back. Returns the restores that FAILED, by name.

    A rollback that raises part-way is the same failure one level up: the
    remaining originals stay unrestored and the exception carries no list of
    which ones. So every copy is attempted and the casualties are NAMED.
    """
    failures: list[str] = []
    for outcome in targets:
        try:
            shutil.copy2(archive_dir / f"{outcome.agent}.spec.yaml", outcome.path)
        except (
            OSError,
            shutil.Error,
        ) as exc:  # stx-allow: fallback (reason: one spec that cannot be restored must not abandon the rest; it is named into the returned failures list, which becomes MigrationApply.errors and is reported on stdout by `sac agents migrate-engines`)
            failures.append(f"{outcome.agent}: could not be restored: {exc}")
    return failures


def _archive(targets, archive_dir: Path) -> str:
    """Copy every original into ``archive_dir``. Returns "" or a refusal."""
    try:
        archive_dir.mkdir(parents=True, exist_ok=True)
        for outcome in targets:
            shutil.copy2(outcome.path, archive_dir / f"{outcome.agent}.spec.yaml")
    except (
        OSError,
        shutil.Error,
    ) as exc:  # stx-allow: fallback (reason: no archive means no copy-back, so nothing may be written; refuse before the first byte)
        return (
            f"the originals could not be archived to {archive_dir}, so an undo "
            f"would be a reconstruction rather than a copy-back. Nothing was "
            f"written: {type(exc).__name__}: {exc}"
        )
    return ""


def apply_engines_migration(plan, archive_dir: Path) -> ApplyResult:
    """Archive, write, re-measure, and undo unless the backends are identical."""
    targets = list(plan.migrated)
    if not targets:
        return ApplyResult(applied=True)
    paths = [o.path for o in targets]

    before, unmeasurable = _snapshot_all(paths)
    if unmeasurable:
        # Refuse BEFORE writing. The gate would catch this afterwards too, but
        # writing N files to learn something knowable beforehand is a rollback
        # waiting to be needed, not a safety property.
        return ApplyResult(
            refused=(
                f"{len(unmeasurable)} spec(s) could not be loaded BEFORE the "
                f"sweep, so no post-write comparison could prove them "
                f"unchanged: " + "; ".join(unmeasurable)
            )
        )

    archive_dir = Path(archive_dir)
    refusal = _archive(targets, archive_dir)
    if refusal:
        return ApplyResult(refused=refusal, errors=(refusal,))

    written: list = []
    failure = ""
    for outcome in targets:
        try:
            _write_atomically(outcome.path, outcome.new_text or "")
        except (
            OSError,
            UnicodeError,
        ) as exc:  # stx-allow: fallback (reason: a failed write must roll the batch back, not abort the process mid-sweep on a traceback)
            failure = f"{outcome.agent}: {type(exc).__name__}: {exc}"
            break
        written.append(outcome)
    if failure:
        restore_failures = _restore(written, archive_dir)
        return ApplyResult(
            archive_dir=archive_dir,
            rolled_back=(
                f"the write failed part-way through the batch, so the "
                f"{len(written)} spec(s) already written were restored from "
                f"{archive_dir}"
            ),
            errors=(failure, *restore_failures),
        )

    after, still_unmeasurable = _snapshot_all(paths)
    drift = [
        f"{Path(key).parent.name}: {before[key]!r} -> {after[key]!r}"
        for key in before
        if key in after and after[key] != before[key]
    ]
    if still_unmeasurable or drift:
        restore_failures = _restore(targets, archive_dir)
        return ApplyResult(
            archive_dir=archive_dir,
            rolled_back=(
                f"{len(drift)} spec(s) changed backend and "
                f"{len(still_unmeasurable)} stopped loading; every original was "
                f"restored from {archive_dir}"
            ),
            drift=tuple(drift + still_unmeasurable),
            errors=tuple(restore_failures),
        )
    return ApplyResult(
        written=tuple(o.agent for o in targets),
        written_paths=tuple(str(o.path) for o in targets),
        archive_dir=archive_dir,
        applied=True,
    )
