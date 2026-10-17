"""``sac accounts probe-api-keys`` — the PRODUCER of the key-liveness verdicts.

The boot picker and the listen key-health endpoint only ever READ the
cached verdicts (see ``.._account.api_key_liveness`` for why live probing
must never happen at start-up or in the control plane). Something has to
write those verdicts, and this is it.

Without a periodic run of this command the cache file never exists, every
slot reads UNKNOWN, and the key-health gate is inert — exactly the failure
mode ``refresh-quota-cache`` documents for the quota cache. It is meant to
be attached to the supervisor timer that already walks every account.

WHAT IT MEASURES. One cheap read-only request per declared API-key slot
across every agent spec on this host (the key -> agent map comes from the
specs' own Hermes failover pools, so the map can never drift from what
the agents actually consume). Four verdicts: alive / exhausted (valid key,
empty allowance — the 2026-10-15 infra-lead stop) / dead (401/403) /
unknown (no answer), plus unresolved for slots with no value on this host.

Read-only: it rotates nothing, spends no conversation turns, and costs
one billing-endpoint GET per slot. Exit 0 even with dead/exhausted slots:
recording a dead key is this command SUCCEEDING — a non-zero exit would
mark the timer unit failed on every pass for as long as a key stays dead.
"""

from __future__ import annotations

import click


def register_probe_api_keys_command(group) -> None:
    """Attach ``probe-api-keys`` onto the ``accounts`` group."""

    @group.command("probe-api-keys")
    @click.option(
        "--all",
        "all_slots",
        is_flag=True,
        default=False,
        help="Probe every declared key slot (what the supervisor runs).",
    )
    @click.option(
        "--record/--no-record",
        default=True,
        help="Write the verdict cache file (default: record).",
    )
    @click.option(
        "--json",
        "as_json",
        is_flag=True,
        default=False,
        help="Emit the sweep document as JSON.",
    )
    def probe_api_keys_cmd(
        all_slots: bool, record: bool, as_json: bool
    ) -> None:
        """Probe every declared API key once and cache the verdicts.

        \b
        Examples:
          $ sac accounts probe-api-keys --all
          $ sac accounts probe-api-keys --all --json
        """
        import json as _json
        import os
        from pathlib import Path

        from .._account.api_key_liveness import (
            CACHE_FILENAME,
            collect_slots,
            sweep,
            write_cache,
        )

        if not all_slots:
            raise click.UsageError("pass --all (single-slot probing is not a thing)")
        from ..config._resolve import enumerate_agent_names, resolve_config

        import yaml

        specs = []
        for agent_name in enumerate_agent_names():
            try:
                path = resolve_config(agent_name)
            except Exception:
                continue
            try:
                with open(path, encoding="utf-8") as handle:
                    spec = yaml.safe_load(handle)
            except (OSError, ValueError):
                continue
            if isinstance(spec, dict):
                spec["agent_name"] = agent_name
                specs.append(spec)
        slots = collect_slots(specs)
        secrets = {
            alias: os.environ.get(alias, "") for alias in slots
        }
        doc = sweep(slots, secrets)
        recorded = None
        if record:
            cache_path = (
                Path.home() / ".scitex" / "agent-container" / "runtime" / CACHE_FILENAME
            )
            recorded = str(write_cache(cache_path, doc))
        if as_json:
            click.echo(_json.dumps(doc, indent=1, sort_keys=True))
        else:
            for alias in sorted(doc["slots"]):
                row = doc["slots"][alias]
                agents = ",".join(row.get("agents", [])) or "-"
                click.echo(
                    f"  {alias:<32} {row['verdict']:<10} "
                    f"{row.get('reason', ''):<28} {agents}"
                )
            counts: dict[str, int] = {}
            for row in doc["slots"].values():
                counts[row["verdict"]] = counts.get(row["verdict"], 0) + 1
            click.echo(
                f"  slots={len(doc['slots'])} "
                + " ".join(f"{k}={v}" for k, v in sorted(counts.items()))
                + (f" recorded={recorded}" if recorded else " (not recorded)")
            )
