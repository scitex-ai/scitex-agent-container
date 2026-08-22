"""A dispatch chosen by ``host:`` must NAME that line when the hop fails.

MEASURED 2026-08-09: fleet specs still carried ``host: ywata-note-win`` after
the laptop was retired. The lifecycle verbs dispatched there and TWELVE agents
died with ``Permission denied (publickey)`` — a message naming the AGENT and
never the field that picked the destination. Attribution took days, because
nothing connected "this agent will not start" to "a line in its spec points at
a machine that is gone".

What is locked here
-------------------
1. The peer's error propagates UNCHANGED. The pin is still obeyed; this is a
   message, not a veto. A plain ``host:`` string is deliberately never
   reachability-probed (``_host_chain.resolve_host_chain`` — probing a pin
   could only REFUSE, since a pin has no alternatives to choose between), so
   the failure still arrives from the hop itself.
2. The warning NAMES ``host:`` and its value, so the operator can attribute
   the failure to the spec rather than to the agent.
3. A LIST pin gets NO such warning — it was walked candidate-by-candidate and
   its own error already accounts for every entry.
4. A dispatch that SUCCEEDS says nothing. A warning that also fires on healthy
   dispatches is one nobody reads.

PA-306: no mocks. ``dispatcher`` is the production injection seam
``try_dispatch`` already exposes for exactly this, and the failure is a real
exception raised by a real callable.
"""

from __future__ import annotations

import contextlib
import re

import pytest

from scitex_agent_container._state.host_config import PeerSpec
from scitex_agent_container.cli_pkg.lifecycle._dispatch import try_dispatch
from scitex_agent_container.config import AgentConfig
from scitex_agent_container.config._types import HostsSpec

# The retired laptop from the incident, and a machine that is not it.
_RETIRED = "ywata-note-win"
_HERE = "scitex-compute-04"

# What the failing hop actually said in 2026-08-09. Asserted verbatim so a
# change that "helpfully" rewraps or replaces the peer's own error fails here.
_PEER_ERROR = "Permission denied (publickey)"

# ``pytest.raises(match=...)`` is a REGEX search, and the incident's own error
# text contains "(publickey)" — an empty capture group that matches everywhere,
# so an unescaped pattern passes against ANY RuntimeError.
_PEER_ERROR_RE = re.escape(_PEER_ERROR)

# The FIELD together with its VALUE. The peer name alone already appears in
# ordinary dispatch output, so matching only that would pass against a build
# that explains nothing.
_ATTRIBUTION = f"host: {_RETIRED}"


def _cfg(host) -> AgentConfig:
    cfg = AgentConfig(name="figrecipe")
    cfg.hosts_spec = HostsSpec(host=host, hosts=[])
    return cfg


def _peers(*names) -> dict:
    return {n: PeerSpec(name=n, ssh=n) for n in names}


def _raises_like_the_incident(**_kwargs) -> int:
    raise RuntimeError(_PEER_ERROR)


def _succeeds(**_kwargs) -> int:
    return 0


def _dispatch(host, dispatcher):
    return try_dispatch(
        _cfg(host),
        _HERE,
        _peers(_RETIRED),
        dry_run=False,
        force=False,
        dispatcher=dispatcher,
    )


# STDERR, not stdout: `system_msg` writes diagnostics to stderr (and to the
# scitex-logging record). Asserting on `.out` reads an empty string and every
# "warning is absent" test below would pass against a build that warns
# correctly — the negative tests would be vacuous rather than merely wrong.


@pytest.fixture
def string_pin_output(capsys) -> str:
    """Stderr of a STRING-pinned dispatch whose hop failed."""
    with contextlib.suppress(RuntimeError):
        _dispatch(_RETIRED, _raises_like_the_incident)
    return capsys.readouterr().err


@pytest.fixture
def list_pin_output(capsys) -> str:
    """Output of a LIST-pinned dispatch whose hop failed."""
    with contextlib.suppress(RuntimeError):
        _dispatch([_RETIRED], _raises_like_the_incident)
    return capsys.readouterr().err


@pytest.fixture
def successful_dispatch_output(capsys) -> str:
    """Output of a STRING-pinned dispatch that SUCCEEDED."""
    _dispatch(_RETIRED, _succeeds)
    return capsys.readouterr().err


def test_the_peers_error_propagates_unchanged():
    """The pin is obeyed and the failure is not swallowed or reworded."""
    # Arrange
    dispatcher = _raises_like_the_incident
    # Act
    # Assert
    with pytest.raises(RuntimeError, match=_PEER_ERROR_RE):
        _dispatch(_RETIRED, dispatcher)


def test_the_warning_names_the_spec_line_that_chose_the_peer(string_pin_output):
    """The whole point: `host: ywata-note-win` must appear in the output."""
    # Arrange
    text = string_pin_output
    # Act
    attributed = _ATTRIBUTION in text
    # Assert
    assert attributed is True


def test_a_list_pin_is_not_given_the_single_pin_explanation(list_pin_output):
    """A chain already accounts for every candidate in its own error.

    Two explanations for one failure is worse than one: the chain walk reports
    which entries were probed and rejected, and a pin-shaped sentence pasted
    underneath would describe a decision that was not made this way.
    """
    # Arrange
    text = list_pin_output
    # Act
    attributed = _ATTRIBUTION in text
    # Assert
    assert attributed is False


def test_a_dispatch_that_succeeds_explains_nothing(successful_dispatch_output):
    """A warning that fires on healthy dispatches is one nobody reads."""
    # Arrange
    text = successful_dispatch_output
    # Act
    attributed = _ATTRIBUTION in text
    # Assert
    assert attributed is False
