# 05 — Credentials & accounts

> **Stub.** Scope and outline below; to be fully written in a follow-on.
> Consolidates [`credentials.md`](credentials.md).

## Scope

How `sac` authenticates agents to Claude and keeps a fleet running across quota
limits: a pool of stored account credentials, per-agent credential selection,
token refresh, and quota-aware rotation. Covers both the default Anthropic OAuth
path and non-Anthropic providers.

## TODO — this page will contain

- [ ] The account pool: `sac accounts save` / `list` / `switch` / `delete`, and where stored credentials live (`~/.scitex/agent-container/accounts/<name>/.credentials.json`, mode 0600).
- [ ] Per-agent selection: `spec.claude.credentials_files: [...]` and how the runtime picks a healthy credential at launch.
- [ ] Token freshness: `sac accounts refresh` (mint a fresh access token from the refresh token) and the refresh-race caveat (a login rotates the token; running agents stay stale until restart).
- [ ] Live-credential capture: `sac accounts sync-live` / `watch-live` (auto-snapshot on `claude /login`).
- [ ] Quota: `sac accounts status` / `quota` (5h% / 7d% / tier), `refresh-quota-cache`, and `watch-quota` (auto-rotate when a threshold is hit).
- [ ] `sac accounts mint-token` — access-only credential artifacts.
- [ ] Non-Anthropic providers: `spec.claude.provider` (`deepseek` / `mimo` / `xiaomi` or a custom `{base_url, auth_token_env}` endpoint) and the auth-token-env resolution.
- [ ] Cross-host credential distribution: why a remote host has no whole-home bind, so the master must distribute creds to peers, and the auth-SSOT freshness loop.
- [ ] Security posture and the `:ro` credential-mount decision (see the auth ADRs).

## Related

- [ADR-0016 — provider and account axes](adr/0016-provider-and-account-axes.md)
- [ADR-0017 — credential rotation and refresh race](adr/0017-credential-rotation-and-refresh-race.md)

<!-- EOF -->
