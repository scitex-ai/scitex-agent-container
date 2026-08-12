"""The sixteen callables :class:`.._relocate_probe.TargetProbes` asks for.

:mod:`_relocate_probe` is the PORT — pure orchestration that turns any raising
callable into ``None``. This is the ADAPTER: it reads the agent's spec, asks the
target once (:mod:`_relocate_probe_ssh` + :mod:`_relocate_probe_script`), and
hands back sixteen closures over that single answer. Five of them — everything
about sac ITSELF on that host — live in :mod:`_relocate_probe_sac` and arrive
here as a mixin over the same memoized round trip.

HOW PER-FACT DEGRADATION SURVIVES BATCHING — the design point of this file.
One remote call answers all sixteen questions, which is the only way this is fast
enough to be run casually. The obvious way to do that is also the dangerous one:
one blob, one status, sixteen facts that stand or fall together. Three rules keep
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

So a run that answers thirteen of sixteen yields thirteen OBSERVED facts and
three unknowns, each naming its own cause. A transport failure yields sixteen
unknowns sharing one cause. Neither yields a single ``False``.

NEVER ``False`` FOR "I COULD NOT TELL". Every accessor below either returns
something the target actually said or raises. There is no ``except: return
False`` here, and there must never be: it would turn "no route to the host" into
"the host has no image", and the relocation would then proceed on fiction — the
exact 2026-08-07 failure this command exists to prevent.

WHAT IS MEASURED VS WHAT IS READ. Fourteen facts are measured on the target. Two
are not, and each says so: ``card_store_url`` is READ FROM THE SPEC — the URL the
agent WOULD dial after the move, supplied only so a "card store not reachable"
failure can name WHICH store rather than starting an investigation — and
``preamble_declared`` is OBSERVED BY CONSTRUCTION, because what this prober
prepended to its own script is not something the target has to be asked.
"""

from __future__ import annotations

import os
from typing import Callable
from urllib.parse import urlparse

from ._relocate_probe import FactUnavailable, TargetProbes
from ._relocate_probe_sac import SacFacts
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


def _body(spec: dict) -> dict:
    inner = spec.get("spec")
    return inner if isinstance(inner, dict) else spec


def _apptainer(spec: dict) -> dict:
    app = _body(spec).get("apptainer")
    return app if isinstance(app, dict) else {}


def card_store_url_from_spec(spec: dict) -> str:
    """The ``SCITEX_CARDS_DB`` the agent would use, wherever the spec hides it.

    Two places, both real: ``apptainer.env`` and the ``--env KEY=VALUE`` pairs in
    ``apptainer.raw_args``. This repo's own spec uses the second and leaves the
    first an empty mapping, so a reader of ``env`` alone concludes the agent has
    no card store — and then the store check has nothing to check while looking
    like it passed.
    """
    app = _apptainer(spec)
    env = app.get("env")
    if isinstance(env, dict):
        value = env.get("SCITEX_CARDS_DB")
        if isinstance(value, str) and value:
            return value
    raw = app.get("raw_args")
    if isinstance(raw, list):
        for item in raw:
            if isinstance(item, str) and item.startswith("SCITEX_CARDS_DB="):
                return item.split("=", 1)[1]
    return ""


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


def _credential_paths(spec: dict) -> tuple[str, ...]:
    claude = _body(spec).get("claude")
    if not isinstance(claude, dict):
        return ()
    paths: list[str] = []
    single = claude.get("credentials_file")
    if isinstance(single, str) and single.strip():
        paths.append(single.strip())
    listed = claude.get("credentials_files")
    if isinstance(listed, list):
        paths += [p.strip() for p in listed if isinstance(p, str) and p.strip()]
    return tuple(dict.fromkeys(paths))


def _bind_sources(spec: dict) -> tuple[str, ...]:
    binds = _apptainer(spec).get("binds")
    if not isinstance(binds, list):
        return ()
    return tuple(
        b.split(":", 1)[0] for b in binds if isinstance(b, str) and b.split(":", 1)[0]
    )


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
    store_host, store_port = _endpoint(card_store_url_from_spec(spec))
    hub_host, hub_port, _ = hub_address(env)
    image = _apptainer(spec).get("image")
    return RemoteQuestions(
        image=image if isinstance(image, str) else "",
        bind_sources=_bind_sources(spec),
        card_store_host=store_host,
        card_store_port=store_port,
        credential_paths=_credential_paths(spec),
        required_ports=tuple(required_ports),
        hub_host=hub_host,
        hub_port=hub_port,
    )


class TargetBatch(SacFacts):
    """One ssh round trip, memoized, read sixteen different ways.

    The five facts about sac ITSELF — the three PATH lookups, whether a peer
    preamble was in play, and whether the target's own start command would
    accept this agent — arrive as :class:`._relocate_probe_sac.SacFacts`, so a
    reader looking for "what did we learn about sac over there" finds a file
    about that rather than five methods among the credentials and the ports.

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
    """Bind the sixteen accessors of one :class:`TargetBatch` into a probe set.

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
        sac_usable_path=batch.sac_usable_path,
        preamble_declared=batch.preamble_declared,
        spec_source_drift=batch.spec_source_drift,
    )
    return probes, batch
