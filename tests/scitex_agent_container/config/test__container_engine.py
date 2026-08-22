"""THE CONTAINMENT INVARIANT: no launch path runs an agent outside apptainer.

Mirrors ``config/_container_engine.py``, the module that DECLARES the
invariant (``CONTAINER_ENGINE = "apptainer"``, "there is no second one to
choose"). The tests live with the declaration rather than with any one
launch path, because the claim spans several — ``_lifecycle/_runtime_select``,
the three session runtimes, and ``cli_pkg/send_cmds`` — and a claim verified
in one of them only is the shape that let the gap below survive.

Operator ruling 2026-08-14 — abolishing ``spec.container.runtime`` was not
about deleting a field, it was about buying a GUARANTEE: by default nothing
leaks out. A guarantee nothing asserts is a slogan, and until this file
existed nothing asserted it. The repo had argv-shape tests
(``argv[0:2] == ["apptainer", "exec"]``) and a resolver unit test, but
nothing that said "and there is no OTHER way an agent gets launched".

WHAT IS PINNED HERE, in the order a launch actually resolves:

  1. ``_get_runtime`` returns one of exactly two adapters, for every
     ``spec.runtime`` spelling the harness registry accepts. Both dispatch
     through apptainer; a third adapter appearing is a new launch path and
     must fail this test, not be discovered later.
  2. Every accepted spelling resolves to a REAL apptainer container runtime
     — never ``None``. ``claude-agent-sdk`` once fell through to ``None``
     and the recommended value was unusable while the deprecated alias
     worked; that regression is now a red test.
  3. When no container runtime resolves, the runtimes FAIL CLOSED — return
     False, launch nothing. This is the property that makes the whole
     invariant hold: there is no bare-host fallback to fall into.
  4. ``sac agents send`` refuses when no A2A port is recorded, instead of
     shelling out to ``claude --resume`` on the bare host. That fallback
     was a full Claude agent TURN outside the container with the host
     operator's credentials — the one path that genuinely ran an agent
     uncontained during normal operation.

DELIBERATELY NOT ASSERTED (documented, not silently blessed): the
``POST /v1/host_exec`` route runs arbitrary argv on the host by design (it
is the sanctioned escape hatch for image builds and host ops, group-gated,
audited); ``_account/interactive_login`` runs a bare ``claude`` for the
OAuth flow, which is a credential ceremony rather than agent work. Neither
is a way to run an AGENT's session, and pinning them here would freeze
operator tooling under an invariant about agent containment.

No mocks for the code under test — the real registry, the real resolvers,
the real Click command. The only injected object is a stand-in for the
container runtime in (3), which is the seam the runtimes already expose.
"""

from __future__ import annotations

import dataclasses
import logging
from types import SimpleNamespace

import click
import pytest

from scitex_agent_container._lifecycle._runtime_select import _get_runtime
from scitex_agent_container.config._container_engine import CONTAINER_ENGINE
from scitex_agent_container.config._explicit_validation import explicit_spec_defaults
from scitex_agent_container.config._harness_registry import (
    CLAUDE_AGENT_SDK,
    runtime_spellings_for,
    valid_runtime_spellings,
)
from scitex_agent_container.config._types import ContainerSpec
from scitex_agent_container.config._validation import validate_raw
from scitex_agent_container.runtimes._apptainer_runtime import ApptainerContainerRuntime
from scitex_agent_container.runtimes.claude_session import (
    _container_runtime_for as claude_container_runtime_for,
)
from scitex_agent_container.runtimes.claude_session import ClaudeSessionRuntime
from scitex_agent_container.runtimes.openai_session import (
    _container_runtime_for as openai_container_runtime_for,
)
from scitex_agent_container.runtimes.openai_session import OpenAISessionRuntime
from scitex_agent_container.runtimes.tui_session import TuiSessionRuntime

#: The complete set of adapters the lifecycle layer may hand a launch to.
#: Both run their inner process via ``apptainer exec``: ClaudeSessionRuntime
#: (and its OpenAI sibling) through ApptainerContainerRuntime, TuiSession
#: through ``build_run_argv(..., tui=True)`` inside a tmux PTY.
_APPTAINER_ADAPTERS = (ClaudeSessionRuntime, TuiSessionRuntime)


def _config(runtime: str):
    """A stub carrying only what runtime selection reads."""
    return SimpleNamespace(runtime=runtime, kind="Agent", harness="anthropic", name="t")


