"""``sac creds`` — the mechanical answer to "do I have what I need to act?"

Today that question is answered by a human looking at files. These
commands answer it from declared state plus a live measurement of this
host's disk, and they answer it LOUDLY: a fault exits non-zero and names
both the problem and where the fix comes from.

Three verbs:

``sac creds status``
    What this node needs, what it actually has, and what is missing.
    Records each measurement back to the store, so the fleet accumulates
    the fact that nobody held when eight subagents died on a token whose
    refresh timer was green.

``sac creds check``
    The fleet-wide invariant pass — CR-001 (exactly one primary per
    credential) and the divergence report. Read-only.

``sac creds declare``
    Record a credential that exists but is described nowhere. This is the
    verb that would have kept the relocated Telegram path from vanishing
    without trace.

None of these ever prints, logs or copies credential material.
"""

from __future__ import annotations

import json

import click

from .._credstate import _store
from .._credstate._model import (
    ROLE_PRIMARY,
    ROLE_REPLICA,
    TIER_DISTRIBUTABLE,
    TIER_PRIMARY_SECRET,
    CredentialDescriptor,
    CredentialObservation,
    CredentialPlacement,
)
from .._credstate._observe import observe_locator
from .._credstate._verdict import (
    SEVERITY_FAULT,
    SEVERITY_WARN,
    assess,
    check_single_refresher,
    worst_severity,
)

_MARK = {SEVERITY_FAULT: "FAULT", SEVERITY_WARN: "WARN ", "ok": "OK   "}


def _node(explicit: str | None) -> str:
    from .._state.state_db_hostname import resolve_host

    return resolve_host(explicit)


def _open(dsn: str | None):
    """Open the store, or fail loudly. An unreachable store is not 'clean'."""
    try:
        return _store.open_store(dsn)
    except Exception as exc:  # noqa: BLE001 — the reason must reach the operator
        raise click.ClickException(
            f"cannot reach the credential-state store ({type(exc).__name__}: "
            f"{exc}). This is NOT the same as 'no credentials are missing' — "
            f"nothing was checked. Set {_store.DSN_ENV} to this host's "
            f"postgres on 55432."
        ) from exc


@click.group("creds")
def creds() -> None:
    """Credential state: what this host needs, has, and is missing."""


@creds.command("status")
@click.option("--node", default=None, help="Node to report on (default: this host).")
@click.option("--dsn", default=None, help="State DSN override.")
@click.option("--json", "as_json", is_flag=True, help="Machine-readable output.")
@click.option(
    "--no-record",
    is_flag=True,
    help="Do not write the measurements back to the store.",
)
def status(node: str | None, dsn: str | None, as_json: bool, no_record: bool) -> None:
    """Do I have what I need to act, and if not, where does it come from?"""
    here = _node(node)
    conn = _open(dsn)
    try:
        by_key = {d["cred_key"]: d for d in _store.descriptors(conn)}
        placements = _store.placements_for(conn, here)
        findings = []
        for row in placements:
            placement = CredentialPlacement(
                origin_node=row["origin_node"],
                cred_key=row["cred_key"],
                node=row["node"],
                role=row["role"],
                required=row["required"],
                locator=row["locator"],
            )
            observation = observe_locator(placement.locator)
            descriptor = _as_descriptor(by_key.get(placement.cred_key))
            found = assess(
                descriptor=descriptor,
                placement=placement,
                observation=observation,
                node=here,
            )
            findings.extend(found)
            if not no_record:
                _store.record_observation(
                    conn,
                    CredentialObservation(
                        origin_node=here,
                        cred_key=placement.cred_key,
                        node=here,
                        present=observation.present,
                        holds_refresh_material=observation.holds_refresh_material,
                        file_mode=observation.file_mode,
                        artifact_expires_at=observation.artifact_expires_at,
                        verdict=found[0].verdict,
                        detail=found[0].summary,
                    ),
                )
        if not no_record:
            conn.commit()
    finally:
        conn.close()

    if not placements:
        raise click.ClickException(
            f"no credential is DECLARED for node {here!r}. That is not a "
            f"clean bill of health — it means this host's credential needs "
            f"have never been recorded, so nothing can tell you what is "
            f"missing. Declare them with `sac creds declare`."
        )

    _emit(findings, as_json=as_json, node=here)
    if worst_severity(findings) == SEVERITY_FAULT:
        raise SystemExit(1)


