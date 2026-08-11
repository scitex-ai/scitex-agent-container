"""The TRANSPORT phase: derive both directories, snapshot the source, copy, confirm.

Split from :mod:`_relocate_effects` exactly as the source, standby, handshake and
handover phases already are — one file per phase's worth of decisions, so a reader
looking for "what does the transport do" finds a file about the transport rather
than a method in the middle of the adapters.

THE ORDER OF THE THREE MEASUREMENTS IS THE WHOLE DESIGN.

    SNAPSHOT FIRST, ON THE SOURCE. :func:`.._relocate_transport_ssh.
    snapshot_transcripts` records the offset of each file's LAST NEWLINE before
    a byte moves. That number is the bound on the copy and the baseline for the
    verdict, and once it is taken the source may grow freely without changing
    the answer.

    COPY, BOUNDED BY THAT NUMBER. Exactly the snapshotted bytes travel, so the
    target receives whole lines and never a torn final record.

    MEASURE THE TARGET, COMPARE AGAINST THE SNAPSHOT. Not against a fresh
    reading of the source. Re-reading the source is the 2026-08-12 abort: the
    file had grown 11,717 bytes on an identical line count between the
    measurement and the copy, and the transport reported a corruption that had
    not happened.

The copy pipeline's exit code is recorded as EVIDENCE and is never the answer.
"""

from __future__ import annotations

from ._relocate_execute import StepResult
from ._relocate_liveness import observe_running
from ._relocate_quiescence import sample_transcripts
from ._relocate_session_choice import CODE_UNKNOWN, choose_session
from ._relocate_shell import resolved_path
from ._relocate_target_ssh import SID_ABSENT, read_session_marker, target_home
from ._relocate_transcript_home import transcript_home_from_spec
from ._relocate_transport import plan_transport, verify_arrival
from ._relocate_transport_paths import derive_target_dir
from ._relocate_transport_ssh import (
    copy_transcripts,
    ensure_dir,
    list_transcript_dir,
    measure_transcripts,
    move_dir_aside,
    snapshot_transcripts,
)

__all__ = ["TransportEffects"]