# ---------------------------------------------------------------------------
# 1. The adapter set is closed.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("spelling", sorted(valid_runtime_spellings()))
def test_every_accepted_runtime_spelling_selects_an_apptainer_adapter(spelling):
    # Arrange — every spelling validate_raw would accept for spec.runtime.
    config = _config(spelling)
    # Act
    adapter = _get_runtime(config)
    # Assert
    assert isinstance(adapter, _APPTAINER_ADAPTERS), (
        f"spec.runtime={spelling!r} selected {type(adapter).__name__}, which "
        f"is not a known {CONTAINER_ENGINE}-dispatching adapter — a new "
        f"launch path has appeared and its containment is unverified"
    )


# ---------------------------------------------------------------------------
# 2. The SDK spellings resolve to a real container runtime, never None.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("spelling", sorted(runtime_spellings_for(CLAUDE_AGENT_SDK)))
def test_claude_path_resolves_an_apptainer_runtime_for_every_spelling(spelling):
    # Arrange
    config = _config(spelling)
    # Act
    container_rt = claude_container_runtime_for(config)
    # Assert
    assert isinstance(container_rt, ApptainerContainerRuntime), (
        f"spec.runtime={spelling!r} is accepted by the validator but the "
        f"Claude path resolves {container_rt!r} — a spelling that validates "
        f"and cannot launch is a trap"
    )


@pytest.mark.parametrize("spelling", sorted(runtime_spellings_for(CLAUDE_AGENT_SDK)))
def test_openai_path_resolves_an_apptainer_runtime_for_every_spelling(spelling):
    # Arrange
    config = _config(spelling)
    # Act
    container_rt = openai_container_runtime_for(config)
    # Assert
    assert isinstance(container_rt, ApptainerContainerRuntime), (
        f"the OpenAI path must use the same container dispatch as the "
        f"Claude path; spec.runtime={spelling!r} gave {container_rt!r}"
    )


def test_an_unresolvable_spelling_yields_no_container_runtime():
    """The precondition for the fail-closed tests below."""
    # Arrange — a spelling no harness entry claims.
    config = _config("bare-metal")
    # Act
    container_rt = claude_container_runtime_for(config)
    # Assert
    assert container_rt is None


# ---------------------------------------------------------------------------
# 3. No container runtime => launch NOTHING. Fail closed, never bare.
# ---------------------------------------------------------------------------


def test_claude_runtime_refuses_to_start_without_a_container_engine():
    # Arrange — the seam the runtime already exposes, forced to "no engine".
    runtime = ClaudeSessionRuntime(container_runtime_for=lambda config: None)
    # Act
    started = runtime.start(_config("bare-metal"))
    # Assert — False, not a bare-host launch.
    assert started is False


# The refusal is LOGGED, not printed (#1049 converted these diagnostics to
# scitex-logging and added a ruff T201/T203 gate). ``caplog`` therefore reads
# the record where ``capsys`` used to read stderr — asserting on stderr here
# would silently pass on an EMPTY string the day the logger stops emitting,
# which is the failure mode this pair of tests exists to catch.
def test_claude_refusal_says_it_will_not_run_outside_apptainer(caplog):
    # Arrange
    runtime = ClaudeSessionRuntime(container_runtime_for=lambda config: None)
    # Act
    with caplog.at_level(logging.ERROR):
        runtime.start(_config("bare-metal"))
    message = caplog.text
    # Assert — the operator must learn WHY, not just that it failed.
    assert CONTAINER_ENGINE in message, message


def test_claude_refusal_does_not_offer_a_ripped_out_engine(caplog):
    # Arrange — this message offered "docker | podman" until 2026-08-14.
    runtime = ClaudeSessionRuntime(container_runtime_for=lambda config: None)
    # Act
    with caplog.at_level(logging.ERROR):
        runtime.start(_config("bare-metal"))
    message = caplog.text.lower()
    # Assert
    assert "podman" not in message, message


def test_claude_refusal_is_actually_emitted(caplog):
    """Guards the two assertions above from passing on an empty log."""
    # Arrange
    runtime = ClaudeSessionRuntime(container_runtime_for=lambda config: None)
    # Act
    with caplog.at_level(logging.ERROR):
        runtime.start(_config("bare-metal"))
    # Assert
    assert caplog.text.strip(), "the refusal produced no log record at all"


def test_openai_runtime_refuses_to_start_without_a_container_engine():
    # Arrange
    runtime = OpenAISessionRuntime(container_runtime_for=lambda config: None)
    # Act
    started = runtime.start(_config("bare-metal"))
    # Assert
    assert started is False


