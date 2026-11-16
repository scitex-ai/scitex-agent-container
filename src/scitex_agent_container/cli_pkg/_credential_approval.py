"""Explicit CLI admission for credential distribution, before backend lookup."""

from collections.abc import Sequence

import click

from ._credential_preview import validate_account_selection, validate_optional_peers


def require_distribution_approval(
    *,
    yes: bool,
    account_labels: Sequence[str],
    all_accounts: bool,
    peers: Sequence[str],
    optional_peers: Sequence[str],
) -> None:
    """Refuse unapproved distribution after pure argument validation."""
    if yes:
        return
    validate_account_selection(account_labels, all_accounts)
    validate_optional_peers(peers, optional_peers)
    raise click.UsageError(
        "Credential distribution requires --yes/-y; use --dry-run to preview arguments."
    )
