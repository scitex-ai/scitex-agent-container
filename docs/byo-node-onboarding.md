# Bringing a machine onto the fleet overlay (BYO nodes)

How a machine that the fleet does not own joins the private overlay, what it
is allowed to reach once it has, and the platform-specific traps that cost
real time the first time.

Written from the onboarding of `scitex-laptop-02` (a MacBook Air) on
2026-08-25, which was deliberately run as a **bring-your-own** machine — the
same path an external user or a self-hosting user would take — rather than as
a fleet-owned host.

Every number and policy body below was read off the live control server on
2026-08-25; where something is **not** working, this document says so rather
than describing the intent.

---

## 1. The shape

- Control server: **headscale v0.23.0**, on `scitex-compute-04`.
  - config: `~ywatanabe/.scitex/headscale/config.yaml`
  - `server_url: http://192.168.11.174:8080`, `listen_addr: 0.0.0.0:8080`
  - supervised by `/etc/systemd/system/headscale.service` (`Restart=always`,
    enabled)
- Overlay range: `100.64.0.0/10` (plus `fd7a:115c:a1e0::/48`).
- `derp.urls: []` — **no public DERP relays**, by a standing 2026-08-14
  ruling recorded in the config file itself. Do not add Tailscale's public
  DERP map back "to make it work"; that decision has already been made and
  reversed once.

### Nodes, as of 2026-08-25

| id | node | user | overlay IP | forced tag(s) |
|----|------|------|-----------|---------------|
| 1 | scitex-compute-04 | fleet | 100.64.0.1 | `tag:scitex-compute`, `tag:scitex-writer` |
| 2 | scitex-compute-01 | fleet | 100.64.0.2 | `tag:scitex-compute` |
| 4 | scitex-compute-03 | fleet | 100.64.0.4 | `tag:scitex-compute` |
| 5 | dxp480tplus-994 | fleet | 100.64.0.5 | `tag:scitex-nas` |
| 6 | ywata-note-win | fleet | 100.64.0.6 | `tag:scitex-laptop` |
| 7 | scitex-compute-02 | fleet | 100.64.0.7 | `tag:scitex-compute` |
| 8 | watanas2 | fleet | 100.64.0.8 | `tag:scitex-nas` |
| 9 | yusuke-s-macbook-air | **byo** | 100.64.0.9 | `tag:scitex-byo` |

Node id 3 is absent: it was `scitex-compute-02`'s stale registration, deleted
after the machine re-registered as id 7.

`scitex-primary` and `cards-primary` resolve to `100.64.0.1` via `/etc/hosts`
on every overlay host. Nothing should hard-code `100.64.0.1`; the alias is
what makes a future failover a one-line change instead of a fleet-wide edit.

---

## 2. The trust model

A BYO machine is **not** a fleet machine. It is registered under a separate
headscale user (`byo`, not `fleet`) and carries `tag:scitex-byo`, and the ACL
gives that tag exactly one destination.

The live policy, read back with `headscale policy get`:

```json
{
  "tagOwners": {
    "tag:scitex-compute": ["fleet"],
    "tag:scitex-nas":     ["fleet"],
    "tag:scitex-laptop":  ["fleet"],
    "tag:scitex-writer":  ["fleet"],
    "tag:scitex-byo":     ["byo"]
  },
  "acls": [
    {
      "action": "accept",
      "src": ["tag:scitex-compute", "tag:scitex-nas", "tag:scitex-laptop"],
      "dst": ["tag:scitex-compute:*", "tag:scitex-nas:*", "tag:scitex-laptop:*"]
    },
    {
      "action": "accept",
      "proto": "tcp",
      "src": ["tag:scitex-byo"],
      "dst": ["tag:scitex-writer:55432"]
    }
  ]
}
```

Read plainly:

- fleet-tagged nodes reach each other on any port;
- a `tag:scitex-byo` node reaches **one TCP port on one tag** — the single
  writer's PostgreSQL at 55432 — and nothing else on the overlay;
- `tag:scitex-byo` is not in any `dst`, so nothing on the overlay can
  initiate a connection **to** the BYO machine either.

Three properties of this that matter more than the rule text:

1. **`tag:scitex-writer` is a role, not a host.** compute-04 holds it today.
   When the writer moves, the tag moves; the ACL does not change and neither
   does any client's DSN, because both go through the name.
2. **The tags are real.** Every row in the table above carries a `forced_tag`
   on the server. This is worth checking rather than assuming: an ACL whose
   `src`/`dst` tags match no node denies everything and *looks* exactly like
   correct isolation. Confirm with
   `headscale nodes list -o json` and read `forced_tags`, not the default
   table view — which shows the *user* column and no tags at all.
3. **Verify isolation after every re-registration, not once.** A node that
   logs out and logs back in can come back under a different user or with no
   tag. The MacBook's isolation was proven twice for this reason, the second
   time after a full logout / re-register cycle.

---

## 3. Joining a machine

### 3.1 Issue a preauth key for the right user

