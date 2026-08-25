# ADR-0023 — Overlay relay: Tailscale's public DERP now, our own on a VPS later

* **Status**: Accepted (implemented 2026-08-25)
* **Date**: 2026-08-25
* **Operator rulings**:
  「あと、vpnが外部、インターネットからも到達可能かとくにラップトップ01がよく外に
  行くので、確認してください」/「VPNの件ですが、それは変えてください。で9台ですけど、
  それもあなたから触って直してください」/ on being told the only remaining options
  were opening the home router or using a third party:
  「ルーターでPDPとTCPを開けるっていうのは多分怖いことじゃないですか。そこを他の
  仕組みでできないんですかねトンネルとか踏み台を使うとか？」/ and, once told a
  self-hosted path exists:
  「それで別にテールスケール社に頼まなくてもできると言う方法があるならば、別に
  データ主権は守れていて、ただの選択の問題と」/
  「なるほど、そうしたらまずはテールスケールを使ってしまいましょう。これはADRにして
  後にVPSを使えば自前でできると」
* **Consumers**: every agent host on the overlay (7 machines, 8 registered
  nodes), and anything that reaches the card store by its `scitex-primary`
  alias.

---

## 1. The failure this makes impossible

**An agent host that leaves the house silently stops being part of the fleet,
and nothing reports it as a fault.**

Measured 2026-08-25. The overlay was LAN-only in two independent ways:

1. **The control plane advertised a private address.** headscale's
   `server_url` was `http://192.168.11.174:8080` — an RFC1918 address on plain
   HTTP, handed to every client as "where to find me". No client outside the
   LAN can route to it, so a travelling machine cannot re-authenticate, refresh
   its key, or receive peer updates.
2. **There was no working relay at all — not even at home.** Every node
   carried the health warning *"Tailscale could not connect to the 'SciTeX
   fleet' relay server"*. The embedded DERP was served over **plain HTTP**, and
   tailscale requires TLS for DERP. It had therefore never functioned, on any
   node, since the overlay was built.

The fleet appeared healthy because every machine happened to be on one LAN,
where direct paths need no relay. The gap only became visible when someone
asked whether a laptop could work away from home.

This ADR exists so that "the overlay works" is never again a statement about
where the machines happen to be standing.

## 2. What was already true, and was not the problem

Worth recording, because the obvious diagnosis was wrong and cost time:

* The Cloudflare tunnel **already** routed `vpn.scitex.ai` →
  `http://localhost:8080` on the `bastion-scitex-04` tunnel.
* The DNS record **already** existed and was proxied.
* `https://vpn.scitex.ai/key?v=106` **already** returned headscale's real
  public key from the open internet.

Nothing in Cloudflare was missing. A working public path sat unused because of
one config line. The fix to the control plane was to change `server_url` to
`https://vpn.scitex.ai` — after which all 8 nodes stayed connected, because
headscale keeps listening on `0.0.0.0:8080` and LAN-registered nodes were
unaffected.

## 3. Why the relay cannot go through the Cloudflare tunnel

This is the decision's load-bearing fact, and it was **proven with a paired
control**, not inferred:

```
direct to headscale on the LAN : HTTP/1.1 101 Switching Protocols
                                 Upgrade: DERP, Derp-Version: 2, + handshake bytes
through https://vpn.scitex.ai  : HTTP/2 426, server: cloudflare
```

Cloudflare speaks HTTP/2 to the client and forwards **only WebSocket**
upgrades. DERP uses its own `Upgrade: DERP`, which is never passed through.
The control is what makes this conclusive: the origin plainly *can* perform the
upgrade, so the tunnel is where it dies.

Two cheaper fixes were tried first and honestly failed:

* **`stunport: -1`** (custom DERP map via `derp.paths`, with
  `automatically_add_embedded_derp_region: false`) so clients probe over HTTPS
  instead of STUN/UDP. The map reached the client correctly — verified as
  `host=vpn.scitex.ai derpport=443 stunport=-1` — and netcheck still reported
  `Nearest DERP: unknown`. The probe was never the problem; the *connection*
  is.
