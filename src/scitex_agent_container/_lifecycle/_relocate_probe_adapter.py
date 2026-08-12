"""The thirteen callables :class:`.._relocate_probe.TargetProbes` asks for.

:mod:`_relocate_probe` is the PORT — pure orchestration that turns any raising
callable into ``None``. This is the ADAPTER: it reads the agent's spec, asks the
target once (:mod:`_relocate_probe_ssh` + :mod:`_relocate_probe_script`), and
hands back thirteen closures over that single answer.

HOW PER-FACT DEGRADATION SURVIVES BATCHING — the design point of this file.
One remote call answers all thirteen questions, which is the only way this is fast
enough to be run casually. The obvious way to do that is also the dangerous one:
one blob, one status, thirteen facts that stand or fall together. Three rules keep
them independent, and they compose:

    the SCRIPT   prints each answer on its own marker line the moment it is
                 known, and never runs under ``set -e``, so a section that dies
                 cannot retract or prevent the others (see
                 :mod:`_relocate_probe_script`).
    the PARSER   reports only what it SAW. A line that never arrived leaves no
                 key; it does not leave a default.
    THIS FILE    gives every fact its own accessor, and an accessor whose
                 evidence is missing RAISES — with a sentence saying which
                 evidence and why it might be absent. ``probe()`` turns that
                 raise into ``None`` plus the reason, so the operator reads
                 "credentials_valid: UNKNOWN (no credential file exists on the
                 target among: …)" rather than a bare UNKNOWN.

So a run that answers ten of thirteen yields ten OBSERVED facts and three
unknowns, each naming its own cause. A transport failure yields thirteen unknowns
sharing one cause. Neither yields a single ``False``.

NEVER ``False`` FOR "I COULD NOT TELL". Every accessor below either returns
something the target actually said or raises. There is no ``except: return
False`` here, and there must never be: it would turn "no route to the host" into
"the host has no image", and the relocation would then proceed on fiction — the
exact 2026-08-07 failure this command exists to prevent.

WHAT IS MEASURED VS WHAT IS READ. Twelve facts are measured on the target. One —
``card_store_url`` — is READ FROM THE SPEC: it is the URL the agent WOULD dial
after the move, and preflight uses it only to name the endpoint in a failure
message. It is supplied because a failure that says "card store not reachable"
without saying WHICH is an error message that starts an investigation instead of
ending one.
"""

from __future__ import annotations

import os
from typing import Callable
from urllib.parse import urlparse

from ._relocate_probe import TargetProbes
from ._relocate_probe_script import (
    REMOTE_DEFAULT_CREDENTIAL,
    RemoteQuestions,
    RemoteReadout,
    parse_probe_output,
    render_probe_script,
)
from ._relocate_probe_ssh import (
    DEFAULT_TIMEOUT_S,
    ProbeTransportError,
    RemoteRun,
    peer_preamble,
    run_probe_script,
)
from ._relocate_spec_reads import (
    apptainer_section,
    bind_sources_from_spec,
    card_store_url_from_spec,
    credential_paths_from_spec,
    declared_groups_from_spec,
    group_labels_from_spec,
    workdirs_from_spec,
)

__all__ = [
    "FactUnavailable",
    "TargetBatch",
    "build_target_probes",
    "card_store_url_from_spec",
    "hub_address",
    "questions_from_spec",
]

#: Operator override for the hub address AS SEEN FROM THE TARGET, ``host:port``.
HUB_ADDR_ENV = "SAC_RELOCATE_HUB_ADDR"

_LOOPBACK = frozenset({"127.0.0.1", "localhost", "::1", "0.0.0.0", ""})


class FactUnavailable(RuntimeError):
    """This fact was not observed, and here is the sentence explaining why.

    Raised — never returned as a falsy value. :func:`.._relocate_probe.probe`
    catches it, records the message, and leaves the fact ``None``.
    """


def _endpoint(url: str) -> tuple[str, int]:
    """``postgresql://user@host:5432/db`` -> ``("host", 5432)``; ``("", 0)`` if unusable."""
    if not url:
        return ("", 0)
    try:
        parsed = urlparse(url)
        host = parsed.hostname or ""
        port = parsed.port or 0
    except ValueError:
        return ("", 0)
    return (host, port) if host and port else ("", 0)


