"""The credential-state tables, and the conflict rule each one lives under.

Three tables, and the split between them is the design:

``credential_descriptor`` — WHAT EXISTS AND WHO OWNS IT.
    One row per credential in the fleet. Identity, kind, account, which
    node is PRIMARY, which tier, when it expires, and how to obtain or
    renew it. This is the row that today does not exist anywhere: the
    fleet's refresh-holder is currently *inferred from disk* by
    ``_keepalive_guards.holds_refresh_material`` — "is a refreshToken
    field present in this file" — so primacy is a property of a
    filesystem, discoverable only by looking, and true of whichever host
    happens to have the bytes. Declaring it makes it a fact that can be
    wrong, which is the entire point: a fact that can be wrong can be
    CHECKED against the disk. An inference cannot disagree with itself.

``credential_placement`` — WHERE IT IS SUPPOSED TO BE.
    One row per (credential, node): does this node need it, at what
    locator, in what role. This is the row that would have survived the
    Telegram relocation — the working path was a token folded into
    ``$HOME/.env`` by an ``.envrc`` cascade, and nothing anywhere
    recorded that this was the working path, so the move silently took
    it away.

``credential_observation`` — WHAT WAS ACTUALLY SEEN, AND WHEN.
    Append-only. A node records what it found on its own disk. Separate
    from placement on purpose: declaration and measurement must be
    different rows so that the two can DISAGREE, out loud. Eight
    subagents died on expired credentials while the refresh timer was
    green and scheduled; the system held no row anywhere saying what the
    token actually was.

Conflict classes (ADR-0022 §5.2), and why each was chosen
---------------------------------------------------------
* descriptor + placement → ``shared_mutable``. Any host may edit them,
  and primacy HANDOVER is exactly the edit whose conflicts must never be
  auto-merged: a blind last-write-wins here would let a clock-skewed
  replica seize primacy and produce two refreshers — the precise
  mutual-invalidation failure the whole two-tier model exists to prevent.
  So: accept a remote row only when it is a newer revision from that
  row's own owner; otherwise DO NOTHING and record a divergence.
* observation → ``log``. Append-only union with ``origin_node`` in the
  PRIMARY KEY, so two nodes can never author the same row and a
  collision provably means the identical row arrived twice.

Nothing in any of these tables is a secret. See :mod:`._material` for the
write-time guard that keeps it that way, and this package's
``__init__`` for why a reference beats the material.
"""

from __future__ import annotations

from ._contract import assert_sync_contract, sync_columns_sql

_SYNC = sync_columns_sql()

#: WHAT EXISTS AND WHO OWNS IT. ``shared_mutable`` — see module docstring.
#:
#: ``generation`` is deliberately NOT ``revision``. ``revision`` is the
#: ROW's version (bumped by any metadata edit); ``generation`` is the
#: MATERIAL's version (bumped only when the credential is minted or
#: rotated). Conflating them would make "somebody fixed a typo in the
#: note" indistinguishable from "the token was rotated" — and the second
#: is the one that invalidates every replica.
DESCRIPTOR_DDL = f"""
CREATE TABLE IF NOT EXISTS credential_descriptor (
{_SYNC}
    cred_key        TEXT        NOT NULL,
    kind            TEXT        NOT NULL,
    account         TEXT        NOT NULL,
    tier            TEXT        NOT NULL,
    primary_node    TEXT        NOT NULL,
    generation      BIGINT      NOT NULL DEFAULT 1,
    minted_at       TIMESTAMPTZ NULL,
    expires_at      TIMESTAMPTZ NULL,
    refresh_command TEXT        NULL,
    obtain_command  TEXT        NULL,
    note            TEXT        NULL,
    PRIMARY KEY (row_uuid),
    CONSTRAINT credential_descriptor_ident UNIQUE (cred_key, origin_node),
    CONSTRAINT credential_descriptor_tier CHECK (
        tier IN ('primary_secret', 'distributable')
    )
)
"""

#: WHERE IT IS SUPPOSED TO BE. ``shared_mutable``.
#:
#: ``locator`` is a scheme-prefixed reference, never material:
#: ``file:/home/agent/.claude/.credentials.json``, ``env:CCT_BOT_TOKEN_3``.
PLACEMENT_DDL = f"""
CREATE TABLE IF NOT EXISTS credential_placement (
{_SYNC}
    cred_key   TEXT    NOT NULL,
    node       TEXT    NOT NULL,
    role       TEXT    NOT NULL,
    required   BOOLEAN NOT NULL DEFAULT TRUE,
    locator    TEXT    NOT NULL,
    note       TEXT    NULL,
    PRIMARY KEY (row_uuid),
    CONSTRAINT credential_placement_ident UNIQUE (cred_key, node, origin_node),
    CONSTRAINT credential_placement_role CHECK (role IN ('primary', 'replica'))
)
"""

