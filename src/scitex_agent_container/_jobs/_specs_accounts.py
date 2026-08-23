"""The OAuth-credential beat: three jobs that keep sac able to log in.

Split out of :mod:`._jobs_plugin` (at the per-file cap), following the
convention :mod:`._specs_liveness` established for the same reason.

These three belong together because they share ONE failure mode — an
expired token — and their cadences are chosen against each other rather
than in isolation: ``accounts-refresh`` renews on the beat the token
lifetime dictates, ``accounts-keepalive`` covers the hosts that refresh
cannot reach, and ``accounts-quota-cache`` publishes what both produce.
A reader asking "why is the fleet logged out?" must see all three at
once; asking it of any one of them gives an answer that is true and
useless.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover - typing only
    from scitex_dev.jobs import JobSpec

__all__ = ["accounts_jobs"]


def accounts_jobs(*, executable: str | None = None) -> "list[JobSpec]":
    """sac's OAuth-credential JobSpecs, in their historical order.

    ``executable`` is the same test seam :func:`._sac_bin.sac_bin` exposes,
    threaded through so a test can resolve the payload against a venv-shaped
    tree it built on disk. Without it the rendered command depends on whether
    the RUNNING environment happens to have a ``sac`` console script beside
    its interpreter — true in production, false under a PYTHONPATH-only CI
    run — and a population guard over these specs would then assert an
    environmental fact rather than a property of the specs.
    """
    from scitex_dev.jobs import JobSpec

    from ._sac_bin import sac_bin

    # ABSOLUTE, resolved per host -- see :mod:`._sac_bin` for the measurement.
    sac = sac_bin(executable=executable)

    return [
        JobSpec(
            # THE LAST NAME OFF THE LEGACY PREFIX. It was held back for
            # months on the rule "never rename a spec ahead of its unit", and
            # that rule was right about the hazard and wrong about the order.
            #
            # WHAT THE HOLD ASSUMED: that the supervised cutover renames spec
            # and unit TOGETHER. MEASURED 2026-08-19, that is unreachable —
            # the migration's install step delegates to scitex-dev BY THE NEW
            # CANONICAL NAME, and scitex-dev resolves a name only if a JobSpec
            # DECLARES it:
            #
            #   $ sac dev timer install scitex-agent-container-accounts-refresh
            #   no job named 'scitex-agent-container-accounts-refresh' here
            #
            # So install-new cannot run until this line moves. The spec moves
            # FIRST or the cutover never happens at all; that is not a rule
            # violation, it is the only order the tooling admits. (Attempted
            # in the other order 2026-08-18: stop/remove succeeded, install
            # failed on exactly that lookup, and the fleet's sole OAuth
            # refresher was down ~2 minutes until the old units were restored.)
            #
            # WHAT THE WINDOW ACTUALLY COSTS, stated plainly rather than
            # assumed: between this rename and the host cutover,
            # `sac dev timer status accounts-refresh` resolves to a unit name
            # the host does not carry and reports the refresher ABSENT while
            # `sac.accounts-refresh.timer` keeps firing every 2h. That is a
            # MISREPORT ON ONE MANUAL VERB, not an outage — and it cannot
            # escalate on its own: nothing in this repo installs or enables a
            # timer automatically (`_dev_jobs_backend` declines to run
            # systemctl; `_dev_jobs_apply` only PRINTS the enable line), so
            # the two-racing-refreshers catastrophe still requires a human to
            # type the install command against a host that already has the
            # old unit.
            #
            # CLOSING THE WINDOW is `sac dev migrate-job-names --only
            # accounts-refresh --include-held --yes`, run supervised, then
            # `sac accounts status` BEFORE walking away. Ordered
            # stop-old -> remove-old -> install-new -> verify-exactly-one, and
            # "exactly one" means one unit FOR THIS JOB:
            # `scitex-agent-container-accounts-keepalive` is a DIFFERENT job
            # that must keep existing.
            name="scitex-agent-container-accounts-refresh",
            schedule="0 */2 * * *",  # every 2h
            # SELF-BOUNDING (120s) — see the convention note above. The
            # bound has to live in the command because this job lands on
            # CRON, where `timeout_sec` cannot follow it.
            command=(
                "/usr/bin/timeout 120 "
                f"{sac} accounts refresh --all --include-active --sync-active-login"
            ),
            description=(
                "Headless OAuth access-token refresh for all stored Claude "
                "accounts including the active one (sole-refresher model), "
                "mirroring the rotation into the live ~/.claude login."
            ),
            # 2026-06-11 (lead msg c5212862): scitex_dev.jobs.JobSpec kind
            # taxonomy is {"service","timer","cron"} since scitex-dev #153.
            # ``sac.accounts-refresh`` is a periodic systemd --user timer
            # (token TTL ~7h, refresh every 2h) → ``kind="timer"`` with the
            # cadence carried by ``on_unit_active_sec`` below. The legacy
            # ``kind="systemd"`` is no longer accepted; it raises
            # ``ValueError`` at construction time and ``scitex-dev
            # ecosystem up`` silently drops sac's whole provider
            # (provider-isolated, WARN-only), leaving the OAuth refresh
            # unmanaged.
            kind="timer",
            on_boot_sec="15min",
            on_unit_active_sec="2h",
        ),
        JobSpec(
            name="scitex-agent-container-accounts-keepalive",
            schedule="*/15 * * * *",  # every 15min (cron form; timer below)
            # SELF-BOUNDING (300s). Per peer: a handful of coreutils ssh ops
            # plus ONE outbound HTTPS verification from the peer (15s cap
            # inside the probe). 300s covers three peers including a slow one
            # without ever hanging forever. A pass killed here leaves the
            # peer's previous credential intact — nothing is published
            # unverified.
            # ywata-note-win IS DECLARED OPTIONAL, and that declaration is the
            # difference between two rails running the same verb.
            #
            # MEASURED 2026-08-23, both sides, because a divergence claim needs
            # both. The INSTALLED timer unit on compute-04 carries a drop-in
            # (…keepalive.service.d/optional-peer.conf) whose ExecStart ends
            # `--optional-peer ywata-note-win`. This JobSpec did not. Same verb,
            # two policies — and only one of them was in git.
            #
            # The cost, from the supervisor's own execution log
            # (~/.scitex/dev/runtime/periodic-executions.jsonl, surfaced by
            # handyman-06): the supervisor rail failed 96 of 416 runs = 23.1%,
            # still failing the day this was written, while the timer rail with
            # the declaration runs ~2 failures/day over the same window.
            #
            # The laptop is DOCUMENTED intermittently reachable — the 2026-08-16
            # "No route to host" incident is the reason --optional-peer exists at
            # all. A peer known to come and go must be declared, or every one of
            # its absences reds a unit whose real job (keeping the always-on
            # hosts' credentials alive) succeeded.
            #
            # NOT CLAIMED: that this explains all 96. Every failed record carries
            # error=None, so the failing peer is still unidentified; output
            # capture in the execution log is the instrument that would prove it.
            # This change is justified on its own — one verb, one policy — rather
            # than on being the whole cause.
            command=(
                "/usr/bin/timeout 300 "
                f"{sac} accounts keepalive --all "
                "--to ywata-note-win "
                "--to scitex-compute-03 "
                "--to scitex-compute-04 "
                "--optional-peer ywata-note-win"
            ),
            description=(
                "The DISTRIBUTION half of the single-refresher model, and "
                "the only thing keeping the access-only hosts alive. "
                "sac.accounts-refresh rotates the token on the ONE host that "
                "holds refresh material (scitex-nas-03 as of 2026-08-10); "
                "every other host holds an ACCESS-ONLY copy that nothing on "
                "that box can renew, so without this job those hosts simply "
                "expire and 401 within one access-token lifetime. COPIES the "
                "current token (never mints — minting rotates, which revokes "
                "the token running agents hold), refuses a payload carrying "
                "refresh material, refuses under 300s of validity, refuses "
                "to overwrite a valid remote credential with a dead one, "
                "backs up what it replaces, publishes 0600, and PROVES the "
                "far side answers HTTP 200. CONVERGENT: it compares "
                "fingerprints and rewrites a peer only when the master's "
                "token actually changed, so most runs are cheap verified "
                "no-ops. WORST-CASE FOLLOWER OUTAGE THE OPERATOR IS "
                "ACCEPTING AT THIS CADENCE: 15 minutes — the moment the "
                "master refreshes, every follower's copy is revoked, and "
                "they stay dead until the next tick converges them. Exits "
                "non-zero on any peer's failure. NOT armed by this "
                "declaration."
            ),
            kind="timer",
            # HOST PINNING IS NOT EXPRESSIBLE HERE. JobSpec has no host
            # field (name/kind/schedule/command/description/on_boot_sec/
            # on_unit_active_sec/timeout_sec/restart_policy/watchdog_sec/
            # venv), so WHERE this runs is decided by where the operator
            # installs it. It must run ONLY on the refresh holder. sac's
            # own mitigation is inside the verb: `--all` resolves to the
            # accounts THIS host holds refresh material for, and exits
            # non-zero when that set is empty — so a keepalive installed on
            # the wrong host fails loudly instead of pretending to work.
            #
            # 15min is a BOUND, not a guess. Measured 2026-08-10: Claude
            # Code refreshes only when the token is genuinely near expiry,
            # so the master's token changes ONCE in ~7h at an unpredictable
            # moment — and the instant it does, every follower's copy is
            # revoked and its agents 401. The tick therefore does not decide
            # when work happens (the fingerprint comparison does); it decides
            # only how long that revoked window lasts. 15min bounds the
            # follower outage to 15min; hourly would bound it to an hour.
            # The cost of the extra ticks is near zero because a converged
            # peer is verified, not rewritten.
            on_boot_sec="10min",
            on_unit_active_sec="15min",
        ),
        JobSpec(
            name="scitex-agent-container-accounts-quota-cache",
            schedule="*/5 * * * *",  # every 5min (cron form; timer below)
            # SELF-BOUNDING (120s). One usage read per stored account (4
            # today), each a single HTTPS call; 120s covers all of them on a
            # slow network and never hangs. A pass killed here leaves the
            # PREVIOUS cache in place — stale, which is the status quo this
            # job improves on, never wrong.
            command=f"/usr/bin/timeout 120 {sac} accounts refresh-quota-cache",
            description=(
                "Keeps the per-account usage cache FRESH. Nothing else did: "
                "sac.accounts-refresh rotates TOKENS and accounts-keepalive "
                "COPIES tokens, so before this job every quota number the "
                "fleet showed was as old as the last time a human happened to "
                "run the verb by hand. Reads usage only — it neither mints nor "
                "rotates, so it cannot revoke a credential a running agent "
                "holds (the hazard that makes `accounts refresh` dangerous to "
                "schedule aggressively). "
                "MEASURED COST OF NOT HAVING IT, 2026-08-17: `sac accounts "
                "list` rendered every bar with `! snapshot older than the "
                "refresh window` and `(stale 1d)` — it detected its own "
                "staleness and did not fix it. On that day-old snapshot "
                "scitex-01 read 7d=23% and wyusuuke read 7d=100%; the truth "
                "after a manual refresh was scitex-01 94% and wyusuuke 0% — "
                "INVERTED. An agent was repointed at the nearly-exhausted "
                "account on the strength of it, and the operator's own "
                "diagnosis was that an account reaches 94% precisely because "
                "no one is refreshing the numbers that would have shown it "
                "climbing. A cache that reports its own staleness without "
                "repairing it is a record, not a gate."
            ),
            kind="timer",
            # 5min, at the operator's instruction — he proposed 1min and
            # offered 5min "if that is overdoing it". Taking the 5.
            #
            # The cadence is chosen against the 5h window, which is the fast
            # one: a busy account can cross from comfortable to exhausted
            # inside a single hour, so an hourly cadence would still permit a
            # placement decision on numbers that predate the exhaustion. The
            # 7d window moves far too slowly to drive this.
            #
            # WHY NOT 1min. Staleness at 5min is already far inside any
            # decision window — no account crosses a threshold that matters in
            # five minutes, so 1min buys freshness nobody can act on while
            # costing 5x the calls (4 accounts x 1440 = 5760/day against a
            # third-party usage endpoint whose rate limits we do not control
            # and have not measured). If a case ever appears where a 5min-old
            # number caused a wrong decision, that is the evidence to go to
            # 1min — and it would be evidence, not a guess.
            on_boot_sec="2min",
            on_unit_active_sec="5min",
        ),
    ]