def hub_address(env: dict[str, str] | None = None) -> tuple[str, int, str]:
    """The hub as an address that MEANS SOMETHING ON THE TARGET, or a refusal.

    Returns ``(host, port, reason_it_cannot_be_probed)``; exactly one side is
    populated.

    THE LOOPBACK TRAP IS WHY THIS RETURNS A REASON RATHER THAN A GUESS. sac's
    listen daemon is normally reached at ``http://127.0.0.1:7878``. Handing that
    to the target would measure the TARGET's own loopback — answering a
    different question, confidently, in the same words. That is precisely the
    mistake :data:`.._relocate_preflight.CHECK_HUB_FROM_TARGET` was written
    about ("reaching them from HERE proves nothing about THERE"), so a loopback
    hub address yields an honest UNKNOWN naming the override to set.

    The card store is deliberately NOT treated this way: there, loopback is the
    right thing to probe, because after the move the agent runs on the target
    and ``127.0.0.1`` means the target's own database.
    """
    env = os.environ if env is None else env
    override = (env.get(HUB_ADDR_ENV) or "").strip()
    if override:
        host, _, raw_port = override.rpartition(":")
        if host and raw_port.isdigit():
            return (host, int(raw_port), "")
        return (
            "",
            0,
            f"{HUB_ADDR_ENV}={override!r} is not a 'host:port' address",
        )
    base = (env.get("SAC_LISTEN_BASE_URL") or "").strip()
    host, port = _endpoint(base)
    if host and host not in _LOOPBACK:
        return (host, port, "")
    where = base or "(SAC_LISTEN_BASE_URL unset)"
    return (
        "",
        0,
        f"the hub is only known by a loopback address here ({where}); probing that "
        f"FROM the target would measure the TARGET's loopback, not the hub. Set "
        f"{HUB_ADDR_ENV}=<host:port> to the address the target should reach it on.",
    )


def questions_from_spec(
    spec: dict,
    *,
    required_ports: tuple[int, ...] = (),
    env: dict[str, str] | None = None,
) -> RemoteQuestions:
    """Turn the agent's spec into the set of questions to ask its future host."""
    import json

    store_host, store_port = _endpoint(card_store_url_from_spec(spec))
    hub_host, hub_port, _ = hub_address(env)
    image = apptainer_section(spec).get("image")
    # Asked only when the spec actually claims a group. An empty labels blob
    # would make the target answer "no groups", which is a real-looking verdict
    # about a question nobody asked.
    labels_json = (
        json.dumps(group_labels_from_spec(spec))
        if declared_groups_from_spec(spec)
        else ""
    )
    return RemoteQuestions(
        image=image if isinstance(image, str) else "",
        bind_sources=bind_sources_from_spec(spec),
        workdirs=workdirs_from_spec(spec),
        group_labels_json=labels_json,
        card_store_host=store_host,
        card_store_port=store_port,
        credential_paths=credential_paths_from_spec(spec),
        required_ports=tuple(required_ports),
        hub_host=hub_host,
        hub_port=hub_port,
    )


