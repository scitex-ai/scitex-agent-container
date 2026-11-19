"""Pure parts of the in-container gateway (packet, DNS and /proc parsing)."""

from __future__ import annotations

import socket
import struct

from scitex_agent_container._sandbox import netgw


def _dns_query(name: str, qid: int = 0x1234) -> bytes:
    q = b"".join(bytes([len(p)]) + p.encode() for p in name.split(".")) + b"\0"
    return (
        struct.pack("!HHHHHH", qid, 0x0100, 1, 0, 0, 0) + q + struct.pack("!HH", 1, 1)
    )


def _syn(dst: str = "140.82.112.3", dport: int = 443) -> bytes:
    tcp = struct.pack("!HHIIBBHHH", 40000, dport, 1000, 0, 5 << 4, 0x02, 65535, 0, 0)
    ip = struct.pack(
        "!BBHHHBBH4s4s", 0x45, 0, 20 + len(tcp), 0, 0, 64, 6, 0,
        socket.inet_aton("10.0.2.15"), socket.inet_aton(dst),
    )  # fmt: skip
    return ip + tcp


def test_dns_qname_reads_the_question():
    # Arrange
    q = _dns_query("github.com")
    # Act
    got = netgw.dns_qname(q)
    # Assert
    assert got == "github.com (qtype 1)"


def test_nxdomain_reply_keeps_id_and_sets_rcode():
    # Arrange
    q = _dns_query("github.com")
    # Act
    reply = netgw.dns_nxdomain(q)
    # Assert
    assert struct.unpack("!HH", reply[:4]) == (0x1234, 0x8183)


def test_syn_is_parsed_with_destination():
    # Arrange
    pkt = _syn()
    # Act
    p = netgw.parse_packet(pkt)
    # Assert
    assert (p["dst"], p["dport"], p["tcp_flags"]) == ("140.82.112.3", 443, 0x02)


def test_rst_answers_from_the_destination():
    # Arrange
    p = netgw.parse_packet(_syn())
    # Act
    rst = netgw.parse_packet(netgw.tcp_rst(p))
    # Assert
    assert (rst["src"], rst["sport"], rst["tcp_flags"]) == ("140.82.112.3", 443, 0x14)


def test_loopback_listeners_from_proc_tables(tmp_path):
    # Arrange
    table = tmp_path / "tcp"
    table.write_text(
        "  sl  local_address rem_address   st\n"
        "   0: 0100007F:1F90 00000000:0000 0A\n"  # 127.0.0.1:8080 LISTEN
        "   1: 0F02000A:01BB 0300528C:01BB 01\n"  # established, not loopback
        "   2: 00000000:2328 00000000:0000 0A\n"  # 0.0.0.0:9000 LISTEN
    )
    # Act
    ports = netgw.loopback_listeners((str(table),))
    # Assert
    assert ports == {8080, 9000}


def test_loopback_nameservers_keeps_only_loopback(tmp_path):
    # Arrange
    rc = tmp_path / "resolv.conf"
    rc.write_text("nameserver 127.0.0.53\nnameserver 8.8.8.8\n")
    # Act
    got = netgw.loopback_nameservers(str(rc))
    # Assert
    assert got == ["127.0.0.53"]
