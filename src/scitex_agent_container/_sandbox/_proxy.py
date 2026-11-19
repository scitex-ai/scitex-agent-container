"""Host side of a sealed run's egress policy (one thread in the supervisor).

* A CONNECT-only HTTP proxy on a unix socket (bound into the container,
  reached by ``netgw``'s 127.0.0.1:3128 relay) that tunnels ONLY to the
  ``host:port`` pairs of ``spec.sandbox.egress.allow`` and refuses (403) and
  logs everything else. It resolves allowlisted names itself; nothing in the
  container ever resolves a name. Plain-HTTP absolute-form requests are
  refused (the allowlist is TLS-only).
* The receiving end of ``netgw``'s event stream (``STXLOG``): blocked direct
  packets and DNS queries are appended to the blocked log; loopback
  ``listen``/``unlisten`` events open/close a mirror listener on the host's
  127.0.0.1 that relays back into the container through the ingress socket.

Logs, written host-side in the run's ``out/`` dir (never inside a container
mount): ``egress_blocked.jsonl`` (one line per refused attempt: t, host, port,
via=proxy|direct|dns), ``egress_allowed.jsonl`` (one line per tunnel) and
``egress_gateway.jsonl`` (gateway lifecycle + mirror events). A same-uid agent
can reach the proxy socket and could therefore APPEND forged lines, but it
can neither remove logged lines nor reach a host outside the allowlist.

No host network state is touched (no iptables, netns or sudo).
"""

from __future__ import annotations

import asyncio
import json
import os
import threading
import time
from collections import Counter
from pathlib import Path
from urllib.parse import urlparse

__all__ = [
    "ALLOWED_LOG",
    "BLOCKED_LOG",
    "DNS_POLICY",
    "EgressProxy",
    "GATEWAY_LOG",
    "split_target",
]

BLOCKED_LOG = "egress_blocked.jsonl"
ALLOWED_LOG = "egress_allowed.jsonl"
GATEWAY_LOG = "egress_gateway.jsonl"
DNS_POLICY = (
    "no resolution inside the container: loopback nameservers are answered "
    "NXDOMAIN by netgw (each query logged, via=dns); proxied clients send the "
    "host name in CONNECT and the host proxy resolves only allowlisted names"
)
MAX_HEAD = 16_384
CONNECT_TIMEOUT_S = 30
_MAX_SOCK_PATH = 100  # sun_path incl. NUL on Linux is 108; keep a margin


class _SockAddr:
    """A unix socket path usable at ANY length (Linux).

    ``sun_path`` holds 108 bytes, and a run root on deep scratch storage
    overflows it. Long paths are addressed through an ``O_PATH`` fd of their
    directory (``/proc/self/fd/<n>/<name>``), which resolves to the same
    inode for both ``bind`` and ``connect``.
    """

    def __init__(self, path: Path):
        self.path = Path(path)
        self._fd: int | None = None
        self.addr = str(self.path)
        if len(self.addr) > _MAX_SOCK_PATH:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self._fd = os.open(self.path.parent, os.O_PATH | os.O_DIRECTORY)
            self.addr = f"/proc/self/fd/{self._fd}/{self.path.name}"

    def close(self) -> None:
        if self._fd is not None:
            os.close(self._fd)
            self._fd = None


def _now() -> str:
    t = time.time()
    return (
        time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(t))
        + f".{int(t * 1000) % 1000:03d}Z"
    )


def split_target(target: str) -> tuple[str, int | None]:
    """CONNECT authority -> (host, port); IPv6 literals lose their brackets."""
    target = target.strip().lower()
    if target.startswith("["):
        host, _, rest = target[1:].partition("]")
        port = rest.lstrip(":")
    else:
        host, _, port = target.rpartition(":")
        if not host:
            host, port = port, ""
    return host, int(port) if port.isdigit() else None


async def _pipe(r: asyncio.StreamReader, w: asyncio.StreamWriter) -> None:
    try:
        while data := await r.read(65_536):
            w.write(data)
            await w.drain()
        if w.can_write_eof():
            w.write_eof()
    except (ConnectionError, OSError):
        pass