class TargetBatch:
    """One ssh round trip, memoized, read thirteen different ways.

    The run happens on first access and NOT in ``__init__``: a caller that
    builds probes but never gathers must not open a connection. The outcome —
    success or transport failure — is remembered, so thirteen accessors cost one
    ssh, and a target that is down is not dialled thirteen times.
    """

    def __init__(
        self,
        host: str,
        questions: RemoteQuestions,
        *,
        spec: dict | None = None,
        runner: Callable[..., RemoteRun] | None = None,
        preamble: str | None = None,
        timeout_s: float = DEFAULT_TIMEOUT_S,
        env: dict[str, str] | None = None,
    ) -> None:
        self.host = host
        self.questions = questions
        self.spec = spec or {}
        self._runner = runner if runner is not None else run_probe_script
        self._preamble = peer_preamble(host) if preamble is None else preamble
        self._timeout_s = timeout_s
        self._hub_reason = hub_address(env)[2]
        self._run: RemoteRun | None = None
        self._readout: RemoteReadout | None = None
        self._error: ProbeTransportError | None = None

    # -- the single call ---------------------------------------------------
    def _ensure(self) -> None:
        if self._readout is not None or self._error is not None:
            return
        script = render_probe_script(self.questions, preamble=self._preamble)
        try:
            self._run = self._runner(self.host, script, timeout_s=self._timeout_s)
        except ProbeTransportError as exc:
            self._error = exc
            return
        self._readout = parse_probe_output(self._run.stdout)

    def readout(self) -> RemoteReadout:
        self._ensure()
        if self._error is not None:
            raise FactUnavailable(str(self._error))
        assert self._readout is not None
        return self._readout

    def _field(self, key: str, what: str) -> str:
        value = self.readout().fields.get(key)
        if value is None:
            raise FactUnavailable(
                f"the target printed no {what} line; that section of the probe "
                "did not run (the earlier sections' answers are still good)"
            )
        return value

    # -- the thirteen facts --------------------------------------------------
    def reachable(self) -> bool:
        """True when the target ran our script; False only when ssh itself failed."""
        self._ensure()
        if self._error is not None:
            raise FactUnavailable(str(self._error))
        if self._readout is not None and self._readout.started:
            return True
        run = self._run
        if run is not None and run.ssh_failed:
            # ssh's own exit 255: the connection did not happen. That IS an
            # observation about the target, unlike a transport failure above.
            return False
        tail = (run.stderr.strip().splitlines()[-1:] or [""])[0] if run else ""
        raise FactUnavailable(
            f"ssh to {self.host} exited {run.exit_code if run else '?'} without the "
            f"probe's opening marker, so whether the host answered is undetermined: {tail[:160]}"
        )

    def image_present(self) -> bool:
        if not self.questions.image:
            raise FactUnavailable(
                "the spec declares no apptainer image, so nothing was asked about "
                "on the target; add spec.apptainer.image to make this measurable"
            )
        value = self._field("image", "image")
        if value == "present":
            return True
        if value == "absent":
            return False
        raise FactUnavailable(f"the target reported image={value!r}, which is neither")

    def missing_bind_sources(self) -> tuple[str, ...]:
        wanted = self.questions.bind_sources
        if not wanted:
            # Nothing declared: no bind source can be missing. Observed by
            # construction, not by defaulting.
            return ()
        checked = self._field("binds_checked", "bind-check")
        if checked != str(len(wanted)):
            raise FactUnavailable(
                f"the target reported checking {checked!r} of {len(wanted)} bind "
                "sources; a partial sweep cannot distinguish 'present' from 'not reached'"
            )
        return self.readout().missing_binds

    def missing_workdir_paths(self) -> tuple[str, ...]:
        wanted = self.questions.workdirs
        if not wanted:
            # The spec declares no workdir, so no workdir can be missing.
            # Observed by construction, not by defaulting.
            return ()
        checked = self._field("workdirs_checked", "workdir-check")
        if checked != str(len(wanted)):
            raise FactUnavailable(
                f"the target reported checking {checked!r} of {len(wanted)} workdir(s); "
                "a partial sweep cannot distinguish 'present' from 'not reached'"
            )
        return self.readout().missing_workdirs

    def target_resolved_groups(self) -> tuple[str, ...]:
        """What the TARGET's own sac makes of this spec's group labels.

        An EMPTY value is a measurement, not a missing one: it is what a daemon
        too old to read spec labels answers for every agent, and the check treats
        it as undetermined for that reason — but it must reach the check as an
        observed empty tuple, because "answered nothing" and "was never asked"
        need different sentences. Only an absent line is unknown here.
        """
        if not self.questions.group_labels_json:
            raise FactUnavailable(
                "this spec declares no groups under metadata.labels, so nothing was "
                "asked of the target about them"
            )
        value = self.readout().fields.get("groups")
        if value is None:
            raise FactUnavailable(
                "the target's sac did not resolve group labels — it is not importable "
                "there, or is too old to carry config._group_resolver.all_named_groups. "
                "An agent moved onto such a host holds its groups on paper and is "
                f"refused 403 by every group-gated call. Check with: ssh {self.host} "
                "'sac --version'"
            )
        return tuple(v for v in value.split(",") if v)

    def card_store_url(self) -> str:
        url = card_store_url_from_spec(self.spec)
        if not url:
            raise FactUnavailable(
                "no SCITEX_CARDS_DB is declared for this agent (checked "
                "spec.apptainer.env and the --env pairs in spec.apptainer.raw_args)"
            )
        return url

    def card_store_reachable(self) -> bool:
        if not self.questions.card_store_host:
            raise FactUnavailable(
                "the declared SCITEX_CARDS_DB has no host:port to dial, so "
                "reachability from the target could not be measured"
            )
        return self._yes_no("cardstore", "card-store")

    def _yes_no(self, key: str, what: str) -> bool:
        value = self._field(key, what)
        if value == "yes":
            return True
        if value == "no":
            return False
        raise FactUnavailable(
            f"the target could not run a TCP probe for the {what} (neither python3 "
            "nor a -z-capable nc is installed there), so this is undetermined "
            "rather than closed"
        )

    def _chosen_credential(self):
        """The credential the target would actually be able to use.

        Among the files that EXIST there, the one expiring LAST — mirroring
        sac's own healthy-account picker. Both credential facts read this same
        file, so the report never describes an expiry from one file and a
        refresh token from another.
        """
        creds = self.readout().credentials
        dated = [c for c in creds if c.expires_at_ms is not None]
        if not dated:
            candidates = ", ".join(
                (*self.questions.credential_paths, REMOTE_DEFAULT_CREDENTIAL)
            )
            raise FactUnavailable(
                "no credential file with a readable claudeAiOauth.expiresAt exists "
                f"on the target among: {candidates}"
            )
        return max(dated, key=lambda c: c.expires_at_ms or 0.0)

    def credential_expires_in_s(self) -> float:
        chosen = self._chosen_credential()
        raw_epoch = self._field("epoch", "clock")
        try:
            epoch = float(raw_epoch)
        except ValueError as exc:
            raise FactUnavailable(
                f"the target's clock came back as {raw_epoch!r}"
            ) from exc
        # The TARGET's clock, not ours: an expiry compared against the wrong
        # machine's time is wrong by exactly the skew between them.
        return (chosen.expires_at_ms or 0.0) / 1000.0 - epoch

    def credential_refresh_token_present(self) -> bool:
        chosen = self._chosen_credential()
        if chosen.refresh_present is None:
            raise FactUnavailable(
                f"the target could not report whether {chosen.path} carries a refreshToken"
            )
        return chosen.refresh_present

    def supported_runtimes(self) -> tuple[str, ...]:
        value = self.readout().fields.get("runtimes")
        if not value:
            raise FactUnavailable(
                "the target's sac did not report which runtimes it accepts — sac is "
                "not importable there, or is too old to carry the symbol. Check with: "
                f"ssh {self.host} 'sac --version'"
            )
        return tuple(v for v in value.split(",") if v)

    def rejected_spec_keys(self) -> tuple[str, ...]:
        value = self.readout().fields.get("speckeys")
        if not value:
            raise FactUnavailable(
                "the target's sac did not report which top-level spec keys it knows, "
                "so whether it would reject this spec is undetermined"
            )
        known = {k for k in value.split(",") if k}
        ours = [k for k in self.spec if isinstance(k, str)]
        return tuple(sorted(k for k in ours if k not in known))

    def ports_in_use(self) -> tuple[int, ...]:
        wanted = self.questions.required_ports
        if not wanted:
            # The spec pins no port (``a2a.port: auto`` defers to boot), so none
            # of the required ports can clash. No tool on the target is needed
            # to know that, and none was asked for.
            return ()
        checked = self.readout().fields.get("ports_checked")
        if checked != str(len(wanted)):
            raise FactUnavailable(
                f"the target could not list listening ports (neither ss nor netstat "
                f"is installed there), so whether {wanted} are free is undetermined"
            )
        return self.readout().ports_in_use

    def hub_reachable_from_target(self) -> bool:
        if not self.questions.hub_host:
            raise FactUnavailable(self._hub_reason or "the hub address is unknown")
        return self._yes_no("hub", "hub")

    def sac_on_path(self) -> bool:
        """Whether ``command -v sac`` answers under the RAW ssh PATH.

        An empty value is the ANSWER (nothing on PATH), not a missing one — the
        script prints the line either way. Only a line that never arrived is
        unknown, which :meth:`_field` raises for.
        """
        return bool(self._field("sac_path", "sac-on-PATH").strip())

    def sac_resolved_path(self) -> str:
        """Where sac actually is, or ``""`` for looked-and-found-nothing.

        The empty string is load-bearing and must NOT be turned into a raise:
        it is what separates "sac is not installed on this host" from "sac is
        installed and the ssh PATH cannot see it", and those need opposite
        fixes. Only an absent line is undetermined.
        """
        return self._field("sac_found", "sac-location").strip()


