"""Secret env-var hardening for the apptainer runtime (P1 credential fix).

SECURITY
========
apptainer ``--env K=V`` flags land in the launcher's argv, which sac runs
as the command of a tmux ``bash -c '<that whole line>'`` pane. A process's
argv is exposed at ``/proc/<pid>/cmdline``, which is **world-readable** on
Linux — so ANY local process can harvest those values with ``ps`` /
``pgrep -af`` / a plain read of ``/proc`` (no privilege needed). Secrets
injected as ``--env K=V`` (the Anthropic / OpenAI API keys, and the
``sac listen`` bearer that authorises ``host_exec`` — RCE-equivalent) are
therefore readable by every local user for the life of the process.

``/proc/<pid>/environ``, by contrast, is **owner-only**. Putting a secret
in the process ENVIRONMENT is safe; putting it in ARGV to *set* the
environment is the leak. apptainer's ``--env-file <path>`` sets the same
variables by reading a file at exec time, so the secret VALUE never
appears in argv — only the file PATH does.

That only closes the exposure if the file itself is not readable by other
local users: moving a secret from argv to a ``0644`` file merely
RELOCATES the leak. So the file MUST be mode ``0600`` (owner rw only) in a
non-world-readable, launching-user-owned directory (here: a ``0700``
subdir of the per-agent ``~/.scitex/agent-container/runtime/<name>/``
state dir — never world-readable ``/tmp``).

Mechanism
=========
``redact_secret_env_to_file`` post-processes a fully-assembled
``apptainer exec`` argv: it lifts every secret-shaped ``--env KEY=VALUE``
pair out of argv, writes those pairs to the per-agent ``0600`` env-file,
and appends a single ``--env-file <path>``. apptainer ``--env-file`` is
repeatable and accumulates (verified: apptainer 1.5.2 types it
``strings``), and ``--env`` still overrides ``--env-file``; because no
secret-shaped ``--env`` remains after the sweep, every swept value is
delivered to the container unchanged — only its transport moved from
world-readable argv to an owner-only file.

The predicate errs toward protecting: any operator-declared
``spec.env`` / ``raw_args`` var whose NAME looks secret-shaped is swept
too. A false positive is harmless — a non-secret merely also travels via
the ``0600`` file and still reaches the container — while a false
negative would re-open the exposure this module exists to close.
"""

from __future__ import annotations

import os
from pathlib import Path

from .._state._meta.secrets import _SECRET_ENV

# Owner-only subdir + file under the per-agent state dir. Distinct from
# the agent's $HOME/.env (deploy_to_home) and $HOME/secrets/ (to_home) so
# sac's infra secrets never mix into agent-facing files.
_SECRETS_SUBDIR = "secret-env"
_SECRETS_FILENAME = "apptainer.env"


def is_secret_env_key(key: str) -> bool:
    """True when ``key`` names a secret that must not travel in argv.

    Reuses the ecosystem SSOT secret-name matcher
    ``_state._meta.secrets._SECRET_ENV`` — the SAME predicate the on-disk
    argv-snapshot redactor uses (``_apptainer_argv_record`` /
    ``cli_pkg._explain``). Sharing it keeps the two surfaces in lockstep:
    every ``KEY=value`` the redactor would mask in the snapshot is a key
    this sweep also lifts out of the live argv, so neither can drift to
    leave a secret the other would have caught. Matches ``*_API_KEY`` /
    ``*_TOKEN`` / ``*_BEARER`` / ``*_KEY`` / ``*_SECRET`` / ``*_PASSWORD``
    / ``*_CREDENTIAL`` (case-insensitive); none of sac's curated NON-secret
    ``--env`` names (ANTHROPIC_BASE_URL, CLAUDE_CONFIG_DIR, SAC_PROVIDER,
    SCITEX_AGENT_CONTAINER_YAML_DIRS, ...) match.
    """
    return bool(_SECRET_ENV.search(key))


