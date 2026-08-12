"""Eleven facts from one call, and a partial answer that costs only its own facts.

Two properties are pinned here, and the second is the reason the first is safe.

THE INVARIANT: could-not-determine is ``None``, NEVER ``False``. A probe that
degrades a failure into a negative turns "no route to the host" into "the host
has no image", and preflight then refuses — or passes — for a reason nobody can
trace. Several tests below fail loudly if any accessor grows an
``except: return False``; that is deliberate, and they were verified to fail
against a deliberately neutered implementation before being kept.

PER-FACT DEGRADATION UNDER BATCHING: one remote call answers everything, so the
tempting failure mode is all-or-nothing — eleven unknowns because one section
died, or (worse) eleven falses. The tests drive truncated, partial and malformed
readouts and assert that the facts printed BEFORE the failure are still observed
while only the unprinted ones are unknown.

The transport is driven through the ``runner`` seam with real callables that
return canned :class:`RemoteRun` values — the exact shape
:func:`.._relocate_probe_ssh.run_probe_script` returns. Nothing is mocked and
nothing is monkeypatched.
"""

from __future__ import annotations

import pytest

from scitex_agent_container._lifecycle._relocate_origin import RepoWork
from scitex_agent_container._lifecycle._relocate_preflight import SourceFacts, preflight
from scitex_agent_container._lifecycle._relocate_probe import gather_target_facts
from scitex_agent_container._lifecycle._relocate_probe_adapter import (
    build_target_probes,
    card_store_url_from_spec,
    hub_address,
)
from scitex_agent_container._lifecycle._relocate_probe_ssh import (
    ProbeTransportError,
    RemoteRun,
)

# A healthy target, in the shape the real script prints. `epoch` and the
# credential expiry are chosen so the credential has an hour left.
HEALTHY = """SAC_RELOC begin
SAC_RELOC epoch=1786246196
SAC_RELOC image=present
SAC_RELOC binds_checked=2
SAC_RELOC cardstore=yes
SAC_RELOC cred=/creds/a.json|1786249796000|yes
SAC_RELOC creds_checked=2
SAC_RELOC ports_checked=0
SAC_RELOC hub=yes
SAC_RELOC sac_path=/usr/local/bin/sac
SAC_RELOC sac_found=/usr/local/bin/sac
SAC_RELOC runtimes=apptainer,claude-agent-sdk,tui
SAC_RELOC speckeys=apiVersion,kind,metadata,spec
SAC_RELOC end
"""

#: The scitex-compute-04 readout, measured 2026-08-11: sac is installed and the
#: non-interactive ssh PATH cannot see it, so `ssh compute-04 sac …` answers
#: "No such file or directory" while sac works perfectly well there.
SAC_OFF_PATH = HEALTHY.replace(
    "SAC_RELOC sac_path=/usr/local/bin/sac",
    "SAC_RELOC sac_path=",
).replace(
    "SAC_RELOC sac_found=/usr/local/bin/sac",
    "SAC_RELOC sac_found=/home/ywatanabe/.env-sac/bin/sac",
)

# An env with a routable hub, so the hub section is asked about at all.
HUB_ENV = {"SAC_RELOCATE_HUB_ADDR": "hub.example:7878"}


@pytest.fixture
def spec():
    """A spec in this repo's real shape: card store in a --env raw arg."""
    yield {
        "apiVersion": "scitex-agent-container/v3",
        "kind": "Agent",
        "metadata": {},
        "spec": {
            "runtime": "tui",
            "host": "ywata-note-win",
            "apptainer": {
                "image": "/srv/sac-base.sif",
                "binds": ["/home/ywatanabe:/home/ywatanabe:rw", "/mnt/c:/mnt/c:rw"],
                "env": {},
                "raw_args": [
                    "--env",
                    "SCITEX_CARDS_DB=postgresql://cards@127.0.0.1:5432/cards",
                ],
            },
            "claude": {"credentials_files": ["/creds/a.json"]},
        },
    }


def _runner(stdout, *, exit_code=0, stderr="", calls=None):
    """A real ``run_probe_script``-shaped callable returning canned output."""

    def run(host, script, *, timeout_s=60.0, **kwargs):
        if calls is not None:
            calls.append(host)
        return RemoteRun(stdout=stdout, stderr=stderr, exit_code=exit_code)

    return run


def _runner_raising(exc):
    def run(host, script, *, timeout_s=60.0, **kwargs):
        raise exc

    return run


def _facts(spec, stdout, *, exit_code=0, stderr="", required_ports=(), env=None):
    probes, _ = build_target_probes(
        "target",
        spec,
        required_ports=required_ports,
        runner=_runner(stdout, exit_code=exit_code, stderr=stderr),
        preamble="",
        env=HUB_ENV if env is None else env,
    )
    return gather_target_facts(probes)


