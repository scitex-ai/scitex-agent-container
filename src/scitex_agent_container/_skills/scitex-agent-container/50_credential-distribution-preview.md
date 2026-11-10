---
description: |
  [TOPIC] Credential distribution argument preview
  [DETAILS] Inspect send-credentials targets and options without reading
  credential material, discovering refresh holders, contacting peers or sweeping.
tags: [scitex-agent-container-credential-distribution-preview]
---

# Preview credential distribution arguments

```bash
sac accounts send-credentials --all --to peer-key --dry-run --json
sac accounts send-credentials --account account-label --to peer-key --sweep --dry-run
```

Use a configured peer key for an actual transfer. Preview accepts unresolved
account/peer names because it reports supplied arguments only. It retains the
exactly-one `--account`/`--all` requirement, the mandatory `--to`, and the rule
that an `--optional-peer` must also be a target.

The JSON object has `dry_run=true` and `scope="arguments_only"`. With `--all`,
an empty `accounts` list means discovery was not performed. A null validity
floor or remote path leaves its backend default unresolved. `--sweep` records
intent without restarting anything. The hidden `keepalive` alias accepts the
same preview option and may emit its usual deprecation warning on stderr.

The preview branch does not read credentials or pause state, discover peers, verify a
destination, publish material, or qualify a sweep. It never reports a successful
transfer. Ordinary invocation still performs the owning backend's real transfer
and verification path; this preview does not change scheduled invocations.
Shared CLI startup may read global configuration and the version cache and
initialize its log file, as it also does for `sac --version`. This is not a
promise of zero filesystem I/O by the entire process.

Source: `cli_pkg/_credential_preview.py` and `_account_keepalive.py`. Behavioral
controls: `tests/scitex_agent_container/cli_pkg/test__credential_preview.py`;
existing keepalive surface, optional-peer, paused-account and attempt tests
retain the ordinary command's refusal and compatibility coverage.
