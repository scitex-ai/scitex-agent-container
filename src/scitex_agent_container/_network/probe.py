"""WSL ↔ fleet-hub connectivity probe.

Filed under ``todo#457`` (bug(infra/wsl): ywata-note-win loses WSL
internet connectivity periodically). When the WSL side drops
connectivity to the Orochi hub, the fleet's external view becomes
``Connection timed out during banner exchange`` — the WSL agents
look dead from outside but the Windows host itself is usually fine.

This module does not *fix* the root cause (that requires Windows-side
action on the NIC power-save / Wi-Fi roam / ``.wslconfig`` settings).
What it does add is **local, unambiguous evidence** that can be
correlated with fleet-side outages:

1. Is DNS working from inside WSL? (``gethostbyname`` on hub host).
2. Is IPv4 / IPv6 route to the hub open? (``socket.create_connection``
   to port 443 with a short timeout — no HTTPS handshake).
3. Does a TLS handshake + HTTP GET to the hub complete? (full round
   trip via ``urllib.request`` — catches captive portals / stale
   TLS resumes).
4. Is the default gateway reachable? (LAN-side check — if this fails
   but (1)-(3) pass we learn Wi-Fi roam happened but routing survived).

Each probe records ``(ok, latency_ms, err)`` into a JSONL ring at
``~/.scitex/agent-container/runtime/logs/network/<agent>.jsonl`` so the next
incident leaves a timeline the fleet can diff against SSH-dead logs
from other hosts.

Design rules
------------
- **Non-agentic.** Pure functions + one writer. No LLM calls.
- **Stdlib only.** ``socket``, ``ssl``, ``urllib.request``, ``json``,
  ``pathlib``. No ``requests``, no ``psutil``, no DNS libs — the whole
  point is to work when higher-level networking is breaking.
- **Fail-closed but never raise.** Any probe that fails returns a
  ``ProbeResult`` with ``ok=False`` and an ``err`` string. The writer
  swallows disk exceptions so a probe call never breaks a caller.
- **Short timeouts.** Default 3s per probe so a full run takes <15s
  even when every layer is hanging.
"""

from __future__ import annotations

import json
import os
import re
import socket
import ssl
import time
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

# sac is standalone — no built-in hub default. The operator (or the
# orchestrator wrapping sac) supplies the target via --hub-host /
# --hub-port / --hub-url or SAC_HUB_URL. ``probe_network`` errors out
# clearly when none of these are set.
DEFAULT_HUB_HOST = ""
DEFAULT_HUB_PORT = 443
DEFAULT_HUB_URL = ""
DEFAULT_TIMEOUT_S = 3.0


def _default_log_root() -> Path:
    """Resolve the JSONL log root at call time.

    Honours ``$SAC_PROBE_LOG_ROOT`` as an explicit override (test
    isolation + ops override), then ``$XDG_DATA_HOME``, falling back to
    ``~/.scitex/agent-container/logs/network``.
    """
    override = os.environ.get("SAC_PROBE_LOG_ROOT")
    if override:
        return Path(override)
    base = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".scitex")
    return base / "agent-container" / "logs" / "network"


@dataclass
class ProbeResult:
    """Outcome of one probe layer.

    ``name`` identifies the layer ("dns", "tcp", "https", "gateway").
    ``ok`` is the binary success flag. ``latency_ms`` is wall-clock
    round-trip time, **not** RTT of a specific packet — it includes
    any retries the kernel did internally.
    """

    name: str
    ok: bool
    latency_ms: float
    err: str = ""
    extra: dict[str, Any] = field(default_factory=dict)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def probe_dns(
    host: str = DEFAULT_HUB_HOST,
    *,
    timeout: float = DEFAULT_TIMEOUT_S,
    resolver=None,
) -> ProbeResult:
    """Resolve ``host`` to an IP address.

    ``socket.getaddrinfo`` honours ``/etc/resolv.conf`` ordering and
    returns AF_INET + AF_INET6 entries, so we see IPv4 and IPv6 both.
    We apply ``timeout`` indirectly by using a global ``setdefaulttimeout``
    around the call — there is no per-call resolver timeout in stdlib.

    ``resolver`` is an injection seam (default ``socket.getaddrinfo``) so
    tests can substitute a hand-rolled callable without monkeypatching
    the ``socket`` module.
    """
    if resolver is None:
        resolver = socket.getaddrinfo
    start = time.monotonic()
    old = socket.getdefaulttimeout()
    # stx-allow: fallback (reason: DNS resolution can fail with timeout, NXDOMAIN, or no resolver; ProbeResult(ok=False) records the failure as diagnostic evidence without raising)
    try:
        socket.setdefaulttimeout(timeout)
        infos = resolver(host, None)
        addrs = sorted({sockaddr[0] for (_f, _t, _p, _c, sockaddr) in infos})
        latency_ms = (time.monotonic() - start) * 1000.0
        return ProbeResult(
            name="dns",
            ok=True,
            latency_ms=latency_ms,
            extra={"addrs": addrs},
        )
    except Exception as exc:  # pragma: no cover - exercised via fake  # stx-allow: fallback (reason: catch-all safety net — see inline comment for context)
        latency_ms = (time.monotonic() - start) * 1000.0
        return ProbeResult(
            name="dns",
            ok=False,
            latency_ms=latency_ms,
            err=f"{type(exc).__name__}: {exc}",
        )
    finally:
        socket.setdefaulttimeout(old)


