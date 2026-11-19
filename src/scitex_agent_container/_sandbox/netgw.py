"""In-container egress gateway of a sealed run. Runs INSIDE apptainer only.

    unshare --user --map-current-user --net --keep-caps \\
        python3 -I netgw.py --sock P --ingress I -- CMD...

The container already has ``--net --network none`` (loopback only). This
process sits in a further private user+network namespace in which it holds
CAP_NET_ADMIN (and nothing outside it). Before starting CMD it:

* brings up ``lo`` and a TUN device ``tun0`` carrying the default IPv4 (and,
  when possible, IPv6) route, so EVERY direct packet (raw sockets, clients
  that ignore HTTP(S)_PROXY) lands here: it is logged (dst, port, proto) and a
  TCP SYN is answered with RST, so a direct connect fails at once with
  ECONNREFUSED instead of hanging. Nothing is ever forwarded from the TUN.
* serves 127.0.0.1:3128 / [::1]:3128 and relays each connection over the
  unix socket ``--sock`` to the HOST's allowlisting CONNECT proxy
  (``scitex_agent_container._sandbox._proxy``) — the only way out.
* answers DNS on the loopback nameservers of /etc/resolv.conf with NXDOMAIN
  and logs each query name (proxied clients never resolve: the host proxy
  resolves allowlisted names itself).
* MIRRORS loopback listeners to the host: every TCP port CMD's tree listens
  on (127.0.0.1 / ::1 / any) is reported to the host, which opens the same
  port on the HOST's 127.0.0.1 and relays each connection back in through
  the unix socket ``--ingress`` (served here). Host-side control planes that
  talk to an agent over loopback (e.g. a TUI's RPC socket) keep working; the
  mirror is inbound-only and can never carry egress.

Every event goes to the host proxy over ``--sock`` (first line ``STXLOG``);
the host writes it OUTSIDE every container mount.

CMD runs with the ambient capability set cleared, no_new_privs set, and the
proxy variables exported; this process is non-dumpable so CMD can neither
ptrace it nor read its fds. Exit status = CMD's (97 = network setup failed,
98 = gateway failed).

Stdlib only: it runs on the image's ``python3 -I``.
"""

from __future__ import annotations

import argparse
import asyncio
import ctypes
import fcntl
import json
import os
import signal
import socket
import struct
import sys
import time

PROXY_PORT = 3128
TUN_NAME = b"tun0"
TUN_V4 = "10.0.2.15"
TUN_V6 = "fd00:5e:9::15"
MAX_EVENTS = 20_000
MIRROR_POLL_S = 0.25

# ioctl numbers / flags (linux; x86_64 and aarch64 share these)
TUNSETIFF = 0x400454CA
IFF_TUN, IFF_NO_PI = 0x0001, 0x1000
SIOCSIFFLAGS, SIOCGIFFLAGS = 0x8914, 0x8913
SIOCSIFADDR, SIOCSIFNETMASK = 0x8916, 0x891C
SIOCGIFINDEX, SIOCADDRT = 0x8933, 0x890B
IFF_UP, IFF_RUNNING = 0x1, 0x40
RTF_UP = 0x1
PR_SET_PDEATHSIG, PR_SET_DUMPABLE = 1, 4
PR_SET_NO_NEW_PRIVS = 38
PR_CAP_AMBIENT, PR_CAP_AMBIENT_CLEAR_ALL = 47, 4
PROTO_NAMES = {1: "icmp", 6: "tcp", 17: "udp", 58: "icmpv6"}
#: /proc/net/tcp{,6} local addresses that are reachable over loopback.
_LOOPBACK_HEX = {
    "0100007F",
    "00000000",
    "00000000000000000000000001000000",
    "00000000000000000000000000000000",
}

libc = ctypes.CDLL(None, use_errno=True)


def _prctl(*args: int) -> None:
    if libc.prctl(*[ctypes.c_ulong(a) for a in args]) != 0:
        raise OSError(ctypes.get_errno(), f"prctl{args}")


