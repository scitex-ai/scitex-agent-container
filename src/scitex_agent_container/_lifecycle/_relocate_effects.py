"""The adapters that turn each decided phase into something that happens on two machines.

:mod:`_relocate_execute` owns the ORDER and refuses to run without an effect for
every phase; :mod:`_relocate_transport`, ``_relocate_transport_paths``,
``_relocate_handshake`` and ``_relocate_lease`` own the DECISIONS and touch
nothing. This module is the join: it measures, it acts, and it hands each
decision module real observations instead of assumptions.

EVERY PHASE THAT IS NOT BUILT REFUSES BY NAME — see
:func:`._relocate_liveness.unimplemented`. The driver requires an effect per
phase precisely so the tempting shortcut (omit one, let it pass as a no-op) is
unavailable; a named UNKNOWN is what goes there instead, and the relocation stops
at that phase in a state the journal records.

THE FOUR CRITERIA, AND WHERE EACH IS ENFORCED RATHER THAN DESCRIBED:

    src -> tgt confirmation is CONTENT-verified arrival. :meth:`transport`
    measures every carried file ON THE TARGET with the target's own shell and
    passes both sides to :func:`verify_arrival`. The copy pipeline's exit code is
    recorded as evidence and is never the answer.

    tgt -> src confirmation is an OBSERVED answer, and UNKNOWN refuses.
    :meth:`handshake` builds the arrival brief (which IS the challenge) and hands
    what was actually seen to :func:`evaluate_handshake`. Nothing here converts
    "I saw no reply" into "there was none", nor an accepted send into a reply.

    RETIREMENT HAPPENS ONLY AFTER BOTH. :meth:`finish` re-checks the two recorded
    confirmations before writing residency or moving anything. The phase order
    already guarantees it — the driver stops on any non-yes — but a guarantee
    living only in another module's control flow is one refactor from untrue, and
    this is the step that moves a human's conversation out from under them.

    FAILURE LANDS IN A NAMED STATE, RETRY IS IDEMPOTENT, ROLLBACK IS DELIBERATE.
    A stop re-stops nothing; a copy re-copies onto a destination moved aside
    again; a residency write to the host already recorded is a no-op. NOTHING IS
    EVER DELETED — the target's prior transcript directory and the source's
    retired one both go to ``.old/<stamp>/``, so undoing a relocation is a human
    moving a directory back, never this code deciding it should.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Callable

from ._relocate_effects_handover import HandoverEffects
from ._relocate_effects_handshake import HandshakeEffects
from ._relocate_effects_source import SourceEffects
from ._relocate_effects_standby import StandbyEffects
from ._relocate_execute import PhaseEffects, StepResult
from ._relocate_liveness import observe_running
from ._relocate_move_aside import move_aside_destination
from ._relocate_shell import Shell, resolved_path, shell_for
from ._relocate_transcript_home import transcript_home_from_spec
from ._relocate_transport import TranscriptFile, plan_transport, verify_arrival
from ._relocate_transport_paths import derive_target_dir
from ._relocate_transport_ssh import (
    copy_transcripts,
    ensure_dir,
    list_transcript_dir,
    measure_transcripts,
    move_dir_aside,
)

__all__ = ["RelocateAdapters", "adapters_for", "build_effects"]


@dataclass
class RelocateAdapters(
    SourceEffects, StandbyEffects, HandshakeEffects, HandoverEffects
):
    """One relocation's worth of hosts, paths and RECORDED CONFIRMATIONS.

    The confirmations are fields rather than local variables because
    :meth:`finish` has to re-read them: "both legs were observed" is a
    precondition of retiring the source, and a precondition that exists only as
    control flow somewhere else is not one this module can check.
    """

    agent: str
    spec: dict
    from_host: str
    to_host: str
    source: Shell
    target: Shell
    stamp: str
    exec_fn: Callable[..., dict] | None = None
    now: Callable[[], float] = time.time
    peers: object = None

    #: The source-side ``spec.yaml`` this agent is defined by. Carried to the
    #: target by TARGET_STANDBY — a host cannot start an agent it has no spec
    #: for, and the parsed dict above is not the file (comments and formatting
    #: are the operator's, and re-serialising would silently rewrite them).
    spec_path: str = ""

    source_dir: str = ""
    target_dir: str = ""
    carried: tuple[str, ...] = ()
    sent: tuple[TranscriptFile, ...] = ()
    landed: tuple[TranscriptFile, ...] = ()
    arrival_confirmed: bool | None = None
    handshake_confirmed: bool | None = None
    session_uuid: str = ""
    #: The TARGET's ssh ``$HOME`` — sac's own tree root there, and deliberately
    #: NOT the transcript home, which follows the container's home instead.
    target_ssh_home: str = ""
    target_spec_path: str = ""
    target_state_dir: str = ""
    standby_started: bool = False
    lease_fence: int | None = None
    log: list[str] = field(default_factory=list)

    # -- transport ---------------------------------------------------------

    def _resolve_dirs(self) -> tuple[str | None, str | None, str]:
        """Source and target transcript directories, or ``(None, None, why)``.

        The target's directory name is RECOMPUTED from the target's own resolved
        workdir rather than copied from the source's — see
        :mod:`_relocate_transport_paths`. A mismatch is normal and is logged, not
        refused: it is the whole reason the name is derived instead of reused.
        """
        home = transcript_home_from_spec(self.spec)
        if home.path is None:
            return None, None, f"{home.reason} — {home.hint}"

        body = (
            self.spec.get("spec")
            if isinstance(self.spec.get("spec"), dict)
            else self.spec
        )
        workdir = str((body or {}).get("workdir") or "").strip()
        if not workdir:
            return (
                None,
                None,
                "the spec declares no workdir, so no project directory can be encoded",
            )

        src_resolved = resolved_path(self.source, workdir, exec_fn=self.exec_fn)
        tgt_resolved = resolved_path(self.target, workdir, exec_fn=self.exec_fn)
        if not src_resolved:
            return (
                None,
                None,
                f"the SOURCE's resolved workdir for {workdir} was not observed",
            )

        src = derive_target_dir(
            target_home=home.path, target_resolved_workdir=src_resolved
        )
        tgt = derive_target_dir(
            target_home=home.path,
            target_resolved_workdir=tgt_resolved,
            source_dir_name=src.encoded,
        )
        if tgt.path is None:
            return None, None, f"{tgt.reason} — {tgt.hint}"
        if tgt.matches_source is False:
            self.log.append(f"transport: {tgt.reason}")
        return src.path, tgt.path, tgt.reason

    def transport(self) -> StepResult:
        """Copy the transcript across and CONFIRM it on the target, per file.

        List the source, let :func:`plan_transport` decide what may travel, move
        aside anything already at the destination, measure the source, copy, then
        measure THE TARGET and compare. The measurement after the copy is the
        only statement this step makes about success.
        """
        source_dir, target_dir, why = self._resolve_dirs()
        if source_dir is None or target_dir is None:
            return StepResult(
                ok=None,
                detail=f"the transcript directories could not be derived: {why}",
                hint=(
                    "measure the target's resolved workdir and check the spec's bind for "
                    "the container home. A transcript under the wrong directory name is "
                    "present, intact and invisible to the runner"
                ),
            )
        self.source_dir, self.target_dir = source_dir, target_dir

        listing = list_transcript_dir(self.source, source_dir, exec_fn=self.exec_fn)
        running, _ = observe_running(self.source, self.agent, exec_fn=self.exec_fn)
        target_listing = list_transcript_dir(
            self.target, target_dir, exec_fn=self.exec_fn
        )

        plan = plan_transport(
            source_running=running,
            source_files=None if listing is None else list(listing),
            target_dir_exists=None if target_listing is None else bool(target_listing),
            target_dir=target_dir,
            stamp=self.stamp,
        )
        for name, reason in plan.refused:
            self.log.append(f"transport: REFUSED {name} — {reason}")
        if plan.proceed is not True:
            return StepResult(
                ok=False if plan.proceed is False else None,
                detail=f"transport refused ({plan.code}): {plan.reason}",
                hint=plan.hint,
            )
        self.carried = plan.files

        if plan.move_aside is not None and plan.move_aside.required:
            moved = move_dir_aside(
                self.target,
                target_dir,
                plan.move_aside.destination or "",
                exec_fn=self.exec_fn,
            )
            if moved is not True:
                return StepResult(
                    ok=None if moved is None else False,
                    detail=(
                        f"{self.to_host} already holds {target_dir} and it could not be "
                        f"moved aside to {plan.move_aside.destination}"
                    ),
                    hint=(
                        "move it aside by hand and re-run. Nothing is overwritten and "
                        "nothing is deleted — what is there may be the only copy of an "
                        "earlier conversation"
                    ),
                )
            self.log.append(
                f"transport: moved {self.to_host}:{target_dir} aside to {plan.move_aside.destination}"
            )

        made = ensure_dir(self.target, target_dir, exec_fn=self.exec_fn)
        if made is not True:
            return StepResult(
                ok=None if made is None else False,
                detail=f"the destination {target_dir} on {self.to_host} could not be created",
                hint="check write permission on the target's transcript root, then re-run",
            )

        self.sent = measure_transcripts(
            self.source, source_dir, plan.files, exec_fn=self.exec_fn
        )
        run = copy_transcripts(
            source=self.source,
            source_dir=source_dir,
            target=self.target,
            target_dir=target_dir,
            files=plan.files,
            exec_fn=self.exec_fn,
            peers=self.peers,
        )
        self.log.append(
            f"transport: copy pipeline exit {run.exit_code} — EVIDENCE ONLY; arrival is "
            "decided by counting on the target"
        )
        if run.stderr.strip():
            self.log.append(f"transport: copy stderr {run.stderr.strip()[:300]}")
        self.landed = measure_transcripts(
            self.target, target_dir, plan.files, exec_fn=self.exec_fn
        )
        verdict = verify_arrival(sent=self.sent, landed=self.landed)
        self.arrival_confirmed = verdict.arrived
        for line in verdict.mismatches:
            self.log.append(f"transport: MISMATCH {line}")
        for f in self.landed:
            self.log.append(
                f"transport: target holds {f.name} {f.byte_count} bytes / {f.line_count} lines"
            )
        if verdict.arrived is not True:
            return StepResult(
                ok=False if verdict.arrived is False else None,
                detail=f"arrival not confirmed ({verdict.code}): {verdict.reason}",
                hint=verdict.hint,
            )
        if len(plan.files) == 1:
            self.session_uuid = plan.files[0].rsplit(".", 1)[0]
        return StepResult(
            ok=True,
            detail=(
                f"{verdict.reason}; {self.from_host}:{source_dir} -> {self.to_host}:{target_dir}"
            ),
        )

    # -- target ------------------------------------------------------------
    #
    # TARGET_STANDBY, HANDSHAKE and HANDOVER live in their own modules
    # (_relocate_effects_standby / _handshake / _handover) and arrive here as
    # mixins, exactly as the source-side pair does. Each is one phase's worth of
    # decisions and each was a named refusal until it was built; keeping them
    # apart means a reader looking for "what does the handover do" finds a file
    # about the handover rather than a method in the middle of the transport.

    # -- done --------------------------------------------------------------

    def finish(self) -> StepResult:
        """Write residency, then retire the source's transcript — in that order.

        BOTH CONFIRMATIONS ARE RE-CHECKED HERE; see the module docstring.

        Residency is written BEFORE the retirement because the two failures are
        not symmetric: a residency row with the source's transcript still in
        place is a completed relocation that left a spare copy, while a retired
        source with no residency row is an agent that lives nowhere.
        """
        if self.arrival_confirmed is not True:
            return StepResult(
                ok=False,
                detail=(
                    "refusing to retire the source: arrival on the target was not "
                    f"confirmed (arrival_confirmed={self.arrival_confirmed!r})"
                ),
                hint="re-run the transport and confirm by byte and line count first",
            )
        if self.handshake_confirmed is not True:
            return StepResult(
                ok=False,
                detail=(
                    "refusing to retire the source: the target never proved, to the "
                    f"source, that it can do agent work (handshake_confirmed="
                    f"{self.handshake_confirmed!r})"
                ),
                hint=(
                    "complete the handshake first. Retiring on an unproven target is the "
                    "2026-08-07 failure with the source's memory moved out of reach"
                ),
            )

        owners = self._one_owner_check()
        if owners is not None:
            return owners

        from .._state.state_db_relocation import record_residency

        opened = record_residency(
            agent=self.agent,
            host=self.to_host,
            now=self.now(),
            note=f"relocated from {self.from_host}",
        )
        self.log.append(
            f"done: residency {'opened' if opened else 'already recorded'} on {self.to_host}"
        )

        retired = move_aside_destination(self.source_dir, self.stamp)
        moved = move_dir_aside(
            self.source, self.source_dir, retired, exec_fn=self.exec_fn
        )
        if moved is not True:
            return StepResult(
                ok=None if moved is None else False,
                detail=(
                    f"residency now reads {self.to_host}, and the source's transcript at "
                    f"{self.source_dir} could not be moved aside"
                ),
                hint=(
                    "move it aside by hand. The relocation itself is complete — the source "
                    "merely still holds a copy, which is safe and is never deleted"
                ),
            )
        return StepResult(
            ok=True,
            detail=(
                f"residency reads {self.to_host}; the source's transcript was moved aside "
                f"to {retired} (moved, never deleted)"
            ),
        )

    def _one_owner_check(self) -> StepResult | None:
        """Observe BOTH hosts. ``None`` when exactly one of them is running it.

        The residency row about to be written says where the agent lives, and a
        record that disagrees with the machines is worse than no record: it is a
        confident wrong answer that everything downstream will believe. So the
        two hosts are asked, at the last moment before the write, with the same
        instrument the runtime itself uses.

        Both directions are refusals. The SOURCE running means something
        restarted it and there are two live instances under one identity — the
        state the whole sequence exists to make impossible. The TARGET not
        running means the agent that was just handed the lease is not there, so
        recording it as resident would name a host where nothing is listening.
        """
        target_up, target_why = observe_running(
            self.target, self.agent, exec_fn=self.exec_fn
        )
        source_up, source_why = observe_running(
            self.source, self.agent, exec_fn=self.exec_fn
        )
        self.log.append(
            f"done: liveness {self.to_host}={target_up!r} ({target_why}); "
            f"{self.from_host}={source_up!r} ({source_why})"
        )
        if source_up is True:
            return StepResult(
                ok=False,
                detail=(
                    f"refusing to record residency: {self.agent} is running on BOTH "
                    f"{self.from_host} and {self.to_host} — {source_why}"
                ),
                hint=(
                    f"stop it on {self.from_host}. The lease already moved, so "
                    f"{self.from_host} is fenced out of the shared store, but two live "
                    "loops under one identity is exactly what must not persist"
                ),
            )
        if target_up is not True:
            return StepResult(
                ok=None if target_up is None else False,
                detail=(
                    f"refusing to record residency: {self.agent} is not confirmed running "
                    f"on {self.to_host} — {target_why}"
                ),
                hint=(
                    f"start it on {self.to_host} and confirm. The lease has already moved "
                    "there, so this is the one state to settle forward rather than back"
                ),
            )
        return None


def build_effects(adapters: RelocateAdapters) -> PhaseEffects:
    """Bind one :class:`RelocateAdapters` to the driver's phase slots."""
    return PhaseEffects(
        drain_source=adapters.drain_source,
        stop_source=adapters.stop_source,
        transport_transcript=adapters.transport,
        start_target_standby=adapters.start_target_standby,
        handshake=adapters.handshake,
        hand_over_lease=adapters.hand_over_lease,
        finish=adapters.finish,
    )


def adapters_for(
    *,
    agent: str,
    spec: dict,
    from_host: str,
    to_host: str,
    local_host: str | None,
    spec_path: str = "",
    stamp: str | None = None,
    exec_fn: Callable[..., dict] | None = None,
) -> RelocateAdapters:
    """The usual construction: two shells, one timestamp, nothing else decided.

    ``spec_path`` is the FILE the parsed ``spec`` came from. Both are needed and
    they are not the same thing: the dict is what this code reasons about, and
    the file is what the target must end up holding — re-serialising the dict
    would hand another machine a spec with the operator's comments and layout
    silently rewritten by a yaml dumper.
    """
    return RelocateAdapters(
        agent=agent,
        spec=spec,
        spec_path=spec_path,
        from_host=from_host,
        to_host=to_host,
        source=shell_for(from_host, local_host=local_host),
        target=shell_for(to_host, local_host=local_host),
        stamp=stamp or time.strftime("%Y%m%dT%H%M%SZ", time.gmtime()),
        exec_fn=exec_fn,
    )