# ---------------------------------------------------------------------------
# THE INVARIANT: a failure is UNKNOWN, never a negative
# ---------------------------------------------------------------------------


def test_a_transport_failure_leaves_reachability_unknown(spec) -> None:
    # Arrange: the listen daemon could not be reached, so NOTHING about the
    # target was measured. `False` here would claim the host refused us.
    probes, _ = build_target_probes(
        "target",
        spec,
        runner=_runner_raising(ProbeTransportError("listen daemon refused")),
        preamble="",
        env=HUB_ENV,
    )
    # Act
    facts = gather_target_facts(probes).facts
    # Assert
    assert facts.reachable is None


def test_a_transport_failure_leaves_the_image_unknown(spec) -> None:
    # Arrange: `False` here would read as "the image is absent on the target"
    # and send someone to rebuild an image that is already there.
    probes, _ = build_target_probes(
        "target",
        spec,
        runner=_runner_raising(ProbeTransportError("listen daemon refused")),
        preamble="",
        env=HUB_ENV,
    )
    # Act
    facts = gather_target_facts(probes).facts
    # Assert
    assert facts.image_present is None


def test_a_transport_failure_leaves_the_card_store_unknown(spec) -> None:
    # Arrange
    probes, _ = build_target_probes(
        "target",
        spec,
        runner=_runner_raising(ProbeTransportError("listen daemon refused")),
        preamble="",
        env=HUB_ENV,
    )
    # Act
    facts = gather_target_facts(probes).facts
    # Assert
    assert facts.card_store_reachable is None


def test_a_transport_failure_makes_preflight_refuse_as_undetermined(spec) -> None:
    # Arrange: the end-to-end statement of the invariant — a dead transport must
    # produce "could not determine", not a decided refusal built on falses.
    probes, _ = build_target_probes(
        "target",
        spec,
        runner=_runner_raising(ProbeTransportError("listen daemon refused")),
        preamble="",
        env=HUB_ENV,
    )
    gathered = gather_target_facts(probes)
    # Act
    report = preflight(agent="a", to_host="target", facts=gathered.facts, runtime="tui")
    # Assert
    assert report.ok is None


def test_a_transport_failure_says_why_each_fact_is_missing(spec) -> None:
    # Arrange: a bare UNKNOWN turns a five-second fix into an investigation.
    probes, _ = build_target_probes(
        "target",
        spec,
        runner=_runner_raising(ProbeTransportError("listen daemon refused")),
        preamble="",
        env=HUB_ENV,
    )
    # Act
    errors = gather_target_facts(probes).errors
    # Assert
    assert "listen daemon refused" in errors["image_present"]


def test_a_target_with_no_tcp_tool_is_unknown_not_unreachable(spec) -> None:
    # Arrange: the script reports `unknown` when the target has neither python3
    # nor a -z-capable nc. That is "I could not test", not "the port is shut".
    stdout = HEALTHY.replace("cardstore=yes", "cardstore=unknown")
    # Act
    facts = _facts(spec, stdout).facts
    # Assert
    assert facts.card_store_reachable is None


def test_a_missing_credential_file_is_unknown_not_expired(spec) -> None:
    # Arrange: no file means no expiry to report. Inventing a negative number
    # would fabricate a measurement nobody made.
    stdout = HEALTHY.replace("SAC_RELOC cred=/creds/a.json|1786249796000|yes\n", "")
    # Act
    facts = _facts(spec, stdout).facts
    # Assert
    assert facts.credential_expires_in_s is None


def test_a_partial_bind_sweep_is_unknown_rather_than_clean(spec) -> None:
    # Arrange: the target reported checking fewer paths than we asked about, so
    # "no missing binds" would mean "none among the ones it got to".
    stdout = HEALTHY.replace("binds_checked=2", "binds_checked=1")
    # Act
    facts = _facts(spec, stdout).facts
    # Assert
    assert facts.missing_bind_sources is None


def test_a_target_that_cannot_list_ports_is_unknown_not_free(spec) -> None:
    # Arrange: a pinned port and no ss/netstat on the target.
    stdout = HEALTHY.replace("ports_checked=0", "ports_tool=none")
    # Act
    facts = _facts(spec, stdout, required_ports=(7001,)).facts
    # Assert
    assert facts.ports_in_use is None