#: WHAT WAS ACTUALLY SEEN. ``log`` — append-only, origin_node in the PK.
#:
#: ``holds_refresh_material`` is the one-bit measurement that makes the
#: single-refresher invariant checkable: compare it against the
#: descriptor's ``primary_node`` and a disagreement is a named fault
#: rather than an outage nobody can see.
OBSERVATION_DDL = f"""
CREATE TABLE IF NOT EXISTS credential_observation (
{_SYNC}
    cred_key               TEXT        NOT NULL,
    node                   TEXT        NOT NULL,
    observed_at            TIMESTAMPTZ NOT NULL,
    present                BOOLEAN     NOT NULL,
    holds_refresh_material BOOLEAN     NULL,
    file_mode              TEXT        NULL,
    artifact_expires_at    TIMESTAMPTZ NULL,
    generation_seen        BIGINT      NULL,
    verdict                TEXT        NOT NULL,
    detail                 TEXT        NULL,
    PRIMARY KEY (origin_node, cred_key, node, observed_at)
)
"""

INDEX_DDL: tuple[str, ...] = (
    "CREATE INDEX IF NOT EXISTS idx_credential_descriptor_key "
    "ON credential_descriptor (cred_key) WHERE deleted_at IS NULL",
    "CREATE INDEX IF NOT EXISTS idx_credential_placement_node "
    "ON credential_placement (node) WHERE deleted_at IS NULL",
    "CREATE INDEX IF NOT EXISTS idx_credential_observation_recent "
    "ON credential_observation (cred_key, node, observed_at DESC)",
)

#: Every table in this domain with the ADR-0022 §5.2 class it lives under.
#: The pairing is the schema's contract with the sync layer; changing a
#: class without changing the merge code is how silent divergence starts.
TABLES: tuple[tuple[str, str, str], ...] = (
    ("credential_descriptor", DESCRIPTOR_DDL, "shared_mutable"),
    ("credential_placement", PLACEMENT_DDL, "shared_mutable"),
    ("credential_observation", OBSERVATION_DDL, "log"),
)


def assert_schema_contract() -> None:
    """Verify every table here satisfies ADR-0022 §5.1 for its class.

    Called at import of :mod:`._store` and directly by the test suite. It
    is cheap and it fails closed, so a table that drifts out of contract
    stops the process that would have created it rather than quietly
    creating a row that can never be synchronised.
    """
    for _name, ddl, conflict_class in TABLES:
        assert_sync_contract(ddl, conflict_class=conflict_class)


#: The CR-001 query. Exactly one node may be PRIMARY for a credential;
#: more than one is mutual invalidation, because an OAuth refresh rotates
#: the refresh token and whichever host refreshes first silently revokes
#: the other.
#:
#: This is the half of CR-001 that a database can answer. The other half
#: — "and the node that IS primary is the only one holding refresh
#: material" — is a disk fact, checked in :mod:`._verdict` against
#: ``credential_observation``. Both halves are needed: the query catches
#: a mis-DECLARED fleet, the disk check catches an undeclared second
#: holder, and the second is what actually happened.
CR001_MULTIPLE_PRIMARIES_SQL = """
SELECT cred_key,
       count(DISTINCT primary_node) AS primary_count,
       string_agg(DISTINCT primary_node, ', ' ORDER BY primary_node) AS nodes
  FROM credential_descriptor
 WHERE deleted_at IS NULL
 GROUP BY cred_key
HAVING count(DISTINCT primary_node) > 1
"""

#: Divergence detector for the shared-mutable tables: the same credential
#: declared by two different origins. Not automatically an error — it is
#: what a handover looks like mid-flight — but it must be REPORTED, per
#: ADR-0022 §5.2 rule 6 (ambiguity resolves to DO NOTHING plus a report).
DIVERGENT_DECLARATIONS_SQL = """
SELECT cred_key,
       count(*) AS declarations,
       string_agg(DISTINCT origin_node, ', ' ORDER BY origin_node) AS origins
  FROM credential_descriptor
 WHERE deleted_at IS NULL
 GROUP BY cred_key
HAVING count(DISTINCT origin_node) > 1
"""