def _now() -> str:
    t = time.time()
    return (
        time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(t))
        + f".{int(t * 1000) % 1000:03d}Z"
    )


# ------------------------------------------------------------- link setup --
def _ifreq_flags(sock, name: bytes, flags: int | None = None) -> int:
    if flags is None:
        res = fcntl.ioctl(sock, SIOCGIFFLAGS, struct.pack("16sH22x", name, 0))
        return struct.unpack("16sH22x", res)[1]
    fcntl.ioctl(sock, SIOCSIFFLAGS, struct.pack("16sH22x", name, flags))
    return flags


def _sockaddr_in(ip: str) -> bytes:
    return struct.pack("HH4s8x", socket.AF_INET, 0, socket.inet_aton(ip))


def setup_links() -> tuple[int, dict]:
    """lo up; tun0 up with the default v4 (+ v6 when possible) route."""
    s4 = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    _ifreq_flags(s4, b"lo", _ifreq_flags(s4, b"lo") | IFF_UP)
    tun = os.open("/dev/net/tun", os.O_RDWR)
    fcntl.ioctl(tun, TUNSETIFF, struct.pack("16sH22x", TUN_NAME, IFF_TUN | IFF_NO_PI))
    fcntl.ioctl(
        s4, SIOCSIFADDR, struct.pack("16s16s8x", TUN_NAME, _sockaddr_in(TUN_V4))
    )
    fcntl.ioctl(
        s4,
        SIOCSIFNETMASK,
        struct.pack("16s16s8x", TUN_NAME, _sockaddr_in("255.255.255.0")),
    )
    _ifreq_flags(s4, TUN_NAME, _ifreq_flags(s4, TUN_NAME) | IFF_UP | IFF_RUNNING)
    dev = ctypes.create_string_buffer(TUN_NAME)
    zero = _sockaddr_in("0.0.0.0")
    rt = struct.pack(
        "@L16s16s16sHhLPhPLLH0L",
        0,
        zero,
        zero,
        zero,
        RTF_UP,
        0,
        0,
        0,
        0,
        ctypes.addressof(dev),
        0,
        0,
        0,
    )
    fcntl.ioctl(s4, SIOCADDRT, rt)
    info = {"tun": TUN_NAME.decode(), "v4_default_route": True}
    try:  # IPv6 is optional: with no v6 route at all, v6 is blocked anyway.
        s6 = socket.socket(socket.AF_INET6, socket.SOCK_DGRAM)
        idx = struct.unpack(
            "16si20x", fcntl.ioctl(s4, SIOCGIFINDEX, struct.pack("16s24x", TUN_NAME))
        )[1]
        addr = socket.inet_pton(socket.AF_INET6, TUN_V6)
        fcntl.ioctl(s6, SIOCSIFADDR, struct.pack("16sIi", addr, 64, idx))
        any6 = bytes(16)
        fcntl.ioctl(
            s6,
            SIOCADDRT,
            struct.pack(
                "@16s16s16sIHHILIi", any6, any6, any6, 0, 0, 0, 1, 0, RTF_UP, idx
            ),
        )
        info["v6_default_route"] = True
    except OSError as exc:
        info["v6_default_route"] = False
        info["v6_error"] = str(exc)
    return tun, info


# --------------------------------------------------------- packet parsing --
def _csum(data: bytes) -> int:
    if len(data) % 2:
        data += b"\0"
    s = sum(struct.unpack(f"!{len(data) // 2}H", data))
    while s >> 16:
        s = (s & 0xFFFF) + (s >> 16)
    return ~s & 0xFFFF


def dns_qname(payload: bytes) -> str | None:
    """Query name of a DNS message (``name (qtype N)``), or None."""
    try:
        i, labels = 12, []
        while payload[i]:
            n = payload[i]
            if n & 0xC0:
                return None
            labels.append(payload[i + 1 : i + 1 + n].decode("ascii", "replace"))
            i += 1 + n
        qtype = struct.unpack("!H", payload[i + 1 : i + 3])[0]
        return ".".join(labels) + f" (qtype {qtype})"
    except (IndexError, struct.error):
        return None


