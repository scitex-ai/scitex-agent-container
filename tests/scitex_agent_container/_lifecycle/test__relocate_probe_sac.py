"""Three PATHs, one preamble, and a verdict quoted rather than recomputed.

The five facts about sac ITSELF on the target. Two properties are pinned here.

WHICH PATH THE ANSWER IS ABOUT. ``sac_path`` is the bare non-interactive ssh
PATH; ``sac_usable`` is that PATH plus the peer's ``env_preamble``, which is what
:class:`._relocate_shell.Shell` prepends to every command a relocation sends.
Measured 2026-08-11 on scitex-compute-04 the two differ, and measured 2026-08-12
on ywata-note-win reading only the first produced a FAILING check on a host where
everything works.

AN ABSENT ANSWER IS UNKNOWN, NEVER "NO DRIFT". The start-acceptance line is
printed by the target's OWN drift guard. An older sac there prints nothing, and
the fact must then be undetermined — folding it into "current" would restore
exactly the late refusal this check was added to prevent, while looking like a
pass.

The transport is driven through the ``runner`` seam with real callables returning
canned :class:`RemoteRun` values. Nothing is mocked.
"""

from __future__ import annotations

import pytest

from scitex_agent_container._lifecycle._relocate_probe import gather_target_facts
from scitex_agent_container._lifecycle._relocate_probe_adapter import (
    build_target_probes,
)
from scitex_agent_container._lifecycle._relocate_probe_ssh import RemoteRun

#: A readout in the shape the real script prints, from a host where sac is on
#: the bare PATH and the spec source is current.
PLAIN = """SAC_RELOC begin
SAC_RELOC epoch=1786246196
SAC_RELOC creds_checked=1
SAC_RELOC ports_checked=0
SAC_RELOC sac_path=/usr/local/bin/sac
SAC_RELOC sac_found=/usr/local/bin/sac
SAC_RELOC sac_usable=/usr/local/bin/sac
SAC_RELOC startdrift=current|0|0|/home/ywatanabe/.dotfiles|origin/main
SAC_RELOC startdirty=0
SAC_RELOC end
"""

#: scitex-compute-04, measured 2026-08-11 and again 2026-08-12: sac is off the
#: bare PATH, the preamble finds it, and the dotfiles checkout is five commits
#: behind with 2389 modified files.
COMPUTE_04 = """SAC_RELOC begin
SAC_RELOC epoch=1786246196
SAC_RELOC creds_checked=1
SAC_RELOC ports_checked=0
SAC_RELOC sac_path=
SAC_RELOC sac_found=/home/ywatanabe/.env-sac/bin/sac
SAC_RELOC sac_usable=/home/ywatanabe/.env-sac/bin/sac
SAC_RELOC startdrift=behind|5|0|/home/ywatanabe/.dotfiles|origin/main
SAC_RELOC startdirty=2389
SAC_RELOC end
"""


def _runner(stdout: str):
    """A real ``run_probe_script``-shaped callable returning canned output."""

    def run(host, script, *, timeout_s=60.0, **kwargs):
        return RemoteRun(stdout=stdout, stderr="", exit_code=0)

    return run


def _facts(stdout: str, *, preamble: str = ""):
    probes, _batch = build_target_probes(
        "target", {}, runner=_runner(stdout), preamble=preamble, env={}
    )
    return gather_target_facts(probes).facts


@pytest.fixture
def compute_04():
    """The measured scitex-compute-04 readout, probed WITH its peer preamble."""
    return _facts(COMPUTE_04, preamble='export PATH="$HOME/.env-sac/bin:$PATH"')


# ---------------------------------------------------------------------------
# the three PATH facts
# ---------------------------------------------------------------------------


def test_the_bare_ssh_path_is_still_reported(compute_04) -> None:
    # Arrange: the measured readout, probed with its peer preamble.
    facts = compute_04
    # Act: what `ssh compute-04 sac …` would get.
    fact = facts.sac_on_path
    # Assert
    assert fact is False


def test_the_relocation_path_is_reported_separately(compute_04) -> None:
    # Arrange
    facts = compute_04
    # Act: what every command this feature sends actually gets.
    fact = facts.sac_usable_path
    # Assert
    assert fact == "/home/ywatanabe/.env-sac/bin/sac"


