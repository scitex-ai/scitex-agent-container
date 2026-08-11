"""TARGET_STANDBY: carry the spec, seed the session marker, start WITHOUT the lease.

Three separate acts, verified separately, because they fail for different
reasons and a combined "it started" would hide which one did not happen. The
transcript is already on the target and content-verified by the time this runs;
what this phase adds is the boot that reads it.

WHERE THE SPEC GOES IS THE ssh USER'S ``$HOME``, AND WHERE THE TRANSCRIPT WENT
IS NOT. Those two homes are different — the transcript follows the CONTAINER's
home, which the spec decides and :mod:`_relocate_transcript_home` derives —
and conflating them is exactly the 2026-08-09 measurement that produced an agent
whose config was carried and whose conversation was left behind. Here the ssh
home is right for the opposite reason: ``~/.scitex/agent-container/agents`` is
sac's own host-side tree, which the target's own sac resolves the same way.

THE MARKER IS SEEDED ON A FIRST BOOT ONLY. A target that already holds a session
id has booted and diverged, and writing over it would discard whatever it did —
so a marker that DISAGREES with the carried session is a refusal naming the file,
never an overwrite (see :func:`._session_carry.plan_session_carry`, which states
the same rule as a decision). A marker that already EQUALS it is a re-run, and
re-runs are free.

THE STANDBY DOES NOT CLAIM THE LEASE, and that is what makes this phase
reversible. Until :mod:`_relocate_effects_handover` runs, the source is still the
owner of record even though its process is stopped; abandoning here costs a
started process on the target and nothing durable. The one thing that WOULD
break that is two live instances, so this phase measures the source as well as
the target before it reports success — finding a restarted source after the
lease moved would be finding it out too late to refuse.
"""

from __future__ import annotations

from ._relocate_execute import StepResult
from ._relocate_liveness import observe_running
from ._relocate_move_aside import move_aside_destination
from ._relocate_target_ssh import (
    SID_ABSENT,
    list_tree,
    read_session_marker,
    start_standby,
    target_home,
    write_session_marker,
)
from ._relocate_transport import CREDENTIAL_BASENAMES
from ._relocate_transport_ssh import copy_transcripts, ensure_dir, move_dir_aside

__all__ = ["StandbyEffects"]


