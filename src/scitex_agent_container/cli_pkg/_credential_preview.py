"""Pure argument admission and plans for access-only credential distribution."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import click


def validate_account_selection(
    account_labels: Sequence[str], all_accounts: bool
) -> None:
    """Retain the command's exactly-one selection requirement."""
    if bool(account_labels) == all_accounts:
        raise click.UsageError(
            "pass exactly one of --account <slug> (repeatable) or --all."
        )


def validate_optional_peers(
    peers: Sequence[str], optional_peers: Sequence[str]
) -> None:
    """Optional peers must also be named targets, as in ordinary distribution."""
    unknown = sorted(set(optional_peers) - set(peers))
    if unknown:
        raise click.UsageError(
            "--optional-peer names host(s) absent from --to: "
            f"{', '.join(unknown)}. An optional peer must also "
            "be a target, or the declaration applies to nothing."
        )


def credential_distribution_preview(
    *,
    account_labels: Sequence[str],
    all_accounts: bool,
    peers: Sequence[str],
    min_validity: int | None,
    remote_path: str | None,
    sweep: bool,
    optional_peers: Sequence[str],
) -> dict[str, Any]:
    """Report supplied arguments without discovering accounts or contacting peers."""
    validate_account_selection(account_labels, all_accounts)
    validate_optional_peers(peers, optional_peers)
    return {
        "action": "send-credentials",
        "dry_run": True,
        "scope": "arguments_only",
        "accounts": list(account_labels),
        "all_accounts": all_accounts,
        "peers": list(peers),
        "optional_peers": list(optional_peers),
        "min_validity": min_validity,
        "remote_path": remote_path,
        "sweep": sweep,
        "unverified": [
            "refresh-holder and account discovery",
            "account pause state and credential validity",
            "backend defaults when arguments are null",
            "peer topology and remote destinations",
            "publication, peer verification and sweep readiness",
        ],
    }