def _as_descriptor(row):
    if row is None:
        return None
    return CredentialDescriptor(
        origin_node=row["origin_node"],
        cred_key=row["cred_key"],
        kind=row["kind"],
        account=row["account"],
        tier=row["tier"],
        primary_node=row["primary_node"],
        generation=row["generation"],
        expires_at=row["expires_at"],
        refresh_command=row["refresh_command"],
        obtain_command=row["obtain_command"],
    )


def _emit(findings, *, as_json: bool, node: str) -> None:
    if as_json:
        click.echo(
            json.dumps(
                {
                    "node": node,
                    "severity": worst_severity(findings),
                    "findings": [f.__dict__ for f in findings],
                },
                indent=2,
                default=str,
            )
        )
        return
    click.echo(f"credential state on {node}:")
    for finding in findings:
        click.echo(
            f"  [{_MARK.get(finding.severity, finding.severity)}] "
            f"{finding.cred_key}: {finding.verdict}"
        )
        click.echo(f"        {finding.summary}")
        if finding.remedy:
            click.echo(f"        -> {finding.remedy}")


@creds.command("check")
@click.option("--dsn", default=None, help="State DSN override.")
def check(dsn: str | None) -> None:
    """Fleet invariants: CR-001 (one primary per credential) + divergence."""
    conn = _open(dsn)
    try:
        violations = _store.cr001_violations(conn)
        divergent = _store.divergent_declarations(conn)
        findings = []
        for descriptor in _store.descriptors(conn):
            findings.extend(
                check_single_refresher(
                    holders=_store.refresh_holders(
                        conn, cred_key=descriptor["cred_key"]
                    ),
                    cred_key=descriptor["cred_key"],
                    declared_primary=descriptor["primary_node"],
                )
            )
    finally:
        conn.close()

    for row in violations:
        click.echo(
            f"  [FAULT] {row['cred_key']}: {row['primary_count']} nodes are "
            f"declared PRIMARY ({row['nodes']}). CR-001 allows exactly one; "
            f"more than one is mutual invalidation."
        )
    for row in divergent:
        click.echo(
            f"  [WARN ] {row['cred_key']}: declared by {row['origins']}. "
            f"Reported for adjudication, never auto-merged."
        )
    for finding in findings:
        click.echo(f"  [FAULT] {finding.cred_key}: {finding.summary}")
        click.echo(f"        -> {finding.remedy}")

    if not violations and not divergent and not findings:
        click.echo("CR-001 holds: exactly one declared primary per credential.")
        return
    raise SystemExit(1)


@creds.command("declare")
@click.argument("cred_key")
@click.option("--account", required=True, help="Which account this authenticates as.")
@click.option("--locator", required=True, help="file:<abs path> or env:<VARNAME>.")
@click.option("--primary", "primary_node", default=None, help="The primary node.")
@click.option("--node", default=None, help="Node this placement is for.")
@click.option(
    "--tier",
    type=click.Choice([TIER_PRIMARY_SECRET, TIER_DISTRIBUTABLE]),
    default=TIER_PRIMARY_SECRET,
)
@click.option("--kind", default="oauth_session")
@click.option("--obtain-command", default=None, help="How a node gets it if missing.")
@click.option("--refresh-command", default=None, help="How the primary renews it.")
@click.option("--dsn", default=None, help="State DSN override.")
def declare(
    cred_key: str,
    account: str,
    locator: str,
    primary_node: str | None,
    node: str | None,
    tier: str,
    kind: str,
    obtain_command: str | None,
    refresh_command: str | None,
    dsn: str | None,
) -> None:
    """Record a credential so it stops being a fact about a filesystem."""
    here = _node(node)
    primary = primary_node or here
    conn = _open(dsn)
    try:
        _store.record_descriptor(
            conn,
            CredentialDescriptor(
                origin_node=here,
                cred_key=cred_key,
                kind=kind,
                account=account,
                tier=tier,
                primary_node=primary,
                obtain_command=obtain_command,
                refresh_command=refresh_command,
            ),
        )
        _store.record_placement(
            conn,
            CredentialPlacement(
                origin_node=here,
                cred_key=cred_key,
                node=here,
                role=ROLE_PRIMARY if primary == here else ROLE_REPLICA,
                locator=locator,
            ),
        )
        conn.commit()
    finally:
        conn.close()
    click.echo(f"declared {cred_key} on {here} (primary: {primary})")