def secret_env_file_path(state_dir: Path) -> Path:
    """The per-agent ``0600`` secrets env-file path under ``state_dir``."""
    return Path(state_dir).expanduser() / _SECRETS_SUBDIR / _SECRETS_FILENAME


def _write_secret_env_file(state_dir: Path, secrets: dict[str, str]) -> Path:
    """Write ``secrets`` as ``KEY=VALUE`` lines to a ``0600`` file.

    The parent dir is created ``0700`` (best-effort) and the file is
    created ``0600`` up-front — there is NO window where the file is
    world-readable — then chmod'd ``0600`` again to also harden a
    PRE-EXISTING file whose mode ``O_CREAT`` would otherwise leave
    untouched. Format matches apptainer ``--env-file``: bare
    ``KEY=VALUE`` lines, no ``export``, no quoting (apptainer is not a
    shell), one per line.
    """
    path = secret_env_file_path(state_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(path.parent, 0o700)
    except OSError:
        # Dir hardening is defense-in-depth; the 0600 FILE below is the
        # actual guarantee (other users cannot open it regardless of the
        # dir's mode). Never fail a launch over the belt-and-suspenders.
        pass

    body = "".join(f"{k}={secrets[k]}\n" for k in sorted(secrets))
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, body.encode("utf-8"))
    finally:
        os.close(fd)
    # Fail loud if we cannot enforce 0600 — better to abort the launch
    # than to hand apptainer a world-readable secrets file (which would
    # silently re-open the exposure this module closes).
    os.chmod(path, 0o600)
    return path


def redact_secret_env_to_file(argv: list[str], *, state_dir: Path) -> list[str]:
    """Move secret ``--env KEY=VALUE`` flags out of argv into a 0600 file.

    Scans ``argv`` for ``--env`` pairs whose KEY is secret-shaped (see
    :func:`is_secret_env_key`), removes them, writes the collected
    ``KEY=VALUE`` set to the per-agent ``0600`` env-file, and appends a
    single ``["--env-file", <path>]`` so apptainer still delivers the
    values to the container (via the owner-only file instead of
    world-readable argv).

    BOTH SPELLINGS ARE SWEPT, because the recogniser is not this
    module's: :func:`._apptainer_env_dedup.env_pair_at` decides what an
    ``--env`` pair is, and this sweep asks it. That sharing is the fix
    for a real hole — this function used to match only the SPLIT
    ``["--env", "K=V"]`` form while ``_apptainer_env_dedup`` also
    recognised the GLUED ``--env=K=V``, so a spec using the glued form
    (live across the fleet's ``raw_args``) put its secret straight into
    the world-readable launcher argv, silently, while every test of this
    module still passed. A second opinion about what counts as the same
    flag is the vulnerability; there is now only one opinion.

    Pure w.r.t. ``argv`` (returns a NEW list; the input is not mutated).
    The only side effect is writing the secrets file. Returns ``argv``
    unchanged (a fresh copy) when no secret ``--env`` is present — no
    env-file is created in that case. Duplicate keys keep the LAST value,
    matching apptainer's ``--env`` last-wins precedence for the pairs the
    sweep removes.
    """
    from ._apptainer_env_dedup import env_pair_at

    secrets: dict[str, str] = {}
    kept: list[str] = []
    i = 0
    n = len(argv)
    while i < n:
        found = env_pair_at(argv, i)
        if found is not None:
            key, value, width = found
            if is_secret_env_key(key):
                secrets[key] = value  # last-wins on duplicate keys
                i += width
                continue
            kept.extend(argv[i : i + width])
            i += width
            continue
        kept.append(argv[i])
        i += 1

    if not secrets:
        return kept

    env_file = _write_secret_env_file(Path(state_dir), secrets)
    kept += ["--env-file", str(env_file)]
    return kept


__all__ = [
    "is_secret_env_key",
    "redact_secret_env_to_file",
    "secret_env_file_path",
]
