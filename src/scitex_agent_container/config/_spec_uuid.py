"""Immutable per-spec identity: ``spec.uuid`` generation and first-seen backfill.

A spec's directory name is its operator-facing label, and labels are
MUTABLE — renames, multi-instance suffixes (``live-paper-03``) and stale
copies on peer hosts all reuse them. Routing by name alone cannot tell a
live agent from a stale row that happens to share the name (the live-paper
04/03 collision behind card ``sac-identity-spec-uuid-resolution-20261026``).

``spec.uuid`` is the stable half of that pair: minted once, never changed,
never derived from the spec CONTENT (a content hash would rotate on every
edit and is exactly what ``spec_id`` — the config path / sha recorded in
the birth certificate — already is). The name stays the hinted label; the
uuid is what rows and resolvers key on.

FIRST-SEEN-STABLE. A spec without ``uuid`` gets one the first time sac
touches it for real (``sac agents create`` writes it into the spec; the
start path keeps one in RUNTIME STATE via :func:`runtime_spec_uuid` and
never edits the committed spec — issue #1777), and that value is then stable: this
module never overwrites a present, valid uuid in a spec. A start on a spec
that DOES carry ``uuid`` mirrors it into the same runtime state file
(:func:`record_runtime_spec_uuid`), so removing a host-only stamp line later
keeps the identity; :mod:`._spec_uuid_migrate` performs that removal.
Concurrent first-sights on
two hosts stamp per-FILE values that the fleet sync then reconciles like
any other spec edit — the file is the source of truth, not either host's
memory of it.

Line-level edit, deliberately: a ruamel/pyyaml load+dump round-trip would
reformat 100+ hand-maintained files (see :mod:`._yaml_line_edit` for why
that class of edit is refused here). The insert anchors on the ``spec:``
block and adds one ``uuid:`` child line, touching nothing else.
"""

from __future__ import annotations

import uuid as _uuid
from pathlib import Path

from ._yaml_line_edit import find_block, find_key, parse_key_line, split_ending

__all__ = [
    "RUNTIME_UUID_FILE",
    "backfill_spec_uuids",
    "ensure_spec_uuid",
    "parse_spec_uuid",
    "record_runtime_spec_uuid",
    "resolve_start_spec_uuid",
    "runtime_spec_uuid",
]

#: Where the START path keeps a minted identity for a spec without
#: ``spec.uuid`` (issue #1777): the per-agent runtime state dir, never the
#: committed spec file.
RUNTIME_UUID_FILE = "spec_uuid"


def parse_spec_uuid(value: object, *, path: str = "<spec>") -> str:
    """Validate a ``spec.uuid`` value. Returns the normalised string.

    Absent (``None`` / missing / blank) is ``""`` — uuid is OPTIONAL at
    load so the existing fleet keeps parsing while the backfill lands
    (the same posture ``residency`` and ``to_home_layers`` took). A
    present-but-malformed value is a loud ``ValueError`` naming the
    spec: a half-written uuid that loaded as "no uuid" would silently
    re-stamp a new identity on every start.
    """
    if value is None:
        return ""
    text = str(value).strip().strip("\"'")
    if not text:
        return ""
    try:
        return str(_uuid.UUID(text))
    except (ValueError, AttributeError, TypeError) as exc:
        raise ValueError(
            f"spec.uuid is not a valid UUID in {path}: {value!r}. "
            f"Delete the line to let sac stamp a fresh one, or write a "
            f"canonical uuid (e.g. {mint_spec_uuid()})."
        ) from exc


def mint_spec_uuid() -> str:
    """One fresh random uuid4, canonical string form."""
    return str(_uuid.uuid4())


def ensure_spec_uuid(spec_path: str | Path) -> str:
    """Return the spec's uuid, stamping + persisting one if absent.

    Idempotent: a present, valid uuid is returned untouched (the file is
    not even rewritten). A present-but-invalid uuid REFUSES rather than
    re-stamping — overwriting it would fork the identity every resolver
    already keyed on. Missing ``spec:`` block likewise refuses.
    """
    path = Path(spec_path)
    raw = path.read_text()
    lines = raw.splitlines(keepends=True)
    bodies = [split_ending(line)[0] for line in lines]
    endings = [split_ending(line)[1] or "\n" for line in lines]

    block = find_block(bodies, ("spec",))
    if block is None or block.child_indent is None:
        raise ValueError(
            f"cannot stamp spec.uuid in {path}: no mappable `spec:` block. "
            f"Is this a v3 spec.yaml?"
        )
    existing = find_key(bodies, block.start, block.stop, block.child_indent, "uuid")
    if existing is not None:
        parsed = parse_key_line(bodies[existing])
        value = parsed.value.strip() if parsed is not None else ""
        # parse_spec_uuid refuses on malformed — never silently re-stamp.
        return parse_spec_uuid(value, path=str(path))

    stamped = mint_spec_uuid()
    insert_at = block.key_line + 1
    bodies.insert(insert_at, f"{block.child_indent}uuid: {stamped}")
    endings.insert(insert_at, "\n")
    path.write_text("".join(b + e for b, e in zip(bodies, endings)))
    return stamped