def probe_tcp(
    host: str = DEFAULT_HUB_HOST,
    port: int = DEFAULT_HUB_PORT,
    *,
    timeout: float = DEFAULT_TIMEOUT_S,
    connector=None,
) -> ProbeResult:
    """Open a TCP connection to ``host:port``; close immediately.

    Does NOT speak TLS — we want to isolate "routing/firewall reachable"
    from "TLS/HTTP works". A captive portal returning a 200 OK to any
    request would pass ``probe_https`` but also passes this layer.

    ``connector`` is an injection seam (default ``socket.create_connection``)
    so tests can substitute a hand-rolled callable.
    """
    if connector is None:
        connector = socket.create_connection
    start = time.monotonic()
    # stx-allow: fallback (reason: TCP connect can fail with connection refused, timeout, or no route to host; ProbeResult(ok=False) captures the failure as connectivity evidence)
    try:
        with connector((host, port), timeout=timeout):
            pass
        latency_ms = (time.monotonic() - start) * 1000.0
        return ProbeResult(name="tcp", ok=True, latency_ms=latency_ms)
    except Exception as exc:  # stx-allow: fallback (reason: catch-all safety net — see inline comment for context)
        latency_ms = (time.monotonic() - start) * 1000.0
        return ProbeResult(
            name="tcp",
            ok=False,
            latency_ms=latency_ms,
            err=f"{type(exc).__name__}: {exc}",
        )


def probe_https(
    url: str = DEFAULT_HUB_URL,
    *,
    timeout: float = DEFAULT_TIMEOUT_S,
    expected_status_prefix: str = "",
    opener=None,
) -> ProbeResult:
    """GET ``url`` and record status + latency.

    Any 2xx/3xx/4xx counts as "the tunnel is up" because even a 404
    means packets are flowing end-to-end and TLS completed. Only 5xx
    from the hub or transport errors mark a failure.

    ``expected_status_prefix`` lets callers tighten (e.g. ``"2"``)
    if they actually need a 2xx.

    ``opener`` is an injection seam (default ``urllib.request.urlopen``)
    so tests can substitute a hand-rolled callable without monkeypatching
    ``urllib.request``.
    """
    start = time.monotonic()
    # stx-allow: fallback (reason: HTTPS probe can fail with SSL errors, timeout, or captive portal disruption; ProbeResult(ok=False) records the transport failure without raising)
    try:
        ctx = ssl.create_default_context()
        req = urllib.request.Request(url, method="GET")
        if opener is None:
            cm = urllib.request.urlopen(req, timeout=timeout, context=ctx)
        else:
            cm = opener(req, timeout=timeout, context=ctx)
        with cm as resp:
            status = resp.status
            latency_ms = (time.monotonic() - start) * 1000.0
            ok = status < 500 and (
                not expected_status_prefix
                or str(status).startswith(expected_status_prefix)
            )
            return ProbeResult(
                name="https",
                ok=ok,
                latency_ms=latency_ms,
                err="" if ok else f"status={status}",
                extra={"status": status},
            )
    except (
        urllib.error.HTTPError
    ) as exc:  # stx-allow: fallback (reason: expected failure — see inline comment)
        # Cloudflare / hub returning 4xx is still "transport ok".
        latency_ms = (time.monotonic() - start) * 1000.0
        status = exc.code
        ok = status < 500 and (
            not expected_status_prefix or str(status).startswith(expected_status_prefix)
        )
        return ProbeResult(
            name="https",
            ok=ok,
            latency_ms=latency_ms,
            err="" if ok else f"status={status}",
            extra={"status": status},
        )
    except Exception as exc:  # stx-allow: fallback (reason: catch-all safety net — see inline comment for context)
        latency_ms = (time.monotonic() - start) * 1000.0
        return ProbeResult(
            name="https",
            ok=False,
            latency_ms=latency_ms,
            err=f"{type(exc).__name__}: {exc}",
        )