```bash
headscale -c ~/.scitex/headscale/config.yaml users create byo        # once
headscale -c ~/.scitex/headscale/config.yaml preauthkeys create \
    --user byo --expiration 1h
```

Use `--user byo` for a bring-your-own machine and `--user fleet` for a
machine the fleet owns. This is the decision that determines the trust zone;
everything after it is mechanics.

### 3.2 Register from the client

```bash
tailscale login --login-server http://192.168.11.174:8080 --authkey <key>
```

### 3.3 Apply the tag on the server

```bash
headscale -c ~/.scitex/headscale/config.yaml nodes tag -i <id> -t tag:scitex-byo
```

### 3.4 Prove the isolation, from the client

```bash
nc -z -G 5 100.64.0.1 55432   && echo "writer reachable (expected)"
nc -z -G 5 100.64.0.2 22      && echo "REACHED A FLEET HOST — ACL IS WRONG"
```

The second command must fail. If it succeeds, stop and fix the policy before
handing the machine to anyone.

---

## 4. `server_url` is handed to the client, and it overrides `--login-server`

**This is the single most expensive thing to learn the hard way, so it gets
its own section.**

headscale gives every registering client its own configured `server_url`, and
the client then uses *that* — not the URL you passed on the command line.
`--login-server` gets you to the registration endpoint; it does not decide
where the client talks afterwards.

The consequence: `server_url` must be reachable **from every client**, and
changing it changes it for all of them at once.

On 2026-08-25 `server_url` was changed from the LAN address to a public
hostname in order to let a laptop join from outside the LAN. Every node that
subsequently tried to re-register was handed the new URL. Registration broke
fleet-wide; the MacBook and compute-02 both dropped off. Rolling `server_url`
back restored it, and compute-02 recovered on its own.

If off-LAN joining is attempted again, the order must be:

1. prove **one** node registers through the public URL, end to end, from
   outside the LAN;
2. only then consider changing `server_url` for everyone.

Related gotcha from the same afternoon: `GET /key` on the control server
returns HTTP 500 unless you pass the protocol version, `GET /key?v=106`. A
bare `/key` failing is a malformed request, not a broken server — and it was
briefly and wrongly reported as evidence that a tunnel had broken
registration. The origin and the tunnel return byte-identical responses.

**Current state: off-LAN joining does not work.** `vpn.scitex.ai` exists and
answers `/health` and `/derp` with 200, but no node has been proven to
*register* through it, and `server_url` remains the LAN address. Treat the
hostname as groundwork, not as a working path.

---

## 5. macOS specifics

### 5.1 Use the standalone build, not the App Store build

A custom control server needs the standalone (`macsys`) Tailscale build. Its
daemon runs as a **system extension**, `io.tailscale.ipn.macsys`, not as a
plain `tailscaled` process.

### 5.2 `pgrep` does not see it

```bash
pgrep -x tailscaled          # prints nothing — even while the daemon is running
systemextensionsctl list     # this is the honest check
```

`pgrep -x tailscaled` reported "not running" on macOS while the daemon was
alive at pid 75430, and again on QNAP while it was alive at pid 27850. A
process-name mismatch and an absent process are indistinguishable in
`pgrep`'s output. Ask the service's own CLI, which knows its pid.

### 5.3 `timeout` does not exist

`timeout` is GNU coreutils and is absent on macOS and on QNAP. A probe of the
form

```bash
timeout 6 bash -c "cat < /dev/null > /dev/tcp/HOST/PORT" && echo ok || echo unreachable
```

prints `unreachable` **without ever opening a socket**, because the missing
binary fails and the `||` fires. This produced a false "these hosts have no
outbound network path" report that had to be retracted. On macOS use
`nc -z -G <sec> HOST PORT`; where `nc` is also absent, use a bare
`bash -c 'exec 3<>/dev/tcp/H/P'` with no `timeout` prefix and bound the whole
thing with ssh's own timeout instead.

The general rule, which is the part worth carrying to the next platform: **a
probe that cannot run returns the same string as a probe that ran and found
nothing.** Before trusting a negative from an unfamiliar host, prove the
instrument exists there — `command -v timeout` costs one token.

### 5.4 Two GUI approvals, neither scriptable

Both must be clicked by whoever is sitting at the machine:

1. **System extension approval** — System Settings → Privacy & Security,
   after the first launch.
2. **Local network permission** — a prompt on first connection attempt.

Neither can be driven over SSH, and having the machine's admin password does
not avoid them. Plan for a human at the keyboard; for a genuinely remote BYO
user this is the step to put in their instructions with a screenshot.

---

## 6. What a self-hosting user needs

The parts above are what a third party would repeat, in order:

1. reach the control server (today: on the LAN; off-LAN is not yet proven);
2. get a preauth key issued **under the `byo` user**, not `fleet`;
3. register, get tagged `tag:scitex-byo`;
4. reach exactly one port — the writer's 55432 — and hold a per-principal
   credential for it;
5. have that isolation re-verified after any re-registration.

Step 4 is where this connects to the database side: a BYO node gets a
per-principal role, never the shared legacy one. Network reachability and
database authority are two separate gates, and passing the first must not
imply the second.
