"""On-disk argv/plan snapshot writer — REDACTED, never raw (card
``sac-argv-token-plaintext``, security finding 2026-05-24).

Both the apptainer and TUI dry-run paths persist the resolved launch
``argv`` to ``<state_dir>/apptainer_run.argv.txt`` for debugging /
``sac agents explain`` parity. Before this module existed, that write
embedded secret env values (``SAC_ANTHROPIC_API_KEY=<real token>``,
``SAC_LISTEN_BEARER=<real bearer>``, ...) in PLAINTEXT on disk — the
console-facing plan preview (``cli_pkg._explain.render_plan``) already
redacted the same values before printing, but the FILE write bypassed
that treatment entirely.

:func:`write_redacted_argv` is the single write path both runtimes now
share: every argv element shaped like ``KEY=value`` with a
secret-looking ``KEY`` (see ``_state._meta.secrets._SECRET_ENV`` —
``*_API_KEY`` / ``*_TOKEN`` / ``*_BEARER`` / ``*_KEY`` / ``*_SECRET`` /
``*_PASSWORD`` / ``*_CREDENTIAL``) gets its value masked; plain flags
(``--bind``, ``--env``, ...) and non-secret ``KEY=value`` pairs
(``CLAUDE_AGENT_ID=...``) pass through untouched — identical behaviour
to the console preview, so the two surfaces never drift apart.

The real (unredacted) argv is untouched — it's what actually reaches
the ``apptainer``/``tmux`` subprocess; only the ON-DISK RECORD is
masked. The file is also chmod'd 0600 belt-and-suspenders (the
directory/file should already be per-agent-private, but a stray
world/group-readable state dir must not turn a masked-at-write-time
file into the only thing standing between a secret and a co-tenant).
"""

from __future__ import annotations

import stat
from pathlib import Path

from .._state._meta.secrets import _redact_env_entry


def write_redacted_argv(path: Path, argv: list[str]) -> None:
    """Write ``argv`` to ``path``, one element per line, secrets masked."""
    redacted = [_redact_env_entry(a) for a in argv]
    path.write_text("\n".join(redacted) + "\n")
    try:
        path.chmod(stat.S_IRUSR | stat.S_IWUSR)  # 0600 — owner rw only
    except OSError:  # stx-allow: fallback (best-effort hardening only)
        pass
