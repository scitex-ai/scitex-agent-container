"""Socket activation must preserve the caller's real listener and refuse mismatches."""

import os
import socket

import pytest

from scitex_agent_container.a2a._listener import _validate_listener

pytestmark = pytest.mark.skipif(os.name != "posix", reason="POSIX socket activation")


@pytest.fixture
def listener():
    with socket.socket() as owned:
        owned.bind(("127.0.0.1", 0))
        owned.listen()
        yield owned


def test_matching_listener_remains_owned_and_listening(listener):
    # Arrange
    host, port = listener.getsockname()
    # Act
    result = _validate_listener(listener.fileno(), host, port)
    # Assert
    assert (
        result == listener.fileno()
        and listener.getsockopt(socket.SOL_SOCKET, socket.SO_ACCEPTCONN) == 1
    )


@pytest.mark.parametrize("host,offset", [("0.0.0.0", 0), ("127.0.0.1", 1)])
def test_mismatched_endpoint_is_refused(listener, host, offset):
    # Arrange
    port = listener.getsockname()[1]

    # Act
    def validate():
        return _validate_listener(listener.fileno(), host, port + offset)

    # Assert
    with pytest.raises(ValueError, match="does not match"):
        validate()


def test_bound_socket_that_is_not_listening_is_refused():
    # Arrange
    with socket.socket() as owned:
        owned.bind(("127.0.0.1", 0))
        host, port = owned.getsockname()

        # Act
        def validate():
            return _validate_listener(owned.fileno(), host, port)

        # Assert
        with pytest.raises(ValueError, match="already be listening"):
            validate()


def test_datagram_descriptor_is_refused():
    # Arrange
    with socket.socket(type=socket.SOCK_DGRAM) as owned:
        owned.bind(("127.0.0.1", 0))
        host, port = owned.getsockname()

        # Act
        def validate():
            return _validate_listener(owned.fileno(), host, port)

        # Assert
        with pytest.raises(ValueError, match="TCP socket"):
            validate()


def _non_socket_failure(fd):
    try:
        return _validate_listener(fd, "127.0.0.1", 1)
    except ValueError as failure:
        return failure


def test_non_socket_refusal_does_not_close_the_original_file(tmp_path):
    # Arrange
    with (tmp_path / "owned.bin").open("w+b") as owned:
        owned.write(b"caller-owned")
        owned.seek(0)
        # Act
        failure = _non_socket_failure(owned.fileno())
        # Assert
        assert isinstance(failure, ValueError) and owned.read() == b"caller-owned"


@pytest.mark.parametrize("fd", [0, 1, 2])
def test_standard_stream_descriptors_are_refused_without_socket_conversion(fd):
    # Arrange
    host, port = "127.0.0.1", 1

    # Act
    def validate():
        return _validate_listener(fd, host, port)

    # Assert
    with pytest.raises(ValueError, match="standard streams are reserved"):
        validate()
