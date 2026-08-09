"""The batched script must degrade per fact, and the parser must never default.

Batching eleven questions into one remote call buys speed and risks honesty: the
naive version returns one blob and one status, so a single failure costs all
eleven answers — or, worse, turns them all into confident falses. These tests
pin the three properties that keep the batch honest:

    * the script never runs under ``set -e``, so a failing section cannot abort
      the sections after it;
    * every answer is its own marker line, so an unparseable field costs only
      itself;
    * the parser reports what it SAW and defaults nothing, so a missing line
      stays missing rather than becoming a negative.

No mocks: the renderer takes a dataclass and returns a string, the parser takes
a string and returns a dataclass. Both are driven with real values, including
captured output from a real host.
"""

from __future__ import annotations

import pytest

from scitex_agent_container._lifecycle._relocate_probe_script import (
    MARKER,
    RemoteQuestions,
    parse_probe_output,
    render_probe_script,
)

# Captured verbatim from scitex-compute-03 on 2026-08-09. Used as-is so the
# parser is tested against what a real host prints, banners and all.
REAL_OUTPUT = """Welcome to Ubuntu 24.04.2 LTS
SAC_RELOC begin
SAC_RELOC epoch=1786246196
SAC_RELOC image=present
SAC_RELOC bind_missing=/home/ywatanabe/.config/gh
SAC_RELOC bind_missing=/mnt/c
SAC_RELOC binds_checked=5
SAC_RELOC cardstore=no
SAC_RELOC cred=/home/ywatanabe/.claude/.credentials.json|1786300000000|yes
SAC_RELOC creds_checked=2
SAC_RELOC ports_checked=0
SAC_RELOC runtimes=apptainer,claude-agent-sdk,tui
SAC_RELOC speckeys=apiVersion,kind,metadata,spec
SAC_RELOC end
"""


@pytest.fixture
def questions():
    """A full question set — every section of the script is exercised."""
    yield RemoteQuestions(
        image="/srv/sac-base.sif",
        bind_sources=("/home/ywatanabe", "/mnt/c"),
        card_store_host="127.0.0.1",
        card_store_port=5432,
        credential_paths=("/creds/a.json",),
        required_ports=(7001,),
        hub_host="hub.example",
        hub_port=7878,
    )


# ---------------------------------------------------------------------------
# the script: a failing section must not take the others with it
# ---------------------------------------------------------------------------


def test_the_script_never_enables_errexit(questions) -> None:
    # Arrange: `set -e` would abort at the first failing section, so one
    # unreadable path would cost every fact printed after it.
    script = render_probe_script(questions)
    # Act
    has_errexit = "set -e" in script
    # Assert
    assert has_errexit is False


def test_every_answer_is_its_own_marker_line(questions) -> None:
    # Arrange: positional output would let one bad field shift every later one.
    script = render_probe_script(questions)
    # Act
    marker_echoes = script.count('echo "$M ')
    # Assert
    assert marker_echoes >= 6


def test_the_preamble_runs_before_anything_is_measured(questions) -> None:
    # Arrange: without it `sac` is off PATH on scitex-compute-03 and the two
    # facts only the target's validator can answer go silently unanswered.
    script = render_probe_script(questions, preamble='export PATH="$HOME/x:$PATH"')
    # Act
    first_line = script.splitlines()[0]
    # Assert
    assert first_line == 'export PATH="$HOME/x:$PATH"'


def test_a_path_with_a_space_is_quoted_rather_than_split() -> None:
    # Arrange: an unquoted path would split into two tests, both failing, and
    # report a missing bind source that does not exist.
    script = render_probe_script(RemoteQuestions(bind_sources=("/mnt/my disk",)))
    # Act
    quoted = "'/mnt/my disk'" in script
    # Assert
    assert quoted is True


def test_an_undeclared_image_is_never_asked_about() -> None:
    # Arrange: "the spec names no image" must not render as "the image is
    # missing on the target".
    script = render_probe_script(RemoteQuestions(bind_sources=("/x",)))
    # Act
    asks = "image=" in script
    # Assert
    assert asks is False


def test_a_loopback_hub_is_never_probed_from_the_target() -> None:
    # Arrange: an empty hub address means the caller refused to guess one;
    # probing it would measure the TARGET's loopback and call it the hub.
    script = render_probe_script(RemoteQuestions(hub_host="", hub_port=0))
    # Act
    asks = "hub=" in script
    # Assert
    assert asks is False


