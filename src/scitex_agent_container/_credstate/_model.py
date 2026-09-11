"""Row types for the credential-state tables, and the rules they carry.

Every ``to_row`` runs :func:`.._material.assert_no_material` over the
whole payload before it can become a database row. That placement is
deliberate: the guard sits on the ONLY path into the store, so there is
no way to write a row that bypasses it — including from a future caller
that has never read this module's docstring.

``row_uuid`` is minted here at construction, not by the database, because
ADR-0022 §5.1 says "minted at insert" and a server-side default would
make the value unknown to the writer until a round trip — which is
exactly when a sync layer needs it.
"""

from __future__ import annotations

import uuid
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime, timezone
from typing import Any

from ._material import assert_no_material

#: Credential kinds this domain knows how to reason about. Open on
#: purpose — an unknown kind is recorded, not rejected, because refusing
#: to record a credential is how it goes back to being invisible.
KIND_OAUTH_SESSION = "oauth_session"
KIND_API_KEY = "api_key"
KIND_BOT_TOKEN = "bot_token"
KIND_FORGE_TOKEN = "forge_token"
KIND_SSH_KEY = "ssh_key"

#: The two tiers of the existing model, named. ``primary_secret`` is
#: material that MUST NOT leave its primary (the OAuth refresh token);
#: ``distributable`` is the access-only artifact that may be pushed to
#: replicas. ``_account.mint_token`` is what turns the first into the
#: second, and it is the only sanctioned conversion.
TIER_PRIMARY_SECRET = "primary_secret"
TIER_DISTRIBUTABLE = "distributable"

ROLE_PRIMARY = "primary"
ROLE_REPLICA = "replica"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _new_uuid() -> str:
    return str(uuid.uuid4())


@dataclass
class _SyncRow:
    """The ADR-0022 §5.1 columns every row in this domain carries."""

    origin_node: str
    row_uuid: str = field(default_factory=_new_uuid)
    revision: int = 1
    updated_at: datetime = field(default_factory=_now)
    deleted_at: datetime | None = None

    def bumped(self, **changes: Any):
        """Return a copy with ``changes`` applied and ``revision`` bumped.

        The owner bumps; nobody else may. ADR-0022 §5.2 rule 4 accepts a
        remote row only when it is a higher revision from the SAME
        origin, so a bump that does not come from the owner cannot be
        distinguished from divergence — and is therefore not offered.
        """
        return replace(self, revision=self.revision + 1, updated_at=_now(), **changes)

    def tombstoned(self):
        """Return a soft-deleted copy. Rows are never ``DELETE``d (§5.2 r5)."""
        return replace(
            self,
            revision=self.revision + 1,
            updated_at=_now(),
            deleted_at=_now(),
        )


@dataclass
class CredentialDescriptor(_SyncRow):
    """WHAT EXISTS AND WHO OWNS IT — one row per credential in the fleet.

    ``primary_node`` is the declaration that today does not exist:
    ``_keepalive_guards.holds_refresh_material`` infers the refresh
    holder from whether a file happens to contain a field. An inference
    cannot be wrong in a useful way — it simply reports whatever the disk
    says, so a second holder appearing looks exactly like the first one.
    A DECLARED primary can be contradicted by the disk, and that
    contradiction is the fault report.
    """

    cred_key: str = ""
    kind: str = KIND_OAUTH_SESSION
    account: str = ""
    tier: str = TIER_PRIMARY_SECRET
    primary_node: str = ""
    generation: int = 1
    minted_at: datetime | None = None
    expires_at: datetime | None = None
    refresh_command: str | None = None
    obtain_command: str | None = None
    note: str | None = None

    def to_row(self) -> dict[str, Any]:
        row = asdict(self)
        assert_no_material(row, what=f"credential descriptor {self.cred_key!r}")
        return row


@dataclass
class CredentialPlacement(_SyncRow):
    """WHERE IT IS SUPPOSED TO BE — one row per (credential, node).

    ``locator`` is a scheme-prefixed REFERENCE and never material:
    ``file:<abs path>`` or ``env:<VARNAME>``. Recording the reference is
    what would have survived the relocation that silently removed the one
    Telegram credential path that actually worked.
    """

    cred_key: str = ""
    node: str = ""
    role: str = ROLE_REPLICA
    required: bool = True
    locator: str = ""
    note: str | None = None

    def to_row(self) -> dict[str, Any]:
        row = asdict(self)
        assert_no_material(
            row, what=f"credential placement {self.cred_key!r} on {self.node!r}"
        )
        return row


@dataclass
class CredentialObservation(_SyncRow):
    """WHAT WAS ACTUALLY SEEN — append-only, authored only by the node itself.

    Never written by a timer and never derived from one. A timer firing
    proves a process ran; it proves nothing about whether the credential
    it touched is usable. Every field here is a measurement of the
    artifact, so the row cannot go green because a schedule did.
    """

    cred_key: str = ""
    node: str = ""
    observed_at: datetime = field(default_factory=_now)
    present: bool = False
    holds_refresh_material: bool | None = None
    file_mode: str | None = None
    artifact_expires_at: datetime | None = None
    generation_seen: int | None = None
    verdict: str = ""
    detail: str | None = None

    def to_row(self) -> dict[str, Any]:
        row = asdict(self)
        assert_no_material(
            row, what=f"credential observation {self.cred_key!r} on {self.node!r}"
        )
        return row