def test_the_direct_lookup_is_reported_too(compute_04) -> None:
    # Arrange
    facts = compute_04
    # Act: separates "not installed" from "installed and unreachable".
    fact = facts.sac_resolved_path
    # Assert
    assert fact == "/home/ywatanabe/.env-sac/bin/sac"


def test_a_probe_that_carried_a_preamble_says_so(compute_04) -> None:
    # Arrange
    facts = compute_04
    # Act: observed by construction — the prober knows what it sent.
    fact = facts.preamble_declared
    # Assert
    assert fact is True


def test_a_probe_with_no_preamble_says_so() -> None:
    # Arrange
    facts = _facts(PLAIN)
    # Act
    fact = facts.preamble_declared
    # Assert
    assert fact is False


def test_an_empty_usable_line_is_an_answer_not_a_gap() -> None:
    # Arrange: looked-and-found-nothing. A raise here would report UNKNOWN for a
    # question the target answered.
    facts = _facts(PLAIN.replace("sac_usable=/usr/local/bin/sac", "sac_usable="))
    # Act
    fact = facts.sac_usable_path
    # Assert
    assert fact == ""


def test_a_missing_usable_line_is_unknown() -> None:
    # Arrange: an older probe script never printed it, so nobody looked.
    facts = _facts(
        "\n".join(
            ln for ln in PLAIN.splitlines() if not ln.startswith("SAC_RELOC sac_usable")
        )
    )
    # Act
    fact = facts.sac_usable_path
    # Assert
    assert fact is None


# ---------------------------------------------------------------------------
# start acceptance — the target's own verdict, quoted
# ---------------------------------------------------------------------------


def test_the_drift_state_is_read_back_verbatim(compute_04) -> None:
    # Arrange
    facts = compute_04
    # Act
    drift = facts.spec_source_drift
    # Assert
    assert drift.state == "behind"


def test_the_commit_count_is_read_back(compute_04) -> None:
    # Arrange
    facts = compute_04
    # Act
    drift = facts.spec_source_drift
    # Assert
    assert drift.behind == 5


def test_the_repo_is_read_back_so_the_hint_can_name_it(compute_04) -> None:
    # Arrange
    facts = compute_04
    # Act
    drift = facts.spec_source_drift
    # Assert
    assert drift.repo == "/home/ywatanabe/.dotfiles"


def test_the_upstream_is_read_back(compute_04) -> None:
    # Arrange
    facts = compute_04
    # Act
    drift = facts.spec_source_drift
    # Assert
    assert drift.upstream == "origin/main"


def test_the_dirty_count_is_read_back(compute_04) -> None:
    # Arrange: evidence for the hint, because `pull --ff-only` aborts here.
    facts = compute_04
    # Act
    drift = facts.spec_source_drift
    # Assert
    assert drift.dirty == 2389


def test_a_missing_drift_line_is_unknown_not_current() -> None:
    # Arrange: THE one to get right. An older sac on the target prints nothing.
    facts = _facts(
        "\n".join(
            ln for ln in PLAIN.splitlines() if not ln.startswith("SAC_RELOC startdrift")
        )
    )
    # Act
    drift = facts.spec_source_drift
    # Assert
    assert drift is None


def test_a_drift_line_with_an_empty_state_is_unknown() -> None:
    # Arrange: the section ran and named no verdict, which is not a verdict.
    facts = _facts(
        PLAIN.replace(
            "startdrift=current|0|0|/home/ywatanabe/.dotfiles|origin/main",
            "startdrift=|0|0||",
        )
    )
    # Act
    drift = facts.spec_source_drift
    # Assert
    assert drift is None


def test_a_missing_dirty_count_still_yields_a_drift_answer() -> None:
    # Arrange: no git on the target. The dirty count is evidence, never verdict,
    # so its absence must not cost the verdict.
    facts = _facts(
        "\n".join(
            ln for ln in PLAIN.splitlines() if not ln.startswith("SAC_RELOC startdirty")
        )
    )
    # Act
    drift = facts.spec_source_drift
    # Assert
    assert drift.state == "current"


def test_a_missing_dirty_count_is_reported_as_not_taken() -> None:
    # Arrange: 0 would claim a clean tree nobody counted.
    facts = _facts(
        "\n".join(
            ln for ln in PLAIN.splitlines() if not ln.startswith("SAC_RELOC startdirty")
        )
    )
    # Act
    drift = facts.spec_source_drift
    # Assert
    assert drift.dirty is None