def runtime_spec_uuid(state_dir: str | Path) -> str:
    """The uuid a START uses for a spec that declares none (issue #1777).

    Minted once into ``<state_dir>/spec_uuid`` and reused by every later
    start on this host. Starting an agent must never edit its committed
    spec: the spec-authority gate refuses a dirty spec repo, so a stamp
    written by start N refused start N+1. Authoring paths (``sac agents
    create``, :func:`backfill_spec_uuids`) still write ``spec.uuid`` into
    the spec, where the operator commits it.

    A present-but-malformed state file refuses (same rule as the spec
    value): silently re-minting would fork the identity rows key on.
    """
    import os

    path = Path(state_dir).expanduser() / RUNTIME_UUID_FILE
    if path.is_file():
        return parse_spec_uuid(path.read_text(), path=str(path))
    path.parent.mkdir(parents=True, exist_ok=True)
    stamped = mint_spec_uuid()
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(stamped + "\n")
    try:
        os.link(tmp, path)  # first writer wins; a racing start reads its value
    except FileExistsError:
        return parse_spec_uuid(path.read_text(), path=str(path))
    finally:
        tmp.unlink(missing_ok=True)
    return stamped


def _write_state_uuid(path: Path, value: str) -> None:
    """Atomically replace ``path`` with ``value`` (tmp in the same dir + rename)."""
    import os

    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        tmp.write_text(value + "\n")
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def record_runtime_spec_uuid(state_dir: str | Path, spec_uuid: str) -> str | None:
    """Mirror an AUTHORED ``spec.uuid`` into ``<state_dir>/spec_uuid``.

    Returns the value the state file held before (``None`` when absent), so
    a caller can tell a first record from a correction.

    Why: SAC < 0.40 stamped ``uuid:`` lines into specs at START, and those
    lines are often working-tree-only (never committed). If the identity
    lives ONLY in the spec, deleting such a line makes the next start mint a
    new runtime uuid and the agent's rows fork. Keeping the runtime state
    file in step with the spec means :func:`runtime_spec_uuid` already holds
    the same identity the day the line goes away.

    The spec is authoritative: a state file holding a different (or
    malformed) value is overwritten atomically and the change is logged —
    the start is recording what the spec SAYS, so the spec wins.
    """
    value = parse_spec_uuid(spec_uuid, path="<spec.uuid>")
    if not value:
        raise ValueError("record_runtime_spec_uuid needs a non-empty spec.uuid")
    path = Path(state_dir).expanduser() / RUNTIME_UUID_FILE
    previous: str | None = None
    if path.is_file():
        previous = path.read_text().strip()
        try:
            if parse_spec_uuid(previous, path=str(path)) == value:
                return previous
        except ValueError:  # stx-allow: fallback (reason: a malformed state file is replaced by the authoritative spec value below; the warning goes to the scitex_logging logger scitex_agent_container.config._spec_uuid)
            pass
        _logger().warning(
            "spec.uuid %s differs from runtime state %s (%r); the spec is "
            "authoritative, overwriting the state file",
            value,
            path,
            previous,
        )
    _write_state_uuid(path, value)
    return previous


def resolve_start_spec_uuid(spec_uuid: str, state_dir: str | Path) -> str:
    """The ``spec_uuid`` a START records, keeping runtime state in step.

    An authored ``spec.uuid`` is returned as-is and mirrored into
    ``<state_dir>/spec_uuid`` (:func:`record_runtime_spec_uuid`); a spec
    without one uses the runtime-minted value (:func:`runtime_spec_uuid`).
    """
    if spec_uuid:
        record_runtime_spec_uuid(state_dir, spec_uuid)
        return parse_spec_uuid(spec_uuid)
    return runtime_spec_uuid(state_dir)


def _logger():
    import scitex_logging

    return scitex_logging.getLogger(__name__)


def backfill_spec_uuids(agents_dir: str | Path) -> dict[str, str]:
    """Stamp ``spec.uuid`` into every spec under ``agents_dir`` missing one.

    The one-shot migration half of the card: run once per agents root
    (fleet stopped, so no start races a stamp), then let the fleet sync
    carry the files. Returns ``{agent_name: uuid}`` for the specs it
    touched or confirmed — skipped dirs (no ``spec.yaml``) are absent,
    and an invalid pre-existing uuid ABORTS with the path in the error
    rather than forking that agent's identity mid-migration.
    """
    root = Path(agents_dir)
    out: dict[str, str] = {}
    if not root.is_dir():
        return out
    for sub in sorted(root.iterdir()):
        if not sub.is_dir():
            continue
        spec = sub / "spec.yaml"
        if not spec.is_file():
            continue
        out[sub.name] = ensure_spec_uuid(spec)
    return out