class TransportEffects:
    """Mixin: the transport phase. Expects the attributes of ``RelocateAdapters``."""

    def _workdir(self) -> str:
        body = (
            self.spec.get("spec")
            if isinstance(self.spec.get("spec"), dict)
            else self.spec
        )
        return str((body or {}).get("workdir") or "").strip()

    def source_transcript_dir(self) -> tuple[str | None, str | None, str]:
        """``(path, encoded_name, why)`` for the SOURCE's transcript directory.

        Derived by asking ONLY the source. Split out of :meth:`_resolve_dirs`
        because SOURCE_STOP needs it before the target is involved in anything:
        it waits for that directory to stop changing, and routing that through
        the full derivation would make the source's quiescence depend on the
        target answering a question about its own workdir — two hosts to fail
        where one will do.

        ``path`` is ``None`` when it could not be derived, and ``why`` then says
        which of the three questions went unanswered.
        """
        home = transcript_home_from_spec(self.spec)
        if home.path is None:
            return None, None, f"{home.reason} — {home.hint}"
        workdir = self._workdir()
        if not workdir:
            return (
                None,
                None,
                "the spec declares no workdir, so no project directory can be encoded",
            )
        src_resolved = resolved_path(self.source, workdir, exec_fn=self.exec_fn)
        if not src_resolved:
            return (
                None,
                None,
                f"the SOURCE's resolved workdir for {workdir} was not observed",
            )
        src = derive_target_dir(
            target_home=home.path, target_resolved_workdir=src_resolved
        )
        return src.path, src.encoded, src.reason

    def _resolve_dirs(self) -> tuple[str | None, str | None, str]:
        """Source and target transcript directories, or ``(None, None, why)``.

        The target's directory name is RECOMPUTED from the target's own resolved
        workdir rather than copied from the source's — see
        :mod:`_relocate_transport_paths`. A mismatch is normal and is logged, not
        refused: it is the whole reason the name is derived instead of reused.
        """
        source_dir, source_encoded, why = self.source_transcript_dir()
        if source_dir is None:
            return None, None, why

        home = transcript_home_from_spec(self.spec)
        tgt_resolved = resolved_path(self.target, self._workdir(), exec_fn=self.exec_fn)
        tgt = derive_target_dir(
            target_home=home.path,
            target_resolved_workdir=tgt_resolved,
            source_dir_name=source_encoded,
        )
        if tgt.path is None:
            return None, None, f"{tgt.reason} — {tgt.hint}"
        if tgt.matches_source is False:
            self.log.append(f"transport: {tgt.reason}")
        return source_dir, tgt.path, tgt.reason

    def _prepare_destination(self, plan, target_dir: str) -> StepResult | None:
        """Move aside whatever is there, then make sure the directory exists.

        ``None`` when the destination is ready. Nothing is overwritten and
        nothing is deleted: what is at the destination may be the only copy of an
        earlier conversation, and the move-aside is also what makes a retry
        idempotent.
        """
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
                f"transport: moved {self.to_host}:{target_dir} aside to "
                f"{plan.move_aside.destination}"
            )

        made = ensure_dir(self.target, target_dir, exec_fn=self.exec_fn)
        if made is not True:
            return StepResult(
                ok=None if made is None else False,
                detail=(
                    f"the destination {target_dir} on {self.to_host} could not be created"
                ),
                hint="check write permission on the target's transcript root, then re-run",
            )
        return None

    def _snapshot(self, source_dir: str, names) -> StepResult | None:
        """Record the last-newline offset for every file that will travel.

        ``None`` when every name was snapshotted. A name with no recorded offset
        refuses BEFORE anything moves: carrying "however much is there now" is
        precisely the race the offset exists to remove, and the mismatch it
        eventually produces is reported against a source that has already moved.
        """
        self.sent = snapshot_transcripts(
            self.source, source_dir, names, exec_fn=self.exec_fn
        )
        for f in self.sent:
            self.log.append(
                f"transport: snapshot {f.name} at {f.byte_count} bytes / "
                f"{f.line_count} complete lines — the offset of its last newline; "
                "the source may grow past this and the verdict does not change"
            )
        snapshotted = {f.name for f in self.sent if f.measured}
        missing = [n for n in names if n not in snapshotted]
        if not missing:
            return None
        return StepResult(
            ok=None,
            detail=(
                f"no snapshot offset could be taken on {self.from_host} for: "
                + ", ".join(missing)
            ),
            hint=(
                "read the source's transcript directory again before copying. A file "
                "with no recorded offset would have to be carried as 'however much is "
                "there now', which is the race the snapshot removes"
            ),
        )

    def _choose_session(self, source_dir: str, carried) -> StepResult | None:
        """Name the conversation the target will resume. ``None`` when named.

        RUN BEFORE ANY BYTES MOVE, deliberately. The old guard — take the session
        id only when exactly one transcript was carried — deferred this question
        to TARGET_STANDBY, which is AFTER ``source_stop`` has taken the agent
        down. Measured 2026-08-12: every one of the ten agents left on
        ywata-note-win has 2-5 transcripts, so every one of them passed preflight
        and then stopped, copied, and refused with the agent already off.

        The marker is read from the SOURCE's own sac tree, which is rooted at the
        ssh user's ``$HOME`` there — the same home
        :mod:`_relocate_effects_standby` writes the target's marker under, and
        deliberately NOT the container home the transcripts follow.
        """
        home = target_home(self.source, exec_fn=self.exec_fn)
        marker: str | None = None
        if home:
            state_dir = f"{home}/.scitex/agent-container/runtime/{self.agent}"
            raw = read_session_marker(self.source, state_dir, exec_fn=self.exec_fn)
            if raw is not None:
                marker = "" if raw == SID_ABSENT else raw
        sample = sample_transcripts(self.source, source_dir, exec_fn=self.exec_fn)
        mtimes = {
            f.name: int(f.mtime)
            for f in (sample or ())
            if f.mtime is not None and f.mtime.lstrip("-").isdigit()
        }
        choice = choose_session(
            agent=self.agent, carried=carried, marker=marker, mtimes=mtimes
        )
        self.log.append(
            f"transport: session marker on {self.from_host} reads {marker!r}; "
            f"candidates {list(choice.candidates)}"
        )
        if choice.session is None:
            return StepResult(
                ok=None if choice.code == CODE_UNKNOWN else False,
                detail=f"the session to resume could not be chosen ({choice.code}): {choice.reason}",
                hint=choice.hint,
            )
        self.session_uuid = choice.session
        self.log.append(
            f"transport: resuming session {choice.session} — chosen by {choice.chosen_by}"
        )
        return None

    def transport(self) -> StepResult:
        """Copy the transcript across and CONFIRM it on the target, per file.

        List the source, let :func:`plan_transport` decide what may travel, move
        aside anything already at the destination, SNAPSHOT the source, copy
        exactly that snapshot, then measure THE TARGET and compare it against the
        snapshot. That last comparison is the only statement this step makes
        about success.
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

        # BEFORE anything moves: if the conversation to resume cannot be named,
        # the relocation must stop here rather than at TARGET_STANDBY, which runs
        # with the agent already stopped on the source.
        refused = self._choose_session(source_dir, plan.files)
        if refused is not None:
            return refused

        refused = self._prepare_destination(plan, target_dir)
        if refused is not None:
            return refused

        refused = self._snapshot(source_dir, plan.files)
        if refused is not None:
            return refused

        run = copy_transcripts(
            source=self.source,
            source_dir=source_dir,
            target=self.target,
            target_dir=target_dir,
            files=self.sent,
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
                f"transport: target holds {f.name} {f.byte_count} bytes / "
                f"{f.line_count} lines"
            )
        if verdict.arrived is not True:
            return StepResult(
                ok=False if verdict.arrived is False else None,
                detail=f"arrival not confirmed ({verdict.code}): {verdict.reason}",
                hint=verdict.hint,
            )
        return StepResult(
            ok=True,
            detail=(
                f"{verdict.reason}; {self.from_host}:{source_dir} -> "
                f"{self.to_host}:{target_dir}"
            ),
        )