def test_claude_runtime_reports_no_pid_rather_than_inventing_one():
    """A fabricated pid would make an unlaunched agent look alive."""
    # Arrange
    runtime = ClaudeSessionRuntime(container_runtime_for=lambda config: None)
    # Act
    pid = runtime.agent_pid(_config("bare-metal"))
    # Assert
    assert pid is None


# ---------------------------------------------------------------------------
# 4. `sac agents send` has no bare-host `claude --resume` fallback.
# ---------------------------------------------------------------------------


def _refusal() -> click.ClickException:
    """The real terminal branch of ``sac agents send``, invoked directly.

    Called rather than driven through ``CliRunner``: reaching this branch
    through the command would mean rewriting the two dispatch helpers and
    the in-SIF probe, i.e. testing a rearranged module instead of this
    one. The property under test — "when nothing can deliver, refuse" —
    lives entirely in this function.
    """
    from scitex_agent_container.cli_pkg.send_cmds import _refuse_uncontained_send

    with pytest.raises(click.ClickException) as excinfo:
        _refuse_uncontained_send("some-agent")
    return excinfo.value


def test_send_module_no_longer_locates_a_host_claude_binary():
    """The helper whose only job was to find a bare-host ``claude``."""
    # Arrange
    from scitex_agent_container.cli_pkg import send_cmds

    # Act
    still_there = hasattr(send_cmds, "_find_claude_binary")
    # Assert
    assert not still_there, (
        "_find_claude_binary is back — it exists only to run a Claude turn "
        "on the bare host, which is the leak this invariant closes"
    )


def test_send_module_does_not_import_subprocess():
    """No shellout means no reason to hold the tool that performs one."""
    # Arrange
    from scitex_agent_container.cli_pkg import send_cmds

    # Act
    holds_subprocess = hasattr(send_cmds, "subprocess")
    # Assert
    assert not holds_subprocess


def test_send_refuses_rather_than_running_the_turn_somewhere_else():
    """A ClickException prints a usable message; running it bare does not."""
    # Arrange
    from scitex_agent_container.cli_pkg.send_cmds import _refuse_uncontained_send

    # Act
    def deliver_with_nothing_available():
        _refuse_uncontained_send("some-agent")

    # Assert
    with pytest.raises(click.ClickException):
        deliver_with_nothing_available()


def test_send_refusal_explains_that_the_agent_is_contained():
    # Arrange
    exc = _refusal()
    # Act
    message = exc.format_message()
    # Assert
    assert CONTAINER_ENGINE in message, message


def test_send_refusal_names_the_missing_a2a_port():
    # Arrange
    exc = _refusal()
    # Act
    message = exc.format_message().lower()
    # Assert — the actual condition, so the operator can fix it.
    assert "a2a" in message, message


def test_send_refusal_names_the_agent_it_refused_for():
    # Arrange
    exc = _refusal()
    # Act
    message = exc.format_message()
    # Assert
    assert "some-agent" in message, message


def test_send_refusal_tells_the_operator_how_to_recover():
    # Arrange
    exc = _refusal()
    # Act
    message = exc.format_message()
    # Assert — a refusal with no next step is just an obstacle.
    assert "sac agents start" in message, message


# ---------------------------------------------------------------------------
# 5. spec.container.runtime is REMOVED - the engine is not a per-spec choice.
#
# These moved here from test__validation_container_runtime.py, whose premise
# (the enum {none, apptainer}) is gone with the field. That file also carried
# the confusion which let the field survive so long: its
# `test_every_advertised_engine_resolves_to_a_real_runtime` fed a CONTAINER-
# engine value into `SimpleNamespace(runtime=...)` - the TOP-LEVEL launch-mode
# field, a different axis that merely shares the name - and it excluded
# "none", the one value 100% of live specs actually wrote.
# ---------------------------------------------------------------------------


_BASE = {
    "apiVersion": "scitex-agent-container/v3",
    "kind": "Agent",
    "spec": {"runtime": "apptainer"},
}


def _runtime_errors(container_block):
    raw = {**_BASE, "spec": {**_BASE["spec"], "container": container_block}}
    errors = validate_raw(raw, path="<test>")
    return [e for e in errors if "container.runtime" in e]


def test_the_engine_is_apptainer():
    # Arrange — the constant IS the containment guarantee; pin its value.
    expected = "apptainer"
    # Act
    actual = CONTAINER_ENGINE
    # Assert
    assert actual == expected