def test_a_loopback_hub_is_unknown_rather_than_unreachable(spec) -> None:
    # Arrange: probing 127.0.0.1 FROM the target measures the TARGET's loopback.
    # Reporting that as the hub would be a confident answer to another question.
    env = {"SAC_LISTEN_BASE_URL": "http://127.0.0.1:7878"}
    # Act
    facts = _facts(spec, HEALTHY, env=env).facts
    # Assert
    assert facts.hub_reachable_from_target is None


def test_a_loopback_hub_names_the_override_that_would_measure_it(spec) -> None:
    # Arrange
    env = {"SAC_LISTEN_BASE_URL": "http://127.0.0.1:7878"}
    # Act
    errors = _facts(spec, HEALTHY, env=env).errors
    # Assert
    assert "SAC_RELOCATE_HUB_ADDR" in errors["hub_reachable_from_target"]


# ---------------------------------------------------------------------------
# observed negatives must survive — the invariant must not blunt real answers
# ---------------------------------------------------------------------------


def test_an_absent_image_is_an_observed_negative(spec) -> None:
    # Arrange: the mirror of the invariant. If everything degraded to unknown
    # the checks would never fail, which is just as useless.
    stdout = HEALTHY.replace("image=present", "image=absent")
    # Act
    facts = _facts(spec, stdout).facts
    # Assert
    assert facts.image_present is False


def test_an_ssh_connection_failure_is_an_observed_unreachable(spec) -> None:
    # Arrange: ssh ran and could not connect (its exit 255). Unlike a transport
    # failure, that IS a measurement about the target.
    # Act
    facts = _facts(spec, "", exit_code=255, stderr="ssh: connect: timed out").facts
    # Assert
    assert facts.reachable is False


def test_an_expired_credential_is_reported_as_negative_seconds(spec) -> None:
    # Arrange: the 2026-08-07 failure — a file that is PRESENT and useless.
    stdout = HEALTHY.replace("1786249796000", "1780000000000")
    # Act
    expires_in = _facts(spec, stdout).facts.credential_expires_in_s
    # Assert
    assert expires_in < 0


def test_an_empty_refresh_token_is_an_observed_negative(spec) -> None:
    # Arrange: valid now and unrenewable — the agent dies at the first refresh.
    stdout = HEALTHY.replace("|1786249796000|yes", "|1786249796000|no")
    # Act
    facts = _facts(spec, stdout).facts
    # Assert
    assert facts.credential_refresh_token_present is False


# ---------------------------------------------------------------------------
# per-fact degradation: a partial batch costs only the facts it did not print
# ---------------------------------------------------------------------------


def test_a_truncated_batch_keeps_the_facts_it_printed(spec) -> None:
    # Arrange: the script died before the sac section. Everything printed
    # earlier was true when printed.
    truncated = HEALTHY.split("SAC_RELOC runtimes")[0]
    # Act
    facts = _facts(spec, truncated).facts
    # Assert
    assert facts.image_present is True


def test_a_truncated_batch_loses_only_the_facts_it_never_printed(spec) -> None:
    # Arrange
    truncated = HEALTHY.split("SAC_RELOC runtimes")[0]
    # Act
    facts = _facts(spec, truncated).facts
    # Assert
    assert facts.supported_runtimes is None


def test_a_truncated_batch_still_measures_most_of_the_checks(spec) -> None:
    # Arrange: the whole argument for batching — a partial answer is still an
    # answer for the parts that arrived.
    truncated = HEALTHY.split("SAC_RELOC runtimes")[0]
    gathered = _facts(spec, truncated)
    # Act
    unobserved = set(gathered.errors)
    # Assert
    assert unobserved == {"supported_runtimes", "rejected_spec_keys"}


def test_an_old_sac_on_the_target_costs_only_its_own_two_facts(spec) -> None:
    # Arrange: a target whose sac is too old to carry the validator symbols
    # prints neither line; everything the shell could answer still arrives.
    stdout = HEALTHY.replace(
        "SAC_RELOC runtimes=apptainer,claude-agent-sdk,tui\n", ""
    ).replace("SAC_RELOC speckeys=apiVersion,kind,metadata,spec\n", "")
    # Act
    facts = _facts(spec, stdout).facts
    # Assert
    assert facts.card_store_reachable is True


# ---------------------------------------------------------------------------
# the facts themselves
# ---------------------------------------------------------------------------


def test_the_batch_runs_once_for_all_eleven_facts(spec) -> None:
    # Arrange: eleven ssh round trips to a NAS is tens of seconds for a command
    # whose job is to say "not yet".
    calls: list[str] = []
    probes, _ = build_target_probes(
        "target",
        spec,
        runner=_runner(HEALTHY, calls=calls),
        preamble="",
        env=HUB_ENV,
    )
    # Act
    gather_target_facts(probes)
    # Assert
    assert len(calls) == 1


