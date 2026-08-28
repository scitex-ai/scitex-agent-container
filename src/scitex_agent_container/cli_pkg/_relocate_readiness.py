"""Which phases of a relocation can run, and what each one is missing.

Extracted from :mod:`_relocate_cmd` so the command file stays about the command.
The list below is DATA and the notice is GENERATED FROM IT, so the two cannot
drift apart the way the original hard-coded sentence did — it named the
transcript transport as the one missing piece and went stale the moment that
phase was built.
"""

from __future__ import annotations

__all__ = ["PHASE_READINESS", "readiness_notice"]


#: What `--no-dry-run` can and cannot do, phase by phase. THE NOTICE IS
#: GENERATED FROM THIS LIST, so the two cannot drift apart the way the original
#: hard-coded sentence did — it named the transcript transport as the one missing
#: piece and went stale the moment that phase was built.
#:
#: Each entry is (phase, what is BUILT, what is MISSING). A phase with nothing
#: missing carries ``"—"`` there and RUNS. The split is the honest unit of
#: progress: a decision layer being unit-tested says nothing about whether bytes
#: move, and an adapter is not claimed to work until it has been exercised
#: against two real hosts.
PHASE_READINESS: tuple[tuple[str, str, str], ...] = (
    (
        "source_drain",
        "_relocate_liveness (tmux is the same fact the runtime checks) — a STOPPED "
        "source drains vacuously and that is measured, not assumed",
        "no adapter for a RUNNING source: telling an agent to finish its in-flight "
        "work and take no new work, and confirming it did",
    ),
    (
        "source_stop",
        "_relocate_effects.stop_source: `sac agents stop` on the source, then a "
        "SECOND independent liveness observation. Idempotent",
        "—",
    ),
    (
        "transport",
        "_relocate_transport (selection, move-aside, byte+line verification), "
        "_relocate_transport_paths (target-side project dir), "
        "_relocate_transcript_home (the HOST path backing the container's $HOME) "
        "and _relocate_transport_ssh (tar-over-ssh; counts taken ON the target)",
        "—",
    ),
    (
        "target_standby",
        "_relocate_effects_standby: the spec is carried and byte+line verified on the "
        "target, the session_id marker is seeded from the CARRIED transcript "
        "(first boot only — an existing marker refuses rather than being overwritten) "
        "and confirmed by read-back, then `sac agents start --resume <carried uuid>` "
        "WITHOUT the lease and a SECOND independent liveness observation on BOTH hosts",
        "—",
    ),
    (
        "handshake",
        "_relocate_effects_handshake: the brief is delivered to the agent's sidecar on "
        "its own host and the answer is read back out of its transcript by the "
        "coordinator ON THE SOURCE, then put through _relocate_handshake's gate "
        "(nonce + a proof-of-work answer measured independently on the target)",
        "—",
    ),
    (
        "handover",
        "_relocate_effects_handover: the source's lease is bootstrapped when the store "
        "holds none (sac still does not claim one at agent start-up — the bootstrap is "
        "recorded as such), handed to the target, and the row is RE-READ to confirm "
        "the holder and the fence. A row naming a THIRD host is settled by OBSERVING "
        "that host, through the same predicate the lease_holdable check already ran "
        "before anything was stopped",
        "—",
    ),
    (
        "done",
        "_relocate_effects.finish: both hosts are observed for exactly ONE live "
        "instance, then residency is written to the agent_residency store in "
        "PostgreSQL (_state.relocation_pg, NOT state.db) and the "
        "source's transcript MOVED ASIDE — all gated on the two confirmations being "
        "recorded True",
        "—",
    ),
)


def readiness_notice() -> list[str]:
    """Say exactly which phases can and cannot run, before running any of them.

    Printed as a PREAMBLE now rather than as a refusal: the executing path
    exists, so the operator's question has changed from "why won't it run" to
    "how far will it get". Answering that up front is the difference between a
    surprising stop and an expected one.
    """
    lines = ["", "[bold]PHASE READINESS[/bold]"]
    for phase, built, missing in PHASE_READINESS:
        state = "[green]runs[/green]" if missing == "—" else "[yellow]refuses[/yellow]"
        lines.append(f"  [bold]{phase}[/bold]  {state}")
        if built != "—":
            lines.append(f"    built:   {built}")
        if missing != "—":
            lines.append(f"    missing: {missing}")
    lines += [
        "",
        "A phase with no adapter returns UNKNOWN and the relocation STOPS there, in "
        "a state the journal records; it does not journal its way to DONE having "
        "moved nothing. Nothing is deleted at any point — anything displaced goes "
        "to .old/<stamp>/ on the host it was displaced on, so a rollback is you "
        "moving it back, deliberately.",
    ]
    return lines