# Every value a live spec could plausibly carry, INCLUDING the ones the old
# check waved through: 'none' was in the enum, and '' / None slipped past
# `if cr and cr not in ...` entirely.
@pytest.mark.parametrize(
    "value", ["none", "apptainer", "docker", "podman", "kubernetes", "", None, 0, False]
)
def test_declaring_the_key_is_rejected_whatever_the_value(value):
    # Arrange — presence is the offence, not the value.
    block = {"runtime": value}
    # Act
    errors = _runtime_errors(block)
    # Assert
    assert errors, f"container.runtime={value!r} was accepted; it must be rejected"


def test_an_empty_or_null_value_does_not_pass_unexamined():
    """The exact shape the previous ``if cr and ...`` check let through."""
    # Arrange
    written_but_empty = {"runtime": None}
    # Act
    errors = _runtime_errors(written_but_empty)
    # Assert
    assert errors, "a written-but-empty runtime: key still declares the field"


def test_a_container_block_without_the_key_is_clean():
    # Arrange — the post-migration shape every spec must reach.
    block = {"image": "sac.sif", "network": "host", "mount_host_claude": False}
    # Act
    errors = _runtime_errors(block)
    # Assert
    assert errors == []


def test_the_rejection_tells_the_author_to_delete_the_line():
    # Arrange — the hint must clear its own gate.
    message = _runtime_errors({"runtime": "none"})[0]
    # Act
    says_delete = "DELETE" in message and "runtime:" in message
    # Assert
    assert says_delete, message


def test_the_rejection_names_the_engine_that_runs_anyway():
    # Arrange
    message = _runtime_errors({"runtime": "none"})[0]
    # Act
    names_engine = CONTAINER_ENGINE in message
    # Assert
    assert names_engine, message


def test_the_rejection_offers_no_replacement_engine():
    # Arrange — a removal that suggests an alternative rebuilds the menu.
    message = _runtime_errors({"runtime": "docker"})[0]
    # Act
    offered = message.lower()
    # Assert
    assert "podman" not in offered, message


def test_the_rejection_is_not_a_value_complaint():
    # Arrange — "must be X" would mean the field still has a legal value.
    message = _runtime_errors({"runtime": "docker"})[0]
    # Act
    reads_as_enum_error = "must be" in message.lower()
    # Assert
    assert not reads_as_enum_error, message


def test_container_spec_carries_no_engine_field():
    # Arrange — the dataclass is the value tree's SSoT.
    names = {f.name for f in dataclasses.fields(ContainerSpec)}
    # Act
    has_engine = "runtime" in names
    # Assert
    assert not has_engine, f"ContainerSpec still models an engine: {sorted(names)}"


def test_a_scaffolded_spec_cannot_reproduce_the_key():
    """The paste-ready block is where a removed field comes back to life."""
    # Arrange — what the explicit-fields hint / `agents create` would emit.
    defaults = explicit_spec_defaults("Agent")
    # Act
    container = defaults.get("container", {})
    # Assert
    assert "runtime" not in container, (
        "the required/paste-ready block still emits container.runtime — a "
        "spec scaffolded from it would be born failing validation"
    )


# ----------------------------------------------------------------------
# The remedy must be SAFE TO FOLLOW LITERALLY
#
# `spec.runtime` (the harness launch mode) and `spec.container.runtime`
# (removed) share a leaf name and nothing else. Measured 2026-08-20 over
# the 122 live specs on compute-04:
#
#     with spec.runtime            122 / 122
#     with spec.container.runtime    0 / 122
#
# So a reader who hits this error and greps `runtime:` finds exactly one
# line — the LIVE one — in every spec they could be holding. On
# 2026-08-20 the operator did exactly that and was one keystroke from
# deleting `runtime: tui` from a working agent. An ambiguous remedy is
# executed, not puzzled over, so the disambiguation is pinned here.
# ----------------------------------------------------------------------


def test_the_remedy_tells_the_reader_which_runtime_to_keep():
    # Arrange — naming only the doomed line is what made this dangerous.
    message = _runtime_errors({"runtime": "none"})[0]
    # Act
    protects_the_live_field = "KEEP" in message
    # Assert
    assert protects_the_live_field, message


def test_the_remedy_shows_the_nesting_rather_than_the_leaf_name():
    # Arrange — `container:` alone names a block, not a line.
    message = _runtime_errors({"runtime": "none"})[0]
    # Act
    shows_the_parent_path = "spec:" in message
    # Assert
    assert shows_the_parent_path, message


def test_the_remedy_warns_against_acting_on_a_bare_grep():
    # Arrange — the grep is the obvious move and it finds the wrong line.
    message = _runtime_errors({"runtime": "none"})[0]
    # Act
    warns = "grep" in message.lower()
    # Assert
    assert warns, message