def dns_nxdomain(query: bytes) -> bytes | None:
    """NXDOMAIN reply echoing the question; None if ``query`` is not a query."""
    if len(query) < 12:
        return None
    qid, flags, qd = struct.unpack("!HHH", query[:6])
    if flags & 0x8000:
        return None
    i = 12
    for _ in range(qd):
        while i < len(query) and query[i]:
            i += 1 + query[i]
        i += 5
    rflags = 0x8000 | (flags & 0x7900) | 0x0080 | 3
    return struct.pack("!HHHHHH", qid, rflags, qd, 0, 0, 0) + query[12:i]


def parse_packet(pkt: bytes) -> dict | None:
    """IP packet -> {'ver','proto','src','dst','sport','dport','tcp_flags','seq','l4'}."""
    if not pkt:
        return None
    ver = pkt[0] >> 4
    if ver == 4 and len(pkt) >= 20:
        ihl = (pkt[0] & 0xF) * 4
        proto = pkt[9]
        src = socket.inet_ntop(socket.AF_INET, pkt[12:16])
        dst = socket.inet_ntop(socket.AF_INET, pkt[16:20])
        l4 = pkt[ihl:]
    elif ver == 6 and len(pkt) >= 40:
        proto = pkt[6]
        src = socket.inet_ntop(socket.AF_INET6, pkt[8:24])
        dst = socket.inet_ntop(socket.AF_INET6, pkt[24:40])
        l4 = pkt[40:]  # extension headers are not walked (reported as their proto)
    else:
        return None
    out = {"ver": ver, "proto": proto, "src": src, "dst": dst, "l4": l4}
    if proto in (6, 17) and len(l4) >= 8:
        out["sport"], out["dport"] = struct.unpack("!HH", l4[:4])
    if proto == 6 and len(l4) >= 20:
        out["seq"] = struct.unpack("!I", l4[4:8])[0]
        out["tcp_flags"] = l4[13]
    return out


def tcp_rst(p: dict) -> bytes:
    """RST|ACK answering a SYN, as if sent by the destination."""
    ack = (p["seq"] + 1) & 0xFFFFFFFF
    tcp = struct.pack(
        "!HHIIBBHHH", p["dport"], p["sport"], 0, ack, 5 << 4, 0x14, 0, 0, 0
    )
    if p["ver"] == 4:
        src, dst = socket.inet_aton(p["dst"]), socket.inet_aton(p["src"])
        pseudo = src + dst + struct.pack("!BBH", 0, 6, len(tcp))
        tcp = tcp[:16] + struct.pack("!H", _csum(pseudo + tcp)) + tcp[18:]
        ip = struct.pack(
            "!BBHHHBBH4s4s", 0x45, 0, 20 + len(tcp), 0, 0, 64, 6, 0, src, dst
        )
        ip = ip[:10] + struct.pack("!H", _csum(ip)) + ip[12:]
        return ip + tcp
    src = socket.inet_pton(socket.AF_INET6, p["dst"])
    dst = socket.inet_pton(socket.AF_INET6, p["src"])
    pseudo = src + dst + struct.pack("!I3xB", len(tcp), 6)
    tcp = tcp[:16] + struct.pack("!H", _csum(pseudo + tcp)) + tcp[18:]
    return struct.pack("!IHBB16s16s", 6 << 28, len(tcp), 6, 64, src, dst) + tcp


def loopback_listeners(tables=("/proc/net/tcp", "/proc/net/tcp6")) -> set[int]:
    """TCP ports in LISTEN state reachable over this netns' loopback."""
    ports: set[int] = set()
    for path in tables:
        try:
            with open(path) as fh:
                lines = fh.read().splitlines()[1:]
        except OSError:
            continue
        for line in lines:
            parts = line.split()
            if len(parts) < 4 or parts[3] != "0A":
                continue
            addr, _, port = parts[1].partition(":")
            if addr.upper() in _LOOPBACK_HEX:
                ports.add(int(port, 16))
    return ports