def test_a_dead_target_is_not_dialled_once_per_fact(spec) -> None:
    # Arrange: the failure is memoized too, or a down host costs eleven timeouts.
    calls: list[str] = []

    def run(host, script, *, timeout_s=60.0, **kwargs):
        calls.append(host)
        raise ProbeTransportError("down")

    probes, _ = build_target_probes(
        "target", spec, runner=run, preamble="", env=HUB_ENV
    )
    # Act
    gather_target_facts(probes)
    # Assert
    assert len(calls) == 1


def test_the_card_store_url_is_found_in_a_raw_env_arg(spec) -> None:
    # Arrange: this repo's own spec leaves apptainer.env empty and carries the
    # URL in a --env raw arg, so an env-only reader reports "(unset)".
    # Act
    url = card_store_url_from_spec(spec)
    # Assert
    assert url == "postgresql://cards@127.0.0.1:5432/cards"


def test_rejected_spec_keys_are_the_ones_the_target_does_not_know(spec) -> None:
    # Arrange: the 2026-08-07 failure — a top-level `provider:` the older
    # validator rejects.
    spec["provider"] = "anthropic"
    # Act
    facts = _facts(spec, HEALTHY).facts
    # Assert
    assert facts.rejected_spec_keys == ("provider",)


def test_a_spec_the_target_fully_understands_rejects_nothing(spec) -> None:
    # Arrange
    # Act
    facts = _facts(spec, HEALTHY).facts
    # Assert
    assert facts.rejected_spec_keys == ()


def test_the_credential_expiry_uses_the_targets_own_clock(spec) -> None:
    # Arrange: comparing against OUR clock would be wrong by exactly the skew
    # between the two machines. 1786249796 - 1786246196 = 3600.
    # Act
    expires_in = _facts(spec, HEALTHY).facts.credential_expires_in_s
    # Assert
    assert expires_in == pytest.approx(3600.0)


def test_an_unpinned_port_needs_no_tool_on_the_target(spec) -> None:
    # Arrange: `a2a.port: auto` pins nothing, so no required port can clash and
    # nothing had to be listed to know it.
    # Act
    facts = _facts(spec, HEALTHY).facts
    # Assert
    assert facts.ports_in_use == ()


def test_an_explicit_hub_address_is_probed_from_the_target(spec) -> None:
    # Arrange
    # Act
    facts = _facts(spec, HEALTHY).facts
    # Assert
    assert facts.hub_reachable_from_target is True


def test_a_routable_listen_url_needs_no_override() -> None:
    # Arrange: only a LOOPBACK hub is unprobeable; a real address is usable.
    env = {"SAC_LISTEN_BASE_URL": "http://hub.internal:7878"}
    # Act
    host, port, reason = hub_address(env)
    # Assert
    assert (host, port, reason) == ("hub.internal", 7878, "")


def test_a_healthy_target_reaches_preflight_as_a_go(spec) -> None:
    # Arrange: the positive control. Without it the unknown-propagation tests
    # could pass because the probe set is broken rather than because unknown
    # propagates.
    gathered = _facts(spec, HEALTHY)
    # Act
    report = preflight(
        agent="a",
        to_host="target",
        facts=gathered.facts,
        runtime="tui",
        # The source-work and session checks are gathered locally, not by this
        # batch. A scanned and clean source is supplied so the probe adapter is
        # what is measured.
        source_facts=SourceFacts(
            repos=(RepoWork(path="/proj/x", uncommitted=0, unpushed=0),),
            transcripts=(("aaa1.jsonl", 1000), ("bbb2.jsonl", 3000)),
            session_marker="bbb2",
        ),
        from_host="ywata-note-win",
    )
    # Assert
    assert report.ok is True


def test_sac_installed_off_the_ssh_path_is_read_as_a_path_problem(spec) -> None:
    # Arrange: the scitex-compute-04 readout. `command -v sac` prints nothing
    # while /home/ywatanabe/.env-sac/bin/sac exists and works — two different
    # states behind one "No such file or directory".
    gathered = _facts(spec, SAC_OFF_PATH)
    # Act
    resolved = gathered.facts.sac_resolved_path
    # Assert
    assert resolved == "/home/ywatanabe/.env-sac/bin/sac"


def test_an_empty_sac_path_line_is_an_answer_not_a_missing_fact(spec) -> None:
    # Arrange: `sac_path=` means looked-and-found-nothing. Only a line that
    # never arrived is undetermined.
    gathered = _facts(spec, SAC_OFF_PATH)
    # Act
    on_path = gathered.facts.sac_on_path
    # Assert
    assert on_path is False