def test_the_default_credential_path_is_expanded_by_the_target() -> None:
    # Arrange: `~` here would expand to the CALLER's home, naming a file on the
    # wrong machine. `$HOME` is left for the remote shell on purpose.
    script = render_probe_script(RemoteQuestions())
    # Act
    remote_home = '"$HOME/.claude/.credentials.json"' in script
    # Assert
    assert remote_home is True


def test_the_refresh_token_value_is_never_printed(questions) -> None:
    # Arrange: an ssh transcript of a preflight must not be a credential leak;
    # the script reports presence, never the secret.
    script = render_probe_script(questions)
    # Act
    echoes_token = 'echo "$M cred_token=$_r"' in script
    # Assert
    assert echoes_token is False


def test_no_bashism_reaches_the_busybox_targets(questions) -> None:
    # Arrange: scitex-nas-01/-02 are QNAP busybox — no `[[ ]]`, no /dev/tcp, no
    # `local`. (The token is `[[ ` with the space: `[[:space:]]` is a POSIX
    # character class busybox grep handles fine, and matching on a bare `[[`
    # would flag it.)
    script = render_probe_script(questions)
    # Act
    bashisms = [tok for tok in ("[[ ", "/dev/tcp", "local ") if tok in script]
    # Assert
    assert bashisms == []


# ---------------------------------------------------------------------------
# the parser: report what was seen, default nothing
# ---------------------------------------------------------------------------


def test_a_real_hosts_banner_is_not_mistaken_for_a_measurement() -> None:
    # Arrange
    readout = parse_probe_output(REAL_OUTPUT)
    # Act
    keys = set(readout.fields)
    # Assert
    assert "Welcome to Ubuntu 24.04.2 LTS" not in keys


def test_a_completed_run_is_reported_as_complete() -> None:
    # Arrange
    readout = parse_probe_output(REAL_OUTPUT)
    # Act
    complete = readout.complete
    # Assert
    assert complete is True


def test_a_truncated_run_keeps_the_facts_it_managed_to_print() -> None:
    # Arrange: the script died after the binds. Everything printed before the
    # cut was true when printed, and must survive.
    truncated = REAL_OUTPUT.split("SAC_RELOC cardstore")[0]
    readout = parse_probe_output(truncated)
    # Act
    image = readout.fields.get("image")
    # Assert
    assert image == "present"


def test_a_truncated_run_is_not_reported_as_complete() -> None:
    # Arrange
    truncated = REAL_OUTPUT.split("SAC_RELOC cardstore")[0]
    readout = parse_probe_output(truncated)
    # Act
    complete = readout.complete
    # Assert
    assert complete is False


def test_a_fact_whose_line_never_arrived_is_absent_not_false() -> None:
    # Arrange: the whole three-valued chain dies here if the parser defaults.
    truncated = REAL_OUTPUT.split("SAC_RELOC runtimes")[0]
    readout = parse_probe_output(truncated)
    # Act
    runtimes = readout.fields.get("runtimes")
    # Assert
    assert runtimes is None


def test_missing_binds_are_collected_by_name() -> None:
    # Arrange
    readout = parse_probe_output(REAL_OUTPUT)
    # Act
    missing = readout.missing_binds
    # Assert
    assert missing == ("/home/ywatanabe/.config/gh", "/mnt/c")


def test_a_credential_line_yields_its_expiry() -> None:
    # Arrange
    readout = parse_probe_output(REAL_OUTPUT)
    # Act
    expiry = readout.credentials[0].expires_at_ms
    # Assert
    assert expiry == 1786300000000.0


def test_an_unparseable_expiry_costs_only_the_expiry() -> None:
    # Arrange: one bad field must not discard the other two on the same line.
    text = f"{MARKER} begin\n{MARKER} cred=/c.json|not-a-number|yes\n{MARKER} end\n"
    readout = parse_probe_output(text)
    # Act
    refresh = readout.credentials[0].refresh_present
    # Assert
    assert refresh is True


def test_an_unparseable_line_does_not_poison_the_other_facts() -> None:
    # Arrange: the batching trap, stated as a test — one bad line, ten good ones.
    text = REAL_OUTPUT.replace(
        "SAC_RELOC binds_checked=5", "SAC_RELOC port_in_use=not-a-port"
    )
    readout = parse_probe_output(text)
    # Act
    runtimes = readout.fields.get("runtimes")
    # Assert
    assert runtimes == "apptainer,claude-agent-sdk,tui"


def test_output_with_no_marker_at_all_started_nothing() -> None:
    # Arrange: a host that printed only its motd told us nothing.
    readout = parse_probe_output("Welcome to Ubuntu\nLast login: never\n")
    # Act
    started = readout.started
    # Assert
    assert started is False
