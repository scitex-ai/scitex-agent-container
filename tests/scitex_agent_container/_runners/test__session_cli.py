"""CLI parsing for the daemon ``--channels`` and ``--a2a-host`` flags.

``_session_cli._parse_argv`` must accept a repeatable ``--channels`` so
``spec.claude.channels`` can be carried into the long-lived runner. When
the set contains ``server:sac`` the runner threads it into
``build_sdk_options`` and the ``sac mcp channel`` adapter is registered.

``--a2a-host`` is the receiving end of the bind address the apptainer argv
builder emits from ``spec.a2a.host``. It continues into
``claude_session.run(a2a_host=)`` -> ``_session_http.serve_inbound(host=)`` ->
``uvicorn.Config(host=)``, so a value mangled here would silently move an
agent's inbound endpoint somewhere the spec never asked for.
"""

from __future__ import annotations

import scitex_agent_container._runners.claude_session as runner
from scitex_agent_container.config._a2a_defaults import DEFAULT_A2A_HOST


def test_parse_argv_channels_absent_defaults_none() -> None:
    # Arrange
    argv = ["--name", "ag"]
    # Act
    ns = runner._parse_argv(argv)
    # Assert
    assert ns.channels is None


def test_parse_argv_single_channel_collected_into_list() -> None:
    # Arrange
    argv = ["--name", "ag", "--channels", "server:sac"]
    # Act
    ns = runner._parse_argv(argv)
    # Assert
    assert ns.channels == ["server:sac"]


def test_parse_argv_repeated_channels_accumulate() -> None:
    # Arrange
    argv = ["--name", "ag", "--channels", "server:sac", "--channels", "client:x"]
    # Act
    ns = runner._parse_argv(argv)
    # Assert
    assert ns.channels == ["server:sac", "client:x"]


def test_parse_argv_a2a_host_is_carried_through_verbatim() -> None:
    # Arrange — the non-loopback bind the argv builder now emits when a spec
    # declares one; anything but a verbatim carry moves the endpoint.
    argv = ["--name", "ag", "--a2a-port", "7901", "--a2a-host", "0.0.0.0"]
    # Act
    ns = runner._parse_argv(argv)
    # Assert
    assert ns.a2a_host == "0.0.0.0"


def test_parse_argv_a2a_host_default_agrees_with_the_fleet_default() -> None:
    # Arrange — this flag carries its OWN "127.0.0.1" literal. Pin it against
    # the documented fleet default so an omitted flag (a hand-run runner) can
    # never bind somewhere the spec readers would not have chosen.
    argv = ["--name", "ag"]
    # Act
    ns = runner._parse_argv(argv)
    # Assert
    assert ns.a2a_host == DEFAULT_A2A_HOST
