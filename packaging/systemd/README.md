# sac accounts refresh — systemd-user timer

Headless rotation of the Claude Code OAuth access-token using the
long-lived refresh-token stored under
`~/.scitex/agent-container/accounts/<name>/.credentials.json`. Removes
the need for routine manual `claude /login` for stored accounts.

The CLI lives at `sac accounts refresh [<name>] [--all]`; this directory
ships the periodic-execution unit files. Host install is intentionally
NOT done by the sac package — copy the two files into the user's
systemd-user directory and enable the timer:

```bash
mkdir -p ~/.config/systemd/user
install -m 0644 packaging/systemd/sac-accounts-refresh.service \\
                packaging/systemd/sac-accounts-refresh.timer   \\
                ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now sac-accounts-refresh.timer
# Verify
systemctl --user list-timers sac-accounts-refresh.timer
journalctl --user -u sac-accounts-refresh.service -n 50
```

The unit calls `sac accounts refresh --all` every ~4 hours (and 15
minutes after boot / login), with `Persistent=true` so missed runs are
caught up on next boot. The unit exits non-zero only when EVERY stored
account's refresh failed — that's the operator's signal that a real
`claude /login` is finally needed.

The service is read-only with respect to source code; the only state
mutation is atomic write-back of the refreshed access_token to the
per-account credentials file (the same write the legacy
`_refresh_access_token` path already performs).