def build_target_probes(
    to_host: str,
    spec: dict,
    *,
    required_ports: tuple[int, ...] = (),
    runner: Callable[..., RemoteRun] | None = None,
    preamble: str | None = None,
    timeout_s: float = DEFAULT_TIMEOUT_S,
    env: dict[str, str] | None = None,
) -> tuple[TargetProbes, TargetBatch]:
    """Bind the thirteen accessors of one :class:`TargetBatch` into a probe set.

    Returns the probes and the batch, so a caller that wants the raw readout
    (for diagnostics) does not have to run the probe twice.
    """
    questions = questions_from_spec(spec, required_ports=required_ports, env=env)
    batch = TargetBatch(
        to_host,
        questions,
        spec=spec,
        runner=runner,
        preamble=preamble,
        timeout_s=timeout_s,
        env=env,
    )
    probes = TargetProbes(
        reachable=batch.reachable,
        image_present=batch.image_present,
        missing_bind_sources=batch.missing_bind_sources,
        missing_workdir_paths=batch.missing_workdir_paths,
        target_resolved_groups=batch.target_resolved_groups,
        card_store_url=batch.card_store_url,
        card_store_reachable=batch.card_store_reachable,
        credential_expires_in_s=batch.credential_expires_in_s,
        credential_refresh_token_present=batch.credential_refresh_token_present,
        supported_runtimes=batch.supported_runtimes,
        rejected_spec_keys=batch.rejected_spec_keys,
        ports_in_use=batch.ports_in_use,
        hub_reachable_from_target=batch.hub_reachable_from_target,
        sac_on_path=batch.sac_on_path,
        sac_resolved_path=batch.sac_resolved_path,
    )
    return probes, batch