def loopback_nameservers(path: str = "/etc/resolv.conf") -> list[str]:
    out = []
    try:
        with open(path) as fh:
            lines = fh.read().splitlines()
    except OSError:
        return out
    for line in lines:
        parts = line.split()
        if len(parts) >= 2 and parts[0] == "nameserver":
            if parts[1].startswith("127.") or parts[1] == "::1":
                out.append(parts[1])
    return out


async def _pipe(r: asyncio.StreamReader, w: asyncio.StreamWriter) -> None:
    try:
        while data := await r.read(65_536):
            w.write(data)
            await w.drain()
        if w.can_write_eof():
            w.write_eof()
    except (ConnectionError, OSError):
        pass


# ------------------------------------------------------------------ main --
class Gateway:
    def __init__(self, sock_path: str, ingress_path: str | None):
        self.sock_path = sock_path
        self.ingress_path = ingress_path
        self.log_writer: asyncio.StreamWriter | None = None
        self.n_events = 0
        self.mirrored: set[int] = set()

    def event(self, **ev) -> None:
        self.n_events += 1
        if self.n_events > MAX_EVENTS:
            if self.n_events != MAX_EVENTS + 1:
                return
            ev = {"event": "log_truncated", "after": MAX_EVENTS}
        ev.setdefault("t", _now())
        self.log_writer.write((json.dumps(ev) + "\n").encode())

    def on_tun(self, fd: int) -> None:
        try:
            pkt = os.read(fd, 65_535)
        except BlockingIOError:
            return
        p = parse_packet(pkt)
        if p is None:
            return
        if p["proto"] == 6:
            flags = p.get("tcp_flags", 0)
            if not (flags & 0x02) or (flags & 0x10):  # log/answer SYNs only
                return
            self.event(
                event="blocked",
                via="direct",
                proto="tcp",
                host=p["dst"],
                port=p["dport"],
            )
            os.write(fd, tcp_rst(p))
        elif p["proto"] == 17 and p.get("dport") == 53:
            self.event(
                event="blocked",
                via="dns",
                proto="udp",
                host=p["dst"],
                port=53,
                qname=dns_qname(p["l4"][8:]),
            )
        elif p["proto"] == 58 and p["l4"] and p["l4"][0] in (133, 135, 143):
            return  # IPv6 neighbour/router solicitation, MLD: link noise
        else:
            self.event(
                event="blocked",
                via="direct",
                proto=PROTO_NAMES.get(p["proto"], str(p["proto"])),
                host=p["dst"],
                port=p.get("dport"),
            )

    async def relay_out(self, cr, cw) -> None:
        """127.0.0.1:3128 -> host proxy (the one way out)."""
        try:
            ur, uw = await asyncio.open_unix_connection(self.sock_path)
        except OSError as exc:
            self.event(event="error", via="relay", error=str(exc))
            cw.close()
            return
        await asyncio.gather(_pipe(cr, uw), _pipe(ur, cw))
        for w in (cw, uw):
            w.close()

    async def relay_in(self, cr, cw) -> None:
        """Host mirror -> a loopback listener of CMD's tree (inbound only)."""
        try:
            first = await cr.readline()
            verb, _, port = first.decode("ascii", "replace").strip().partition(" ")
            if verb != "FWD" or not port.isdigit() or int(port) not in self.mirrored:
                cw.close()
                return
            ur, uw = await asyncio.open_connection("127.0.0.1", int(port))
        except (OSError, ValueError):
            cw.close()
            return
        await asyncio.gather(_pipe(cr, uw), _pipe(ur, cw))
        for w in (cw, uw):
            w.close()

    async def watch_listeners(self) -> None:
        while True:
            now = loopback_listeners() - {PROXY_PORT}
            for port in sorted(now - self.mirrored):
                self.event(event="listen", port=port)
            for port in sorted(self.mirrored - now):
                self.event(event="unlisten", port=port)
            self.mirrored = now
            await asyncio.sleep(MIRROR_POLL_S)

    async def _dns_stubs(self, loop) -> None:
        gw = self

        class Dns(asyncio.DatagramProtocol):
            ns = ""

            def connection_made(self, transport):
                self.t = transport

            def datagram_received(self, data, addr):
                gw.event(
                    event="blocked",
                    via="dns",
                    proto="udp",
                    host=self.ns,
                    port=53,
                    qname=dns_qname(data),
                )
                reply = dns_nxdomain(data)
                if reply:
                    self.t.sendto(reply, addr)

        for ns in loopback_nameservers():
            proto = Dns()
            proto.ns = ns
            try:
                await loop.create_datagram_endpoint(
                    lambda p=proto: p,
                    local_addr=(ns, 53),
                    family=socket.AF_INET6 if ":" in ns else socket.AF_INET,
                )
            except OSError as exc:
                self.event(event="error", via="dns_stub", host=ns, error=str(exc))

    async def run(self, cmd: list[str], tun: int, info: dict) -> int:
        loop = asyncio.get_running_loop()
        _, self.log_writer = await asyncio.open_unix_connection(self.sock_path)
        self.log_writer.write(b"STXLOG\n")
        self.event(event="gateway_up", **info)
        os.set_blocking(tun, False)
        loop.add_reader(tun, self.on_tun, tun)
        servers = [await asyncio.start_server(self.relay_out, "127.0.0.1", PROXY_PORT)]
        try:
            servers.append(
                await asyncio.start_server(self.relay_out, "::1", PROXY_PORT)
            )
        except OSError:
            pass
        watcher = None
        if self.ingress_path:
            try:
                os.unlink(self.ingress_path)
            except FileNotFoundError:
                pass
            servers.append(
                await asyncio.start_unix_server(self.relay_in, path=self.ingress_path)
            )
            watcher = asyncio.ensure_future(self.watch_listeners())
        await self._dns_stubs(loop)

        proxy = f"http://127.0.0.1:{PROXY_PORT}"
        env = dict(os.environ)  # the CONTAINER env (already sealed), not the host's
        for k in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
            env[k] = proxy
        env["NO_PROXY"] = env["no_proxy"] = "localhost,127.0.0.1,::1"
        for k in ("ALL_PROXY", "all_proxy"):
            env.pop(k, None)

        def child_setup():
            _prctl(PR_CAP_AMBIENT, PR_CAP_AMBIENT_CLEAR_ALL, 0, 0, 0)
            _prctl(PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0)
            _prctl(PR_SET_PDEATHSIG, signal.SIGKILL, 0, 0, 0)

        proc = await asyncio.create_subprocess_exec(
            *cmd, env=env, preexec_fn=child_setup
        )
        for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
            loop.add_signal_handler(sig, proc.send_signal, sig)
        rc = await proc.wait()
        if watcher is not None:
            watcher.cancel()
        self.event(event="gateway_down", child_exit=rc)
        await self.log_writer.drain()
        self.log_writer.close()
        for s in servers:
            s.close()
        return rc


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--sock", required=True, help="host proxy unix socket")
    ap.add_argument(
        "--ingress", default="", help="unix socket served for the loopback mirror"
    )
    ap.add_argument("cmd", nargs=argparse.REMAINDER)
    a = ap.parse_args(argv)
    cmd = a.cmd[1:] if a.cmd[:1] == ["--"] else a.cmd
    if not cmd:
        ap.error("no command")
    _prctl(PR_SET_DUMPABLE, 0, 0, 0, 0)
    try:
        tun, info = setup_links()
    except OSError as exc:
        sys.stderr.write(f"[sac-netgw] network setup failed: {exc}\n")
        return 97
    try:
        return asyncio.run(Gateway(a.sock, a.ingress or None).run(cmd, tun, info))
    except OSError as exc:
        sys.stderr.write(f"[sac-netgw] gateway failed: {exc}\n")
        return 98


if __name__ == "__main__":
    sys.exit(main())
