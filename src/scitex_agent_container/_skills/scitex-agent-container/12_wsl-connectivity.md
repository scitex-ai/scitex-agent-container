---
description: |
  [TOPIC] WSL ↔ Fleet-Hub Connectivity (todo#457)
  [DETAILS] WSL ↔ Fleet-Hub Connectivity (todo#457) — see file body for details..
tags: [scitex-agent-container-wsl-connectivity]
---

# WSL ↔ Fleet-Hub Connectivity (todo#457)

## Problem

`ywata-note-win` (Windows 11 + WSL2) periodically drops WSL-side
internet connectivity. The fleet sees this as "SSH dead" (banner-
exchange timeout, then connection-refused), which is indistinguish-
able from "host asleep" unless the WSL side leaves a local trail.

## Baseline

**Wired LAN + mirrored mode** (as of 2026-04-21, ywatanabe
msg#15405). The host has been moved off Wi-Fi onto a stable wired
Ethernet link; SSID-roam and 2.4 vs 5 GHz hand-off are no longer
in scope. Remaining live failure modes are NIC-level power-save
and DHCP lease churn.

## What the evidence shows (from this host)

One-shot diagnostics on 2026-04-21:

- `/sbin/ip addr` → `eth0` has `192.168.11.28/24`, a LAN IP (NOT
  the WSL NAT `172.x.x.x` range). This proves
  **`networkingMode=mirrored` is already active** — the
  `.wslconfig` at `/mnt/c/Users/wyusu/.wslconfig` contains
  `[wsl2] networkingMode=mirrored`. Under mirrored mode WSL's
  `eth0` reflects whichever Windows NIC is carrying traffic —
  currently the wired Ethernet adapter.
- `/sbin/ip route` → default via `192.168.11.1` (the home router)
  on `eth0`. No separate WSL-bridge hop.
- `/etc/resolv.conf` → `nameserver 8.8.8.8 / 8.8.4.4` (Google DNS,
  static). `/etc/wsl.conf` has `generateResolvConf = false`, so
  DNS is not rewritten on boot.
- `ping scitex-orochi.com` → 25 ms RTT, 0% loss. Right now the
  pipe is healthy.
- `cloudflared` → installed at `~/.local/bin/cloudflared`, **not a
  systemd service and not currently running**. The cloudflared
  tunnel failures described in todo#457 are on the **NAS side**
  dialling *into* a WSL-hosted bastion; they are not owned by this
  host's head agent.

## What this means for the hypotheses in todo#457

| # | Hypothesis | Status |
|---|---|---|
| 1 | WSL2 NAT bridge instability | **Eliminated.** Mirrored mode removes the NAT bridge. |
| 2 | Hyper-V vswitch desync on NIC change | **Partly eliminated.** Mirrored mode uses the host's NIC directly; wired LAN means no Wi-Fi-driven NIC swaps. |
| 3 | cloudflared tunnel uplink expiry | **N/A for this host.** No cloudflared service runs here. Belongs to head-nas. |
| 4 | Windows NIC power-save | **Still live.** Applies equally to the wired Ethernet adapter — if Windows powers it down (selective-suspend / "allow the computer to turn off this device to save power"), WSL loses connectivity immediately. |
| 5 | DHCP lease churn | **Still live, reduced scope.** With the host on a single wired link (no Wi-Fi roam), only lease-renewal on the Ethernet adapter remains. A static reservation removes this entirely. |

## Mitigations that require ywatanabe-side Windows action

These cannot be done from inside WSL. They are the actual root-
cause fixes; everything below the line is WSL-side visibility.

1. **Disable Windows NIC power-save on the wired Ethernet adapter.**
   In PowerShell (admin):
   ```powershell
   Get-NetAdapter | Where-Object {$_.Status -eq "Up"} |
     Set-NetAdapterPowerManagement -AllowComputerToTurnOffDevice Disabled
   ```
   One-shot, survives reboots. Targets all "Up" adapters, which on
   a wired-only host is just the Ethernet NIC.
2. **Static DHCP reservation on the router for this host's wired
   MAC.** With mirrored mode, every DHCP renegotiation on the
   Ethernet adapter re-IPs WSL's `eth0` directly and breaks any
   TCP session crossing the transition. A static reservation
   eliminates lease-expiry churn.
3. **Leave `networkingMode=mirrored` as-is.** The alternative
   (NAT bridge) is the *previous* failure mode the fleet was
   hitting before this setting was applied.

## Mitigations available on the WSL side (what this PR ships)

Since the fleet-facing symptom is "SSH dead, cause unknown", the
WSL-side contribution is **unambiguous evidence** that the outage
is connectivity, not a crashed Claude process. Use the probe
command shipped in this package.

### One-shot manual probe

```bash
scitex-agent-container probe-network --agent head-$(hostname)
```

Output (when healthy):

```json
{
  "ts": "2026-04-21T09:30:00+00:00",
  "ok": true,
  "probes": [
    {"name": "dns", "ok": true, "latency_ms": 4.1, "extra": {"addrs": ["104.19.xx.xx", "172.67.xx.xx"]}},
    {"name": "gateway", "ok": true, "latency_ms": 1.3, "extra": {"gateway": "192.168.11.1"}},
    {"name": "tcp", "ok": true, "latency_ms": 13.2},
    {"name": "https", "ok": true, "latency_ms": 45.0, "extra": {"status": 200}}
  ]
}
```

When the WSL side is broken, the first probe to fail tells you the
layer that actually broke:

| First failure | Diagnosis |
|---|---|
| `dns` | resolver unreachable — `/etc/resolv.conf` nameserver (8.8.8.8) is blocked or the wired link is actually down |
| `gateway` (but `dns` ok) | rare — DNS is cached / UDP-only while LAN routing died |
| `gateway` AND `dns` | Ethernet NIC power-save just kicked in, or DHCP lease renegotiation re-IP'd eth0 |
| `tcp` (but `dns` + `gateway` ok) | hub unreachable: firewall / Cloudflare regional |
| `https` only | TLS handshake or captive-portal interposing |

The probe writes one JSONL line per run to
`~/.scitex/agent-container/logs/network/<agent>.jsonl`. This is
the ring the fleet correlates against its own SSH-dead timestamps.

### Cron-style continuous probe

To leave a timeline the fleet can grep after the next incident,
run the probe every minute from WSL's user systemd / cron:

```cron
* * * * * /home/ywatanabe/.venv/bin/scitex-agent-container probe-network \
            --agent head-ywata-note-win --quiet >/dev/null 2>&1
```

Or via tmux/screen one-liner for a single session:

```bash
while true; do
  scitex-agent-container probe-network --agent head-ywata-note-win --quiet
  sleep 60
done &
```

### Exit-code behaviour for systemd/monit integration

Pass `--exit-nonzero-on-fail` if you want a wrapper script to alert
on sustained failure:

```bash
scitex-agent-container probe-network --agent head-ywata-note-win \
    --quiet --exit-nonzero-on-fail \
    || notify-send "WSL→hub down"
```

## Correlating with fleet-side SSH-dead events

When the fleet reports `ssh ywata-note-win` timing out, the fleet
cannot tell whether:

- the laptop lid is closed (Windows asleep),
- the laptop is awake but WSL lost its wired link (NIC power-save
  or DHCP renegotiation),
- the laptop is awake and WSL has internet but the SSH service
  restarted,
- or Claude Code TUI crashed while the rest of the host is fine.

A minute-granular probe log on WSL disambiguates all four:

- Absent log lines in the window → **Windows host was asleep**
  (WSL was not running). Fix: disable lid-close / power settings.
- Present lines but `ok=false` → **WSL lost external connectivity**.
  Fix: apply one of the Windows-side mitigations above.
- Present lines and `ok=true` → **WSL was fine**, so the SSH-dead
  signal is a false positive. Fix: investigate the fleet's SSH
  probe, not this host.

## See also

- `liveness_probe.py` — TUI-responsiveness probe (different scope:
  "is Claude still echoing text inside tmux?"). Complements this
  module: network-probe ensures WSL can REACH the hub; liveness-
  probe ensures Claude can respond once reached.
- `infra-host-connectivity`, `infra-network-routing` skills in
  `~/.scitex/orochi/shared/skills/` — fleet-level tunnel setup.