class EgressProxy:
    """Allowlisting CONNECT proxy + gateway event sink + loopback mirror."""

    def __init__(
        self,
        sock_path: str | Path,
        out_dir: str | Path,
        allow,
        *,
        ingress_path: str | Path | None = None,
        mirror_host: str = "127.0.0.1",
    ):
        self.sock_path = Path(sock_path)
        self.ingress_path = Path(ingress_path) if ingress_path else None
        self._sock = _SockAddr(self.sock_path)
        self._ingress = _SockAddr(self.ingress_path) if self.ingress_path else None
        self.out_dir = Path(out_dir)
        self.allow = frozenset((str(h).lower(), int(p)) for h, p in allow)
        self.mirror_host = mirror_host
        self._lock = threading.Lock()
        self._ready = threading.Event()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._error: BaseException | None = None
        self._tasks: set[asyncio.Task] = set()
        self._mirrors: dict[int, asyncio.AbstractServer] = {}
        self.blocked: Counter = Counter()
        self.allowed: Counter = Counter()
        self.gateway_events: Counter = Counter()
        self.mirror_refused: list[int] = []

    # -- logging ---------------------------------------------------------
    def _write(self, name: str, ev: dict) -> None:
        line = json.dumps(ev, sort_keys=True)
        with self._lock, open(self.out_dir / name, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")

    def log_blocked(self, **ev) -> None:
        ev.setdefault("t", _now())
        self.blocked[f"{ev.get('host')}:{ev.get('port')}"] += 1
        self._write(BLOCKED_LOG, ev)

    # -- protocol ----------------------------------------------------------
    async def _handle(self, reader, writer) -> None:
        task = asyncio.current_task()
        self._tasks.add(task)
        try:
            first = await reader.readline()
            if first.strip() == b"STXLOG":
                await self._gateway_events(reader)
            else:
                await self._request(first, reader, writer)
        except (
            ConnectionError,
            asyncio.IncompleteReadError,
            asyncio.LimitOverrunError,
            ValueError,
        ):
            pass
        finally:
            self._tasks.discard(task)
            writer.close()

    async def _gateway_events(self, reader) -> None:
        while line := await reader.readline():
            try:
                ev = json.loads(line)
            except ValueError:
                continue
            if not isinstance(ev, dict):
                continue
            kind = str(ev.pop("event", ""))
            self.gateway_events[kind] += 1
            if kind == "blocked":
                self.log_blocked(**ev)
                continue
            if kind == "listen":
                await self._open_mirror(int(ev.get("port", 0)))
            elif kind == "unlisten":
                self._close_mirror(int(ev.get("port", 0)))
            self._write(GATEWAY_LOG, {"t": ev.pop("t", _now()), "event": kind, **ev})

    async def _request(self, first: bytes, reader, writer) -> None:
        head = first
        while not head.endswith((b"\r\n\r\n", b"\n\n")):
            line = await reader.readline()
            if not line or len(head) > MAX_HEAD:
                return
            head += line
        parts = first.decode("latin-1").split()
        method = parts[0].upper() if parts else ""
        target = parts[1] if len(parts) > 1 else ""
        if method == "CONNECT":
            host, port = split_target(target)
        else:
            u = urlparse(target)
            host, port = (
                u.hostname or "",
                u.port or (80 if u.scheme == "http" else None),
            )
        if method != "CONNECT" or (host, port) not in self.allow:
            self.log_blocked(via="proxy", method=method, host=host, port=port)
            writer.write(
                b"HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\nConnection: close\r\n\r\n"
            )
            await writer.drain()
            return
        try:
            ur, uw = await asyncio.wait_for(
                asyncio.open_connection(host, port), CONNECT_TIMEOUT_S
            )
        except (TimeoutError, asyncio.TimeoutError, OSError) as exc:
            self._write(
                ALLOWED_LOG,
                {"t": _now(), "host": host, "port": port, "error": type(exc).__name__},
            )
            writer.write(b"HTTP/1.1 502 Bad Gateway\r\nContent-Length: 0\r\n\r\n")
            await writer.drain()
            return
        self.allowed[f"{host}:{port}"] += 1
        self._write(ALLOWED_LOG, {"t": _now(), "host": host, "port": port})
        writer.write(b"HTTP/1.1 200 Connection established\r\n\r\n")
        await writer.drain()
        try:
            await asyncio.gather(_pipe(reader, uw), _pipe(ur, writer))
        finally:
            uw.close()

    # -- loopback mirror -----------------------------------------------------
    async def _open_mirror(self, port: int) -> None:
        if self.ingress_path is None or not 0 < port < 65536 or port in self._mirrors:
            return

        async def relay(cr, cw):
            task = asyncio.current_task()
            self._tasks.add(task)
            try:
                ur, uw = await asyncio.open_unix_connection(self._ingress.addr)
                uw.write(f"FWD {port}\n".encode())
                await uw.drain()
                await asyncio.gather(_pipe(cr, uw), _pipe(ur, cw))
                uw.close()
            except OSError:
                pass
            finally:
                self._tasks.discard(task)
                cw.close()

        try:
            self._mirrors[port] = await asyncio.start_server(
                relay, self.mirror_host, port
            )
        except OSError as exc:
            self.mirror_refused.append(port)
            self._write(
                GATEWAY_LOG,
                {
                    "t": _now(),
                    "event": "mirror_refused",
                    "port": port,
                    "error": str(exc),
                },
            )

    def _close_mirror(self, port: int) -> None:
        srv = self._mirrors.pop(port, None)
        if srv is not None:
            srv.close()

    # -- lifecycle -----------------------------------------------------------
    def _serve(self) -> None:
        loop = asyncio.new_event_loop()
        self._loop = loop
        try:
            server = loop.run_until_complete(
                asyncio.start_unix_server(self._handle, path=self._sock.addr)
            )
        except BaseException as exc:  # reported by start()
            self._error = exc
            self._ready.set()
            loop.close()
            return
        self._ready.set()
        try:
            loop.run_forever()
        finally:
            loop.run_until_complete(self._shutdown(server))
            loop.close()

    async def _shutdown(self, server) -> None:
        servers = [server, *self._mirrors.values()]
        self._mirrors.clear()
        for srv in servers:
            srv.close()
        for t in list(self._tasks):
            t.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        for srv in servers:
            await srv.wait_closed()

    def start(self) -> "EgressProxy":
        self.sock_path.parent.mkdir(parents=True, exist_ok=True)
        self.sock_path.unlink(missing_ok=True)
        self.out_dir.mkdir(parents=True, exist_ok=True)
        for name in (BLOCKED_LOG, ALLOWED_LOG, GATEWAY_LOG):
            (self.out_dir / name).touch()
        self._thread = threading.Thread(
            target=self._serve, name="sac-egress-proxy", daemon=True
        )
        self._thread.start()
        self._ready.wait(10)
        if self._error is not None or not self.sock_path.exists():
            raise RuntimeError(f"egress proxy failed to start: {self._error}")
        return self

    def stop(self) -> dict:
        if (
            self._loop is not None
            and self._thread is not None
            and self._thread.is_alive()
        ):
            self._loop.call_soon_threadsafe(self._loop.stop)
            self._thread.join(5)
        self.sock_path.unlink(missing_ok=True)
        for addr in (self._sock, self._ingress):
            if addr is not None:
                addr.close()
        return self.summary()

    def summary(self) -> dict:
        return {
            "mode": "allowlist",
            "allow": sorted(f"{h}:{p}" for h, p in self.allow),
            "dns": DNS_POLICY,
            "blocked_total": sum(self.blocked.values()),
            "blocked": dict(self.blocked.most_common(50)),
            "allowed": dict(self.allowed),
            "gateway_events": dict(self.gateway_events),
            "mirror_refused_ports": list(self.mirror_refused),
            "logs": {
                "blocked": str(self.out_dir / BLOCKED_LOG),
                "allowed": str(self.out_dir / ALLOWED_LOG),
                "gateway": str(self.out_dir / GATEWAY_LOG),
            },
        }
