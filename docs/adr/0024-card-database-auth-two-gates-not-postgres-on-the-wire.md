# ADR-0024 — Card-database authentication: two gates on the app rail, Postgres never leaves loopback

* **Status**: Proposed (design only — no auth, role, `pg_hba.conf` or bind
  address was changed while writing this)
* **Date**: 2026-08-12
* **Builds on**: sac ADR-0023 (credentials are STATE: references, never
  material), sac ADR-0022 (state → PostgreSQL, configuration → git),
  cards ADR-0016 (transport plurality yes, storage plurality no),
  cards ADR-0017 (identity, tenancy and file SSOT),
  scitex-dev ADR-0006 D3/D7 (one store per host; Postgres gives
  multi-CLIENT, not multi-NODE)
* **Operator framing**: 「データベースの認証に関しては、デザインしてもらっても
  いいですか？」 — a design with options, presented honestly; the operator
  decides.

---

## 1. Recommendation, and the one-line reason

**Adopt Option A: keep Postgres loopback-only and authenticate peers at the
application layer, behind two independent gates — Cloudflare Access *and* the
existing per-host bearer token.**

The reason is one line: **the card database has no cross-host protocol to
authenticate yet, so the cheapest correct move is to not put Postgres on the
wire at all** — and the app rail that would carry sync already has identity,
already has hot-reloadable secrets, and already binds loopback by
construction.

**The first two moves are small, reversible, and need no restart**: close the
`local … trust` back door that currently lets any agent reach the cluster as
*superuser with no password* (§2.2, §7), and name the single host authoritative
for minting each credential — because **two hosts are minting today** (§2.7),
which leaves "rotate" undefined however good the gates are.

Everything below is the evidence and the alternatives.

## 2. What is actually true today, measured

The brief for this work assumed loopback `trust` and no password. **Both
halves are wrong**, and the real posture is more interesting.

**2.1 TCP loopback already requires SCRAM.** `pg_hba.conf` on this host
(`/home/ywatanabe/.scitex/pg/18/main/pg_hba.conf`, mode 0600, written
2026-08-09) reads:

```
local   all   all                    trust
host    all   all   127.0.0.1/32     scram-sha-256
host    all   all   ::1/128          scram-sha-256
```

The DSN carries no password because it does not need to: `PGPASSFILE=/home/
ywatanabe/.pgpass` (0600) supplies one, and `PGPASSFILE` is a declared member
of `SAC_SPEC_ENV_KEYS`. So the fleet is *already* on `scram-sha-256` for every
connection it actually makes. There is no "no-auth posture" to fix on the TCP
path.

**2.2 The real hole is the unix socket, not the network.** `local all all
trust` means any client reaching `/home/ywatanabe/.scitex/pg/run/.s.PGSQL.55432`
authenticates as **any role, with no password**. That socket is reachable from
every agent container: `APPTAINER_BIND` includes `/home/ywatanabe`, the socket
is `srwxrwxrwx`, its directory is owned by `ywatanabe`, and every container
runs as **uid 1000 = ywatanabe**. The SCRAM gate on TCP is a locked front door
beside an unlocked back door to which all 108 agents hold a key.

*Honesty note: this is derived from configuration, mount table and uid. I did
not open a socket connection to prove it, because this work was scoped
design-only.*

**2.3 There is no role separation at all.** `setup_cluster.sh` runs `initdb -U
scitex_cards`, which makes `scitex_cards` the **bootstrap superuser**.
`prepare_target.sh` confirms it by selecting `rolsuper`. Every one of 108 agent
specs connects to the card store as the cluster superuser. Any agent can
`DROP DATABASE`.

**2.4 Binding is loopback and hard-coded, in both services.** Postgres:
`listen_addresses = '127.0.0.1'`, `port = 55432`. The card HTTP service:
`_server.py` binds `("127.0.0.1", port)` and the design doc records that "the
bind address is not a parameter: v1" has no override flag. Confirmed rather
than inherited. **`ssl` is entirely commented out — TLS is OFF and no server
certificate exists.**

