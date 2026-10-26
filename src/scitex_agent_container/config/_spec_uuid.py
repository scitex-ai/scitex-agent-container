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
touches it for real (``sac agents create`` writes it; the start path stamps
it via :func:`ensure_spec_uuid`), and that value is then stable: this
module never overwrites a present, valid uuid. Concurrent first-sights on
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

__all__ = ["backfill_spec_uuids", "ensure_spec_uuid", "parse_spec_uuid"]


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
    existing = find_key(
        bodies, block.start, block.stop, block.child_indent, "uuid"
    )
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