class StandbyEffects:
    """Mixin: the TARGET_STANDBY phase. Expects ``RelocateAdapters``' attributes."""

    def _home(self) -> str | None:
        """The target's ssh ``$HOME``, measured once and remembered."""
        if not self.target_ssh_home:
            self.target_ssh_home = target_home(self.target, exec_fn=self.exec_fn) or ""
        return self.target_ssh_home or None

    def start_target_standby(self) -> StepResult:
        """Carry the spec, seed the session marker, start WITHOUT the lease."""
        if not self.session_uuid:
            return StepResult(
                ok=None,
                attempted=False,
                detail=(
                    "the carried session id is not known, so no marker can be written "
                    "and the target would boot with no memory of the conversation that "
                    "moved it"
                ),
                hint=(
                    "re-run the transport: the session id is the name of the single "
                    "transcript it carried, and a standby started without it is the "
                    "2026-08-07 failure"
                ),
            )
        home = self._home()
        if home is None:
            return StepResult(
                ok=None,
                detail=(
                    f"{self.to_host} did not answer with its $HOME, so sac's own paths "
                    "there are unknown"
                ),
                hint="check ssh to the target, then re-run; nothing was started",
            )

        refusal = self._carry_spec(home) or self._seed_marker(home)
        if refusal is not None:
            return refusal
        return self._boot_standby()

    def _carry_spec(self, home: str) -> StepResult | None:
        """Copy the spec DIRECTORY onto the target. ``None`` when it arrived.

        THE UNIT IS THE DIRECTORY, NOT ``spec.yaml``, and that was measured
        rather than reasoned. The first canary carried the single file, and sac's
        own spec-drift guard on the target refused the boot: the carried copy was
        missing ``to_home/``, which the runtime materialises into the container's
        home. Carrying one file would have produced an agent whose declaration
        arrived and whose to_home layer did not — the same shape as a carried
        config with the conversation left behind, one level up.

        The SAME tar-over-ssh pipeline the transcript uses, and a completeness
        check to match: every file under the directory is listed with its size on
        BOTH hosts and the two trees are compared, because a per-file check of
        the names you thought to name says nothing about the one you forgot.
        """
        if not self.spec_path:
            return StepResult(
                ok=None,
                attempted=False,
                detail="the source-side spec file was not supplied to the adapters",
                hint=(
                    "pass spec_path into adapters_for; the target cannot start an agent "
                    "it has no spec for"
                ),
            )
        spec_dir, _, name = str(self.spec_path).rpartition("/")
        agents_dir, _, entry = spec_dir.rpartition("/")
        if name != "spec.yaml":
            return StepResult(
                ok=False,
                detail=(
                    f"the source spec is named {name!r}; sac discovers agents as "
                    "<agents>/<name>/spec.yaml and would not find it under another name"
                ),
                hint="rename the source spec to spec.yaml, or carry it by hand, then re-run",
            )
        if entry != self.agent:
            return StepResult(
                ok=False,
                detail=(
                    f"the source spec lives in a directory named {entry!r} but the agent "
                    f"is {self.agent!r}; carrying the directory would define a different "
                    "agent on the target"
                ),
                hint=(
                    "rename the spec directory to match the agent, or carry it by hand "
                    "under the right name, then re-run"
                ),
            )
        target_parent = f"{home}/.scitex/agent-container/agents"
        target_dir = f"{target_parent}/{entry}"

        source_tree = list_tree(self.source, spec_dir, exec_fn=self.exec_fn)
        if not source_tree:
            return StepResult(
                ok=None if source_tree is None else False,
                detail=(
                    f"the source spec directory {spec_dir} "
                    + (
                        "could not be listed"
                        if source_tree is None
                        else "holds no files"
                    )
                ),
                hint="check the path exists and is readable on the source, then re-run",
            )
        secrets_found = [
            path
            for path, _ in source_tree
            if path.rsplit("/", 1)[-1] in CREDENTIAL_BASENAMES
        ]
        if secrets_found:
            # The credential refusal, repeated at the one place a DIRECTORY copy
            # could smuggle one past the per-file allowlist. `build_copy_argv`
            # checks the names it is handed, and here it is handed a directory.
            return StepResult(
                ok=False,
                detail=(
                    f"the spec directory holds {secrets_found!r}: a credential is never "
                    "carried — the target re-issues its own"
                ),
                hint=(
                    "move the credential out of the spec directory. Carrying one would "
                    "leave two hosts holding one identity's secret, and the source's copy "
                    "outliving the source"
                ),
            )
        aside = move_aside_destination(target_dir, self.stamp)
        moved = move_dir_aside(self.target, target_dir, aside, exec_fn=self.exec_fn)
        if moved is not True:
            return StepResult(
                ok=None if moved is None else False,
                detail=(
                    f"{self.to_host} already holds {target_dir} and it could not be "
                    f"moved aside to {aside}"
                ),
                hint=(
                    "move it aside by hand and re-run. Nothing is overwritten and nothing "
                    "is deleted — what is there may be a spec somebody edited on that host"
                ),
            )
        self.log.append(f"standby: moved {self.to_host}:{target_dir} aside to {aside}")
        if ensure_dir(self.target, target_parent, exec_fn=self.exec_fn) is not True:
            return StepResult(
                ok=None,
                detail=(
                    f"the agents directory {target_parent} on {self.to_host} could not "
                    "be created"
                ),
                hint=(
                    "check write permission under the target's "
                    "~/.scitex/agent-container, then re-run"
                ),
            )
        # The DIRECTORY is the single name handed to tar, so tar recurses and
        # nothing is re-expanded by a shell on either side — the same property
        # the transcript allowlist relies on, applied to one entry.
        run = copy_transcripts(
            source=self.source,
            source_dir=agents_dir,
            target=self.target,
            target_dir=target_parent,
            files=(entry,),
            exec_fn=self.exec_fn,
            peers=self.peers,
        )
        self.log.append(
            f"standby: spec copy pipeline exit {run.exit_code} — EVIDENCE ONLY; "
            "arrival is decided by listing and counting on the target"
        )
        target_tree = list_tree(self.target, target_dir, exec_fn=self.exec_fn)
        if target_tree is None:
            return StepResult(
                ok=None,
                detail=f"the carried spec directory on {self.to_host} could not be listed",
                hint="list it on the target; an unverified spec is not one to boot from",
            )
        if target_tree != source_tree:
            missing = sorted(set(dict(source_tree)) - set(dict(target_tree)))
            differing = sorted(
                p
                for p, size in source_tree
                if p in dict(target_tree) and dict(target_tree)[p] != size
            )
            return StepResult(
                ok=False,
                detail=(
                    f"the spec directory on {self.to_host} does not match the source: "
                    f"{len(missing)} file(s) absent {missing[:5]}, "
                    f"{len(differing)} of different size {differing[:5]}"
                ),
                hint=(
                    "re-run the copy. A spec that arrived incompletely is the shape sac's "
                    "own drift guard refuses to boot from, and rightly — to_home/ is "
                    "materialised into the container's home and its absence is silent"
                ),
            )
        total = sum(size or 0 for _, size in target_tree)
        self.log.append(
            f"standby: {self.to_host} holds {target_dir} — {len(target_tree)} file(s), "
            f"{total} bytes, tree identical to {self.from_host}:{spec_dir}"
        )
        self.target_spec_path = f"{target_dir}/{name}"
        return None

    def _seed_marker(self, home: str) -> StepResult | None:
        """Write the session marker on a FIRST boot only. ``None`` when settled."""
        state_dir = f"{home}/.scitex/agent-container/runtime/{self.agent}"
        self.target_state_dir = state_dir
        existing = read_session_marker(self.target, state_dir, exec_fn=self.exec_fn)
        if existing is None:
            return StepResult(
                ok=None,
                detail=(
                    f"whether {self.to_host} already holds a session marker was not measured"
                ),
                hint=(
                    "measure it before seeding: writing over a marker the target owns "
                    "would discard whatever it did with that session"
                ),
            )
        if existing == self.session_uuid:
            self.log.append(
                f"standby: {self.to_host} already holds session marker "
                f"{self.session_uuid} — re-run, nothing written"
            )
            return None
        if existing != SID_ABSENT:
            return StepResult(
                ok=False,
                detail=(
                    f"{self.to_host} already holds session marker {existing!r}, which is "
                    f"not the carried {self.session_uuid!r} — it has booted and diverged"
                ),
                hint=(
                    f"move {state_dir}/session_id aside by hand if that history is "
                    "finished with, then re-run. Overwriting it here would discard the "
                    "conversation the target had after it last started"
                ),
            )
        wrote = write_session_marker(
            self.target, state_dir, self.session_uuid, exec_fn=self.exec_fn
        )
        if wrote is None:
            return StepResult(
                ok=None,
                detail=(
                    f"the session marker was written to {state_dir} and could not be read back"
                ),
                hint=(
                    "read it on the target; a marker that cannot be confirmed is not one "
                    "the boot will resume"
                ),
            )
        if wrote != self.session_uuid:
            return StepResult(
                ok=False,
                detail=(
                    f"the marker at {state_dir}/session_id reads {wrote!r} after writing "
                    f"{self.session_uuid!r}"
                ),
                hint="something else is writing that file; settle it before starting the target",
            )
        self.log.append(
            f"standby: seeded {self.to_host}:{state_dir}/session_id = "
            f"{self.session_uuid} (confirmed by read-back)"
        )
        return None

    def _boot_standby(self) -> StepResult:
        """Start the agent on the target and VERIFY it, without touching the lease."""
        run = start_standby(self.target, self.agent, exec_fn=self.exec_fn)
        self.log.append(
            f"standby: start exit {run.exit_code} — {run.stdout.strip()[:400]}"
        )
        if run.stderr.strip():
            self.log.append(f"standby: start stderr {run.stderr.strip()[:300]}")

        # BOTH hosts are observed before either is judged, and the order is
        # deliberate. A start that got re-dispatched back to the source leaves
        # the target empty AND the source running; judging the target first and
        # returning would report only half of that, and the half it omits is a
        # live instance on the machine we are trying to leave.
        running, why = observe_running(self.target, self.agent, exec_fn=self.exec_fn)
        source_running, source_why = observe_running(
            self.source, self.agent, exec_fn=self.exec_fn
        )
        if source_running is True:
            where = (
                f"BOTH {self.from_host} and {self.to_host}"
                if running is True
                else f"{self.from_host}, and NOT on {self.to_host}"
            )
            return StepResult(
                ok=False,
                detail=(
                    f"the start was issued on {self.to_host} and {self.agent} is running "
                    f"on {where} — {source_why}"
                ),
                hint=(
                    f"stop it on {self.from_host} and re-run. A start that lands back on "
                    "the source means the spec's host: pin was re-consulted; the standby "
                    "start passes --no-redispatch precisely to prevent that, so check the "
                    "start output above. The lease has NOT moved — the source is still "
                    "the owner of record"
                ),
            )
        if running is None:
            return StepResult(
                ok=None,
                detail=(
                    f"the start was issued on {self.to_host} and the result was not "
                    f"observable: {why}"
                ),
                hint="observe liveness on the target before handing anything over",
            )
        if running is False:
            return StepResult(
                ok=False,
                detail=(
                    f"the start was issued and {self.agent} is NOT running on "
                    f"{self.to_host} — {why}"
                ),
                hint=(
                    f"read the start output above and `sac agents list {self.agent}` on "
                    f"{self.to_host}. Nothing was handed over; the lease has not moved"
                ),
            )
        self.standby_started = True
        self.log.append(f"standby: {self.from_host} is not running it — {source_why}")
        return StepResult(
            ok=True,
            detail=(
                f"{self.agent} is running on {self.to_host} as a STANDBY (no lease) — "
                f"{why}; seeded to resume session {self.session_uuid}"
            ),
        )