_ROUTE_RE = re.compile(r"^default\s+via\s+(\S+)\s+dev\s+\S+", re.M)


def _parse_default_gateway(ip_route_output: str) -> str | None:
    """Extract the IPv4 default gateway from ``ip route`` output.

    Returns ``None`` if no default route is present (which is itself
    a strong diagnostic signal).
    """
    m = _ROUTE_RE.search(ip_route_output or "")
    return m.group(1) if m else None


def _read_ip_route() -> str:
    """Read ``/proc/net/route`` and format it like ``ip route`` default.

    We deliberately do not shell out to ``/sbin/ip`` because some WSL
    distros drop the binary or require sudo. Reading the proc file
    always works.
    """
    path = Path("/proc/net/route")
    if not path.exists():
        return ""
    try:
        lines = path.read_text().splitlines()
    except OSError:  # stx-allow: fallback (reason: file system operation failure)
        return ""
    # /proc/net/route columns: Iface Destination Gateway Flags RefCnt Use Metric Mask ...
    # A default route has Destination == "00000000".
    for line in lines[1:]:
        parts = line.split()
        if len(parts) < 3:
            continue
        dest_hex, gw_hex = parts[1], parts[2]
        if dest_hex != "00000000":
            continue
        # gw_hex is little-endian IPv4 bytes.
        try:
            gw_int = int(gw_hex, 16)
        except (
            ValueError
        ):  # stx-allow: fallback (reason: type coercion or format mismatch)
            continue
        octets = [(gw_int >> (8 * i)) & 0xFF for i in range(4)]
        gw = ".".join(str(o) for o in octets)
        return f"default via {gw} dev {parts[0]}\n"
    return ""


def probe_gateway(
    *,
    timeout: float = DEFAULT_TIMEOUT_S,
    ip_route_reader=_read_ip_route,
    connector=None,
) -> ProbeResult:
    """Try a TCP SYN to the default gateway on port 53 (DNS).

    Port 53 is used because most home routers / captive gateways
    listen there. This is a best-effort LAN-reachability check — if
    the gateway silently drops port 53 we still mark ok=False, which
    is a false negative the caller should interpret loosely.
    """
    start = time.monotonic()
    route_output = (
        ip_route_reader() if callable(ip_route_reader) else str(ip_route_reader or "")
    )
    gw = _parse_default_gateway(route_output)
    if not gw:
        latency_ms = (time.monotonic() - start) * 1000.0
        return ProbeResult(
            name="gateway",
            ok=False,
            latency_ms=latency_ms,
            err="no default route",
        )
    if connector is None:
        connector = socket.create_connection
    # stx-allow: fallback (reason: gateway TCP probe to port 53 can fail if the router filters the port or Wi-Fi is lost; ProbeResult(ok=False) provides LAN reachability evidence without raising)
    try:
        with connector((gw, 53), timeout=timeout):
            pass
        latency_ms = (time.monotonic() - start) * 1000.0
        return ProbeResult(
            name="gateway",
            ok=True,
            latency_ms=latency_ms,
            extra={"gateway": gw},
        )
    except Exception as exc:  # stx-allow: fallback (reason: catch-all safety net — see inline comment for context)
        latency_ms = (time.monotonic() - start) * 1000.0
        return ProbeResult(
            name="gateway",
            ok=False,
            latency_ms=latency_ms,
            err=f"{type(exc).__name__}: {exc}",
            extra={"gateway": gw},
        )