**2.5 No secret is in argv.** Secrets travel as *file locators in env*:
`PGPASSFILE` names a path, `SCITEX_CARDS_HUB_TOKEN_FILE` names a path, and
`cloudflared-bastion.service` carries `TUNNEL_TOKEN` via
`EnvironmentFile=%h/.cloudflared/env` rather than on its command line. This is
already the operator's ruling implemented; this ADR does not relax it.

**2.6 The hub rail exists, is unused, and rotates asymmetrically.**
`scitex-cards serve` + `HubBackend` are built (per-host bearer tokens, 0600,
constant-time compare) but not deployed here: port 8765 is closed, there is no
`tokens/` directory and no `hub.token`. Its trust model is stated plainly in
`docs/design/remote-hub-backend.md` §2 — "bearer authenticates the *host*,
header declares the *agent* — spoofable between mutually-trusted fleet
agents". `X-Scitex-Agent` is **required but self-asserted**.

**2.7 There is no single authoritative holder for any credential — measured,
not feared.** Doctrine says compute-04 is stripped to access-only. A read-only
fingerprint check on the host tonight (2026-08-11; fleet measurement, not this
ADR's own probe) found **compute-04 holding refresh material for all three
stored accounts**. So the fleet has **two credential origins by
construction**, and the race is not hypothetical: compute-04 rotated an account
at **22:22:00** and the laptop rotated the same account at **22:25:39**. Two
holders that can each mint mean last-writer-wins on a secret, with no way to
say which value is current. §6 and §7 are written against this fact rather than
against the doctrine.

**2.8 The sync primitive has no wire.** `scitex_dev.store` v0.47.0 exposes
`pull(local: Store, remote: Store)` where *both arguments are in-process Python
objects*. There is no HTTP server, no client, no socket anywhere under
`store/`, and no fleet consumer imports it. **The auth design and the transport
design are therefore the same piece of work** — we are specifying a protocol,
not retrofitting auth onto one. Usefully, `changes_since`/`apply_remote` take
pure data and carry no ambient identity, so an authenticated RPC can wrap them
without changing them.

## 3. Decision

**Authenticate at the application layer with two independent gates. Postgres
stays bound to `127.0.0.1:55432` and is never published to another host.**

Concretely, five rulings:

**D1 — Postgres is a host-local resource.** No `listen_addresses` change, no
Postgres port through any tunnel, no cross-host `postgresql://`. This is
already scitex-dev ADR-0006 D3 ("Postgres gives multi-CLIENT, not multi-NODE")
and cards ADR-0016; this ADR declines to reverse either.

**D2 — Close the `local` bypass; split the superuser off the hot path.**
`local all all trust` becomes `local all all scram-sha-256`, and a
non-superuser `scitex_cards_app` role owning the cards schema becomes the role
agents use. The bootstrap superuser is retained for migrations only. Two roles,
not one, and not 108 — see §5.

**D3 — Cross-host access is the app rail behind two gates.** Only
`scitex-cards serve` is ever exposed, via the existing Cloudflare Tunnel with
Cloudflare Access in front. **Access is gate one and the bearer token is gate
two, and neither is sufficient alone**: the origin must validate the Access JWT
itself rather than trusting that a request arriving on the tunnel was gated,
and the bearer stays mandatory. Never Tailscale.

**D4 — Exactly one host is authoritative for each credential, and the design
says so out loud because today nothing does.** This design **assumes a single
authoritative holder per secret** — the host that mints it — and §2.7 shows
that assumption **does not currently hold anywhere in this system**. It is
therefore recorded here as a *precondition*, not a background truth: for the
card rail the authoritative holder is the hub that runs `mint_token`, and any
second minting origin is a defect to be removed, not a redundancy to be
tolerated. Two origins for one secret makes "rotate" undefined — there is no
answer to *which value is current* — and no amount of gate design downstream
repairs that. Where the fleet cannot yet name the holder, the honest status is
`unknown`, not "probably the laptop".

**D5 — The database records credential *descriptors*, never material.** Which
account, which node, which tier, when minted, when it expires, and a
scheme-prefixed locator (`file:/home/ywatanabe/.pgpass`,
`env:SCITEX_CARDS_HUB_TOKEN_FILE`). This is sac ADR-0023 §2 applied verbatim;
this ADR adds no exception to it, and specifically does not store a digest —
ADR-0023 §3.5 forecloses that escape hatch.

## 4. Why not Postgres-on-the-wire with mTLS — argued, not inherited

**4.1 It needs a CA we do not have.** `ssl` is commented out in
`postgresql.conf` and there is no server certificate. mTLS is not a
`pg_hba.conf` edit; it is `ssl=on`, a server cert, a CA to issue and revoke
with, per-peer client certs for six hosts, and `hostssl … cert
clientcert=verify-full`. That is a new operational service, permanently.

**4.2 Its rotation story is a flag day, which the brief rules out.**
Certificates expire on a wall-clock date. Unless two overlapping CAs are run,
expiry takes out every peer *simultaneously*, and recovery needs the very
channel that just closed. Compare §6: token overlap is already supported today.

**4.3 Cloudflare Access cannot gate it the same way.** Access's JWT gate is an
HTTP-layer control. Carrying the Postgres wire protocol means a raw TCP tunnel
and a `cloudflared access tcp` client, which moves the second gate from "the
origin validated a signed assertion" to "the client had a tunnel". The
operator's constraint — the tunnel must not be the only gate — is materially
harder to honour on the TCP path.

**4.4 It buys least privilege we cannot enforce anyway.** See §5.

mTLS is not wrong in general. It is the right answer to a question we do not
have: *external, non-fleet clients connecting to their own host's database* —
which is exactly what scitex-dev ADR-0006 D7 already anticipates. If that need
arrives, revisit as Option B below.

## 5. Granularity: per-agent roles buy attribution, not isolation

The instinct is that finer is better. Under the current threat model it is not,
and the reason is measured rather than theoretical: **all 108 agents run as the
same OS uid (1000/ywatanabe) with the same bind-mounted `$HOME`.** Any agent
can read any other agent's token file and `~/.pgpass`. A per-agent database
role is therefore a label its holder can trade for any other label by reading a
neighbouring file. Least privilege becomes real only when it cannot be bypassed
one layer down.

So the ADR splits the axis:

| Axis | Verdict | Why |
|---|---|---|
| Per-agent **DB roles** (108×) | **No, not yet** | Provisioning and rotation scale with the fleet; buys no isolation while uid and `$HOME` are shared. |
| **Capability** split (2 roles) | **Yes, now** | Removes "every agent can drop the cluster". Cheap, static, and per-agent roles would not have fixed it. |
| Per-agent **bearer tokens** | **Yes, next** | On the HTTP rail the identity header is already required; per-agent tokens turn `X-Scitex-Agent` from a claim into a fact. This is the v2 already named in `remote-hub-backend.md` §2. |

## 6. Rotation without stopping the fleet

The failure the brief names — a rotation that silently 401'd every *running*
agent, which a fresh token did not revive — is reproducible from the code, and
it is an **asymmetry, not a law**:

* **Server: already hot.** `_server._load_tokens` globs `*.token` and re-reads
  the directory *on every request*; `mint_token`'s docstring states rotation
  needs no restart. Because *any* file in the dir authenticates, **an overlap
  window is already supported**: drop the new token in, let clients pick it up,
  delete the old one. Two valid secrets at once, by construction.
* **Client: cold.** `_backend_http.HubBackend.__init__` sets `self._token =
  None  # resolved lazily at first call`, and `_call` resolves it once and
  keeps it for the process lifetime. On 401 it raises "re-provision this host".
  A live process therefore holds what it started with — exactly the reported
  symptom.

**The fix is one behaviour change on the client: on 401, re-read the token file
once and retry; only then fail.** That makes rotation hot at both ends with no
fleet restart. It is the single highest-value change in this ADR.

For Postgres, libpq reads `PGPASSFILE` **per connection**, so a password change
is picked up by every new connection while established sessions continue —
which is the overlap property we want, provided the old password is retired
only after connections have turned over.

**But hot rotation presupposes one minting origin, and §2.7 shows we do not
have one.** With two hosts able to mint the same credential — compute-04 at
22:22:00, the laptop at 22:25:39 on the same account — an overlap window stops
being a window and becomes a permanent ambiguity: each origin's "new" value
invalidates the other's, so a client that refreshes correctly can still be
holding a value the *other* origin has already superseded. **D4 is therefore
load-bearing for this section rather than adjacent to it.** Ordering matters:
naming the authoritative holder is a prerequisite for step 4 of §7, not a
cleanup after it. Until it is named, the correct expectation is that rotation
is *not* safely hot, whatever the code permits.

## 7. Migration path, ordered so no step needs a flag day

1. **Add the socket-form `.pgpass` line first.** libpq matches unix-socket
   connections against the hostname `localhost`, **not** `127.0.0.1`. Today's
   file has `127.0.0.1:55432:*:scitex_cards:…` only. Appending a `localhost`
   entry is additive and changes nothing on its own — and skipping it is
   precisely how step 2 locks the fleet out.
2. **Flip `local` to `scram-sha-256`, then `pg_ctl reload`.** A reload re-reads
   `pg_hba.conf` **without dropping existing connections** — authentication is
   evaluated at connect time, so live agents keep their sessions and only new
   connections see the new rule. Rollback is the reverse edit plus another
   reload. No restart, no simultaneity.
3. **Create `scitex_cards_app`, grant it the schema, add its `.pgpass` line,
   then change the user in `fleet_default_env`.** Both roles authenticate
   during the changeover, so agents migrate as they restart naturally. No
   moment exists at which every agent must be restarted.
4. **Name the authoritative minting origin for each credential (D4), and
   remove the second one.** This is ordered *before* the rotation fix on
   purpose: a client that correctly re-reads its token still cannot converge
   while two hosts mint competing values (§2.7, §6). This step is
   knowledge-and-removal work, not a code change, and it needs no restart.
5. **Ship the client 401-retry fix** (§6). Rolls out with normal agent churn.
6. **Only then** consider any cross-host rail — and not before §8 is answered.

Steps 1–5 are independently reversible and none requires the fleet to stop.
Note what this ordering refuses: shipping the rotation fix first would *look*
like progress and would still leave rotation undefined, because the ambiguity
is in who mints, not in who re-reads.

## 8. What authentication does NOT fix, and why it gates the sync work

`scitex_dev.store`'s applier trusts almost everything it is handed.
`apply_remote` skips `check_revision`, skips `check_owner`, and does not
consult `writer_policy` at all. The node id is a caller-supplied bare string,
never derived and never verified, and it becomes both the oplog `origin` and
the HLC tiebreak. Relay is a deliberate, tested feature, so a peer legitimately
serves ops stamped with a third party's origin — which means **origin cannot
simply be bound to the authenticated channel identity without breaking relay.**
There is no signature, MAC or hash chain on oplog entries to bind them instead.

The sharpest case: `fence` is a field *on the incoming entry*, adopted
unconditionally when greater, while locally-written ops always carry `fence=0`.
One accepted batch claiming a large fence under another host's origin
**permanently evicts that host**, irreversibly through the public API.

Authenticating the peer answers *who is talking*. It does not answer *may this
origin rewrite that origin's history*. **Enabling a cross-host oplog rail
before per-origin provenance exists would ship an authenticated channel into an
unauthorizing applier** — so D3's rail is necessary but not sufficient, and
sync stays gated on that protocol work rather than on this ADR.

## 9. When the status quo becomes wrong

Loopback-only with a `trust` socket is *not* obviously wrong on a
single-tenant host, and asserting otherwise without saying why would not
survive contact. It is defensible exactly while all four hold:

1. one human principal on the host;
2. every agent is equally trusted;
3. nothing untrusted can execute as uid 1000;
4. no port leaves the machine.

It becomes wrong the moment any one fails — and (2) is already under pressure,
because agents run model-authored code against untrusted web and repository
content, and (3) is the assumption a single container escape or a malicious
dependency invalidates. §2.2's back door means the blast radius of failing (3)
is *cluster superuser*, not "one agent's cards". D2 shrinks that blast radius
without waiting for the others to fail.

## 10. Alternatives, stated with what breaks first

| | **A — Two gates, app rail** (recommended) | **B — Postgres on the wire, mTLS** | **C — Per-agent roles + tokens** |
|---|---|---|---|
| Protects against | Remote reach without both Access *and* bearer; superuser blast radius (via D2) | Password theft; cryptographic peer identity mapped to a DB role | Identity spoofing *within* the fleet; real audit trail |
| Does **not** protect against | Same-uid secret theft; a compromised peer poisoning the oplog | Same-uid theft; oplog poisoning; needs a CA regardless | Anything, while uid and `$HOME` are shared |
| Cost to run | Low — reuses cloudflared, `serve`, existing tokens | High — a permanent CA, six hosts of cert lifecycle | Medium-high — provisioning scales with the fleet |
| **Rotation — does it need a flag day?** | **No, once two things are true**: the §6 client re-read fix ships, and D4 names one minting origin. Server-side overlap already works today. | **Yes, effectively.** Certificate expiry is a wall-clock cliff that hits every peer at the same instant; avoiding a flag day means running two overlapping CAs, which is a second permanent service. | **No, but N× the work**: each agent rotates independently, so the fleet never stops — at the cost of a rotation surface that grows with the roster. |
| Breaks first if wrong | Access misconfigured → endpoint reachable with bearer alone (mitigated: origin validates the JWT, bearer stays mandatory) | One expired/skewed cert takes out every peer at once | Rotation debt; roles drift from the agent roster |

## 11. Deferred, and honestly named

* **Per-origin provenance for the oplog** (§8). Signatures on `OpEntry`, or an
  explicit trusted-relay model. Closes when a cross-host rail is actually
  wanted; until then it is the reason there isn't one.
* **The `fence` eviction primitive** (§8). Needs its own decision — the
  adopt-from-entry behaviour is *pinned by 18 passing tests*, so changing it is
  an ADR, not a bugfix.
* **Per-agent bearer tokens** (§5) — already named as v2 in
  `remote-hub-backend.md`; this ADR endorses it and does not schedule it.
* **OS-level agent separation.** The precondition that would make per-agent DB
  roles worth their cost. Not proposed here.

## 12. Unknown — recorded rather than rounded

* **What the Cloudflare tunnel publishes, and whether Access policies are
  attached to it.** `~/.cloudflared/` holds only an `env` file with
  `TUNNEL_TOKEN`; there is no `config.yml`, so ingress and Access policy are
  dashboard state and are **not readable from this host**. D3 assumes Access
  can be placed in front; that assumption is unverified here.
* **What is listening on `127.0.0.1:5442`.** It answers, reports **3491 cards**
  and the **same `store_uuid` (`1d55dd6e…`) as `55432`, which reports 3787** —
  and a card present on 55432
  (`sac-cct-store-diverges-across-restart-two-dbs-20260808`) is absent from
  5442. The running MCP server uses 5442; the shell env says 55432. Two
  databases assert one identity. This is an existing carded incident, not a
  finding of this ADR, but **it gates peer authentication**: a store identity
  that two databases can both assert is not yet an identity to authenticate
  against.
* **Which host is authoritative for each stored credential.** §2.7 establishes
  that *two* origins hold refresh material and that both have minted the same
  account within four minutes. It does **not** establish which one should win.
  That is a decision the operator owns, and D4 is written as a precondition
  precisely because this ADR cannot answer it by measurement.
* **Whether the other five peer hosts carry the same `pg_hba.conf`.** Only this
  host's cluster is readable from here.
* **Whether any client connects via the unix socket rather than TCP.** This
  determines the true risk of step 2; step 1 makes it moot either way.
* **Whether `local trust` is exploitable in practice.** Configuration, mount
  table and uid all say yes; deliberately not confirmed by connecting.

<!-- EOF -->
