"""Credentials as DATABASE STATE — facts recorded, material never.

Why this exists
===============
Three incidents, one root cause. Credentials are currently *facts about a
filesystem*: discoverable only by looking, unrecorded, and lost or
duplicated by any host change.

1. Eight subagents died simultaneously on expired credentials. At that
   moment ``sac.accounts-refresh.timer`` had fired 23 minutes earlier and
   was scheduled normally. **A timer running is not evidence that a token
   is usable**, and nothing in the system held the actual fact.
2. The operator's Telegram channel vanished during a relocation. The only
   working credential path was a token folded into ``$HOME/.env`` by an
   ``.envrc`` cascade. Relocation carries neither ``.env`` nor credential
   material — correctly — so the one path that worked disappeared at the
   moment of the move, and nothing recorded that it had been the path.
3. A forge token sits in plaintext in a ``~/.bashrc``, found by looking.

The database records the FACTS. It does not record the MATERIAL.

The exposure question, argued
=============================
The tempting design is to put the secret in the row so any host can
self-serve. It is rejected, on four grounds:

**1. It does not address any of the three incidents.** This is decisive
on its own. In (1) the token existed and was reachable; what was missing
was the fact that it was *unusable*. In (2) the material was intact on
the old host; what was lost was the *knowledge that this was the working
path*. In (3) the material is present and working; the defect is that
nobody *declared* it. All three are knowledge failures. Storing bytes
fixes none of them; storing facts fixes all three. A design that adds
exposure while addressing none of the motivating failures is a strictly
worse trade, whatever else it offers.

**2. Blast radius follows replication, and these rows are built to
replicate.** ADR-0022 §5 exists to make them travel. Four clones of the
cards store exist right now. A secret in a synced table is a secret in
every store it reaches, in each of their WALs — postgres never overwrites
in place, so a rotated secret persists in the heap until VACUUM — and in
every base backup taken meanwhile, permanently. Rotation cannot retract
it. File exposure is bounded and auditable with ``stat``; this is
neither.

**3. It would silently repeal the two-tier invariant rather than preserve
it.** Today's guarantee is structural: ``_account.mint_token`` strips
``refreshToken`` by construction and ``_keepalive_guards.assert_access_only``
re-scans the payload at every depth before it leaves the host, on the
stated grounds that "a guard that only runs when the stripper is correct
guards nothing". That guard sits on ONE rail — the ssh push. A table
holding material opens a SECOND rail that the guard does not sit on, and
it is the quieter one: ``assert_access_only`` raises; ``INSERT`` does
not. The invariant would remain true of the rail nobody leaks through
and become false of the new one.

**4. What the reference model costs is recoverable; what a leak costs is
not.** The only thing material-in-database buys is a replica serving
itself without contacting its primary. A purpose-built rail for that
already exists — ``keepalive_push``, which verifies HTTP 200, publishes
at mode 0600, and refuses to downgrade a working remote credential.
Replacing a verified push with an unverified pull is a downgrade sold as
a feature.

So a row is a **credential descriptor**: which account, which node is
primary, which tier, when minted, when it expires, and a *locator* — a
scheme-prefixed reference (``file:<abs path>``, ``env:<VARNAME>``) that
makes the material findable, refreshable and auditable without being
present.

**This is enforced, not conventional.** :mod:`._material` scans every row
on the only path into the store — by field name and by value shape — and
REFUSES the write if anything is secret-shaped. It is the same guard as
``assert_access_only``, re-erected on the new rail, for the same reason.

What is deliberately NOT stored
-------------------------------
No hash or fingerprint of the material, either. A digest in a replicating
table is a verifier that travels to every host that receives the row, and
it buys only local drift detection ("did something replace this file
behind my back") — a question this design answers with ``generation``
instead, which leaks nothing. If drift detection later needs more, the
right shape is a node-local column excluded from the sync projection, not
a digest that replicates.

The invariant, made checkable
=============================
CR-001 — *exactly one refresh timer per account; more than one is mutual
invalidation* — is today conventional, and worse, it is invisible.
``_keepalive_guards.holds_refresh_material`` infers the holder from
whether a file happens to contain a field, so a second holder appearing
looks exactly like the first one. An inference cannot contradict itself.

Declaring ``primary_node`` makes it a fact that can be WRONG, and both
halves of the check then become mechanical:

* the fleet half — a SQL query, :data:`._schema.CR001_MULTIPLE_PRIMARIES_SQL`;
* the disk half — declared role versus observed refresh material,
  :func:`._verdict.check_single_refresher`, which names
  ``EXTRA_REFRESHER`` out loud.

Nothing here goes green because a schedule ran. Every verdict is a
comparison against a measurement of the artifact itself.
"""

from __future__ import annotations

from ._contract import (
    CONFLICT_CLASSES,
    SYNC_COLUMNS,
    SyncContractError,
    assert_sync_contract,
    sync_columns_sql,
)
from ._material import CredentialMaterialError, assert_no_material, find_material
from ._model import (
    ROLE_PRIMARY,
    ROLE_REPLICA,
    TIER_DISTRIBUTABLE,
    TIER_PRIMARY_SECRET,
    CredentialDescriptor,
    CredentialObservation,
    CredentialPlacement,
)
from ._observe import LocalObservation, observe_locator, parse_locator
from ._schema import (
    CR001_MULTIPLE_PRIMARIES_SQL,
    DIVERGENT_DECLARATIONS_SQL,
    TABLES,
    assert_schema_contract,
)
from ._verdict import (
    ABSENT,
    EXPIRED,
    EXPIRING,
    EXTRA_REFRESHER,
    NO_REFRESHER,
    OK,
    UNDECLARED,
    UNRESOLVABLE,
    WORLD_READABLE,
    Finding,
    assess,
    check_single_refresher,
    undeclared_findings,
    worst_severity,
)

__all__ = [
    "ABSENT",
    "CONFLICT_CLASSES",
    "CR001_MULTIPLE_PRIMARIES_SQL",
    "DIVERGENT_DECLARATIONS_SQL",
    "EXPIRED",
    "EXPIRING",
    "EXTRA_REFRESHER",
    "NO_REFRESHER",
    "OK",
    "ROLE_PRIMARY",
    "ROLE_REPLICA",
    "SYNC_COLUMNS",
    "TABLES",
    "TIER_DISTRIBUTABLE",
    "TIER_PRIMARY_SECRET",
    "UNDECLARED",
    "UNRESOLVABLE",
    "WORLD_READABLE",
    "CredentialDescriptor",
    "CredentialMaterialError",
    "CredentialObservation",
    "CredentialPlacement",
    "Finding",
    "LocalObservation",
    "SyncContractError",
    "assert_no_material",
    "assert_schema_contract",
    "assert_sync_contract",
    "assess",
    "check_single_refresher",
    "find_material",
    "observe_locator",
    "parse_locator",
    "sync_columns_sql",
    "undeclared_findings",
    "worst_severity",
]
