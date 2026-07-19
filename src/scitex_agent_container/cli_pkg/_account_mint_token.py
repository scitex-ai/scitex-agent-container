"""``sac accounts mint-token`` — master-side ACCESS-ONLY credential minting.

Emits ONE wrapped ``{artifact, meta}`` JSON envelope on stdout for the
named stored account. The artifact carries the OAuth ``accessToken`` (the
distributable) but the ``refreshToken`` is STRIPPED — so a compute host
that consumes the artifact can never trigger the single-use refresh-token
rotation that would invalidate the master's token.

Fails loudly (non-zero exit) on an unknown label or an unhealthy/expired
credential; a dead token is NEVER minted. Lives in its own module (like
``_account_refresh`` / ``_account_sync_live``) to keep ``account_group``
under the per-file line cap; attached onto the group at import time.
"""

from __future__ import annotations

import click


def register_mint_token_command(group: click.Group) -> None:
    """Attach the ``mint-token`` subcommand onto ``group``."""

    @group.command("mint-token")
    @click.option(
        "--account",
        "account_label",
        required=True,
        help="Stored account slug to mint from (e.g. alpha-example-com).",
    )
    def account_mint_token(account_label: str) -> None:
        """Mint an ACCESS-ONLY credential artifact (refresh_token stripped).

        Reads the CURRENT stored ``.credentials.json`` for the account
        (mint-on-demand — freshest token), gates on health, strips the
        refreshToken, and emits ONE JSON envelope on stdout::

            {"artifact": {"claudeAiOauth": {"accessToken", "expiresAt",
                                            "scopes"}},
             "meta": {"account", "master_host", "minted_at", "expires_at",
                      "artifact": "access-only", "artifact_version": 1}}

        \b
        Examples:
          $ sac accounts mint-token --account alpha-example-com
        """
        import json as _json
        import sys

        from .._account.mint_token import MintError, mint_access_only_artifact

        try:
            envelope = mint_access_only_artifact(account_label)
        except MintError as exc:
            click.echo(f"error: {exc}", err=True)
            sys.exit(1)

        # The envelope contains the accessToken by design; the refreshToken
        # is structurally absent. Never log either — only emit the envelope.
        click.echo(_json.dumps(envelope, ensure_ascii=False, indent=2))


__all__ = ["register_mint_token_command"]