def probe_cloudflared() -> ProbeResult:
    """Detect a running cloudflared tunnel daemon on this host.

    Scans the process list for ``cloudflared tunnel run`` (the long-lived
    bastion daemon). Does not probe the tunnel's remote endpoint — just
    confirms the local process is alive. If the process is present the
    tunnel is *likely* healthy (cloudflared auto-reconnects); absence
    means the bastion path is definitely down.

    Reports the PID of the first matching process in ``extra``.
    Uses only stdlib ``subprocess`` so it works without psutil.
    """
    t0 = time.monotonic()
    # stx-allow: fallback (reason: pgrep may be absent on some OS (Windows/
    # minimal containers); any unexpected pgrep output is a graceful miss)
    try:
        import subprocess as _sp

        result = _sp.run(
            ["pgrep", "-f", "cloudflared tunnel run"],
            capture_output=True,
            text=True,
        )
        latency_ms = round((time.monotonic() - t0) * 1000, 2)
        if result.returncode == 0:
            pids = [p.strip() for p in result.stdout.splitlines() if p.strip()]
            pid = int(pids[0]) if pids else 0
            return ProbeResult(
                name="cloudflared",
                ok=True,
                latency_ms=latency_ms,
                extra={"pid": pid},
            )
        return ProbeResult(
            name="cloudflared",
            ok=False,
            latency_ms=latency_ms,
            err="cloudflared tunnel run process not found",
        )
    except FileNotFoundError:
        return ProbeResult(
            name="cloudflared",
            ok=False,
            latency_ms=round((time.monotonic() - t0) * 1000, 2),
            err="pgrep not available on this host",
        )
    except Exception as exc:
        return ProbeResult(
            name="cloudflared",
            ok=False,
            latency_ms=round((time.monotonic() - t0) * 1000, 2),
            err=str(exc),
        )


def run_all_probes(
    *,
    hub_host: str = DEFAULT_HUB_HOST,
    hub_port: int = DEFAULT_HUB_PORT,
    hub_url: str = DEFAULT_HUB_URL,
    timeout: float = DEFAULT_TIMEOUT_S,
) -> list[ProbeResult]:
    """Run probes in the order dns → gateway → tcp → https → cloudflared.

    DNS first because hub_host is a name; if DNS fails TCP/HTTPS will
    also fail with a confusing error. Gateway second so we can tell
    "lost Wi-Fi" from "lost DNS only". TCP before HTTPS so a TLS
    failure is distinguishable from a routing failure. Cloudflared last
    (process check — independent of the network probes).
    """
    return [
        probe_dns(hub_host, timeout=timeout),
        probe_gateway(timeout=timeout),
        probe_tcp(hub_host, hub_port, timeout=timeout),
        probe_https(hub_url, timeout=timeout),
        probe_cloudflared(),
    ]


def summarise(results: Iterable[ProbeResult]) -> dict[str, Any]:
    """Collapse N probe results into a single JSON-friendly dict."""
    results = list(results)
    return {
        "ts": _now_iso(),
        "ok": all(r.ok for r in results),
        "probes": [asdict(r) for r in results],
    }


def _log_path(agent: str, root: Path | None = None) -> Path:
    base = Path(root) if root else _default_log_root()
    base.mkdir(parents=True, exist_ok=True)
    safe = re.sub(r"[^a-zA-Z0-9_.\-]", "-", agent or "anonymous-agent")
    return base / f"{safe}.jsonl"


def append_result(
    agent: str,
    summary: dict[str, Any],
    *,
    root: Path | None = None,
) -> Path | None:
    """Append one summary dict to the per-agent JSONL ring.

    Returns the path written to, or ``None`` if the write failed.
    Does not rotate — the file is append-only and small (one line per
    run, <1 KiB); ops rotates weekly via logrotate if needed.
    """
    # stx-allow: fallback (reason: JSONL append can fail due to disk full or permission error; returning None is safe since probe logging must never raise or disrupt the caller)
    try:
        path = _log_path(agent, root=root)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(summary, separators=(",", ":")) + "\n")
        return path
    except Exception:  # stx-allow: fallback (reason: catch-all safety net — see inline comment for context)
        return None


def run_and_log(
    agent: str,
    *,
    hub_host: str = DEFAULT_HUB_HOST,
    hub_port: int = DEFAULT_HUB_PORT,
    hub_url: str = DEFAULT_HUB_URL,
    timeout: float = DEFAULT_TIMEOUT_S,
    root: Path | None = None,
    probes=None,
) -> dict[str, Any]:
    """One-shot convenience: run all probes, log, return the summary.

    ``probes`` is an injection seam — defaults to ``run_all_probes`` so
    production callers are unchanged. Tests pass a hand-rolled callable
    that returns a list of ``ProbeResult`` objects.
    """
    if probes is None:
        probes = run_all_probes
    results = probes(
        hub_host=hub_host,
        hub_port=hub_port,
        hub_url=hub_url,
        timeout=timeout,
    )
    summary = summarise(results)
    append_result(agent, summary, root=root)
    return summary