* **Cloudflare Spectrum**, which does carry arbitrary TCP/UDP, but UDP is
  Enterprise-only.

A caution for whoever revisits this: `curl https://vpn.scitex.ai/derp` returns
**426**, which looks like a healthy endpoint. It is not. Only attempting the
upgrade reveals the truth.

## 4. Decision

**Use Tailscale's public DERP relays now; replace them with our own DERP on a
small VPS later.**

```yaml
derp:
  server:
    enabled: false        # cannot be published; see §3
    automatically_add_embedded_derp_region: false
  urls:
    - https://controlplane.tailscale.com/derpmap/default
  paths: []               # point back at derp.yaml when the VPS exists
  auto_update_enabled: true
```

Rejected alternatives:

* **Open UDP/3478 and TCP/443 on the home router.** Rejected by the operator,
  reasonably — it exposes the home network to reach a relay.
* **Do nothing.** Leaves roaming machines unable to work, which contradicts the
  project's direction (see §5).

## 5. Why this does not surrender data sovereignty

SciTeX's direction is data sovereignty and researcher freedom; "cannot reach
SciTeX, therefore cannot work" is not acceptable. Against that standard:

* DERP relays carry **WireGuard ciphertext only**. A relay operator cannot read
  fleet traffic. What they can observe is metadata: which node keys exchange
  packets, volume, timing, and source addresses.
* No billing relationship exists — headscale users hold no Tailscale account —
  so this cannot silently become a paid dependency. The real risk is different:
  it is not a contractual entitlement, so access could be rate-limited or
  withdrawn at any time.
* **A self-hosted path demonstrably exists** (§6). That is what makes this a
  scheduling decision rather than a concession, and it is the operator's own
  framing: sovereignty is preserved because we *can* do it ourselves, so which
  we run today is a choice.

Relaying is also only a fallback. Tailscale always attempts a direct path
first, uses it when available, and keeps retrying direct in the background
after falling back — so on the LAN the relay is never used.

## 6. Migrating to our own relay

When a VPS with a public address exists:

1. Run a DERP server on it with a real TLS certificate for a hostname we
   control (a sub-domain of `scitex.ai`).
2. Set `derp.urls: []` and point `derp.paths:` back at
   `~/.scitex/headscale/derp.yaml`, which is kept on disk for this purpose,
   with the VPS hostname and a real `stunport`.
3. Restart headscale and verify from a client — **not from the server** — that
   `tailscale netcheck` reports `UDP: true` and names the new region.

Sizing: DERP relays ciphertext and holds no state; the smallest tier at a
Japanese provider (Sakura, ConoHa; roughly ¥500–800/month) is sufficient, and
keeps latency low for a fleet in Japan.

## 7. Verification (what "done" meant here)

Measured from the **clients**, after a deliberate delay, on both an internal
and the external laptop:

```
laptop-01 : UDP: true, Nearest DERP: Tokyo, no health warnings
laptop-02 : UDP: true, Nearest DERP: Tokyo, no health warnings
```

Before the change both reported `UDP: false` and
`Nearest DERP: unknown (no response to latency probes)`.

The delay is part of the method. Immediately after `tailscale down && up` the
health section is empty because the check has not run yet — during this work
that briefly produced a false "fixed" report, which had to be corrected. An
empty warnings list right after a restart is not evidence of health.

## 8. Known gap this does not close

`laptop-02` (the external machine in the operator's diagram) still sees **only
`scitex-compute-04`** in its peer list. It is registered under the `byo` user
while fleet machines are under `fleet`, so policy — not connectivity — is what
prevents it from reaching `scitex-nas-03`. `tailscale ping 100.64.0.1` succeeds
directly in 3 ms, which proves the transport is fine. Making laptop-02 an
external PostgreSQL replica therefore needs an ACL/user decision, and is
tracked separately.
