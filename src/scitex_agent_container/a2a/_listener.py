"""Validation for caller-owned socket activation; never bind or close its socket."""

import os
import socket


def _validate_listener(fd: int, host: str, port: int) -> int:
    """Require a listening TCP descriptor at the advertised host and port."""
    if os.name != "posix":
        raise ValueError("Listener descriptor handoff requires POSIX")
    if isinstance(fd, bool) or not isinstance(fd, int) or fd < 3:
        raise ValueError(
            "Listener descriptor must be an integer >= 3; standard streams are reserved"
        )
    try:
        duplicate = os.dup(fd)
    except OSError as exc:
        raise ValueError(f"Cannot duplicate listener descriptor: {exc}") from exc
    try:
        listener = socket.socket(fileno=duplicate)
    except OSError as exc:
        os.close(duplicate)
        raise ValueError("Listener descriptor is not a socket") from exc
    with listener:
        if listener.family not in (socket.AF_INET, socket.AF_INET6):
            raise ValueError("Listener must be an IP socket")
        if listener.getsockopt(socket.SOL_SOCKET, socket.SO_TYPE) != socket.SOCK_STREAM:
            raise ValueError("Listener must be a TCP socket")
        if not listener.getsockopt(socket.SOL_SOCKET, socket.SO_ACCEPTCONN):
            raise ValueError("Listener socket must already be listening")
        address = listener.getsockname()
        if address[0] != host or address[1] != port:
            raise ValueError(
                "Listener address does not match the advertised host and port"
            )
    return fd
