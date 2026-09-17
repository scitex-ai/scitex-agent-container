"""The fleet secrets POOL and the ``.env`` carrier it is folded into.

Extracted from :mod:`._cct_token_pool` (2026-08-10). Nothing here is specific to
Telegram: it is the generic machinery two token injectors share — read the pool,
say where the pool lives, read and rewrite the agent's materialised ``.env``.
:mod:`._github_token` was already importing all four names out of the CCT module
("shared pool, one source"), which is a GitHub-token module reaching into a
Telegram module for env-file I/O. Now both import from here and the CCT module
keeps only CCT policy.

The pool itself is the set of ``<PREFIX>_<SLOT>`` environment variables visible
to the launching ``sac agents start`` process — the union of its own environment
and the secret files listed in ``SAC_SECRETS_ENVRC`` (colon-separated absolute
paths), with a canonical ``$HOME`` fallback when that var is unset. Token VALUES
never appear in a log line; only slot names and paths do.
"""

from __future__ import annotations

import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path

# The .envrc secrets-preamble env var (shared with :mod:`._envrc`).
_SECRETS_ENVRC_VAR = "SAC_SECRETS_ENVRC"


@dataclass(frozen=True)
class PoolRead:
    """One read of the secrets pool, and whether a MISS in it means anything.

    A HIT is always conclusive — a slot we found is a slot that is there. The
    interesting question is the other direction, and it has THREE answers, not
    two: the slot is absent, or *sac never looked at the pool it meant to
    look at*. :attr:`trusted` is exactly that distinction, and collapsing it is
    the bug this class exists to prevent.

    THE MEASUREMENT BEHIND IT (2026-08-12, card
    ``sac-cct-token-slot-mismatch-and-env-fold-20260812``). After
    ``scitex-agent-container`` was relocated to a new host its Telegram rail
    vanished, and the first three diagnoses — mine — all said "there is no
    token on compute-04". Every one was wrong. The pool file was present on
    that host, complete, with all fifty secret files intact. What was missing
    was ``SAC_SECRETS_ENVRC`` in the *launching* ``sac-listen.service``
    environment, so the resolver found no secret file to source and sac read
    the bare process env instead. The operator's own correction is the whole
    specification for this field:

        「04 にトークンが無い」と私は言ったが誤り。**起動プロセスに無かった**
        が正しい。この区別がバグそのもの。

    ("I said 'there is no token on 04'. That was wrong — 'it was not in the
    LAUNCHING PROCESS' is right. That distinction IS the bug.")

    So ``trusted`` is False whenever sac sourced no pool FILE at all, even
    though :attr:`env` is still populated from the process environment. The
    process env can prove a slot present; it cannot prove one absent.

    Attributes
    ----------
    env
        The variables read. Values are secrets — never log, print, or embed
        them; check presence only.
    trusted
        Whether a MISS in :attr:`env` is CONCLUSIVE. True only when the
        canonical secret file(s) resolved AND sourced cleanly.
    detail
        Why the read is untrusted (empty when it is trusted), phrased for an
        operator-facing message. Paths only, never values.
    """

    env: dict[str, str]
    trusted: bool
    detail: str = ""


def _logger():
    """scitex-logging logger, imported lazily (same rationale as
    ``config.__init__._config_logger``: the package auto-configures
    handlers on first import, which must not tax module import)."""
    import scitex_logging

    return scitex_logging.getLogger(__name__)


def _read_env_file(path: Path) -> dict[str, str]:
    """Parse a plain ``KEY=VALUE``-per-line env file (the fold's format).

    Tolerates blank lines and ``#`` comments; no shell semantics (the fold
    writes raw values, no quoting/export).
    """
    env: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        key, sep, val = line.partition("=")
        if sep:
            env[key.strip()] = val
    return env


_ENV_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

# A secret file is data, but its parsed mapping is later passed to bash (and
# provider/runtime children).  These names change how the next process starts
# or what code it loads before its argv runs, so an owner-only file is not
# sufficient authority to set them.  In particular, non-interactive bash
# sources $BASH_ENV even under --noprofile/--norc, and the dynamic loader acts
# on LD_* before Python can regain control.
_PROCESS_CONTROL_NAMES = frozenset(
    {
        "BASH_ENV",
        "ENV",
        "PATH",
        "SHELLOPTS",
        "BASHOPTS",
        "CDPATH",
        "GLOBIGNORE",
        "BASH_XTRACEFD",
        "BASH_LOADABLES_PATH",
        "PS4",
        "ZDOTDIR",
        "KSH_ENV",
        "FPATH",
        "INPUTRC",
        "PYTHONPATH",
        "PYTHONHOME",
        "PYTHONSTARTUP",
        "PYTHONINSPECT",
        "PYTHONBREAKPOINT",
        "PYTHONWARNINGS",
        "PERL5OPT",
        "PERL5LIB",
        "RUBYOPT",
        "RUBYLIB",
        "NODE_OPTIONS",
        "NODE_PATH",
        "GCONV_PATH",
        "GLIBC_TUNABLES",
    }
)
_PROCESS_CONTROL_PREFIXES = ("LD_", "DYLD_", "GIT_", "BASH_FUNC_")


def _is_process_control_name(name: str) -> bool:
    return name in _PROCESS_CONTROL_NAMES or name.startswith(
        _PROCESS_CONTROL_PREFIXES
    )


class SecretPoolFileError(RuntimeError):
    """A value-free secret-file refusal safe to expose in logs/errors."""

    def __init__(self, category: str) -> None:
        self.category = category
        super().__init__(category)


def _parse_secret_file(content: str) -> dict[str, str]:
    """Parse literal assignments only; never invoke a shell.

    ``export KEY=VALUE`` is explicitly supported for the fleet's existing
    ``*.src`` format.  Quotes may wrap the entire literal value and are removed;
    interpolation, command substitution, redirects, pipelines and standalone
    shell commands are rejected rather than interpreted.
    """
    env: dict[str, str] = {}
    for raw_line in content.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        key, sep, value = line.partition("=")
        key = key.strip()
        if not sep or not _ENV_KEY_RE.fullmatch(key):
            raise SecretPoolFileError("secret_file_syntax")
        if _is_process_control_name(key):
            raise SecretPoolFileError("secret_file_control_variable")
        value = value.strip()
        if any(marker in value for marker in ("$(", "${", "`", "\x00")):
            raise SecretPoolFileError("secret_file_shell_syntax")
        if value[:1] in {"'", '"'}:
            quote = value[0]
            if len(value) < 2 or value[-1] != quote or quote in value[1:-1]:
                raise SecretPoolFileError("secret_file_syntax")
            value = value[1:-1]
        elif value[-1:] in {"'", '"'} or any(
            char.isspace() or char in ";&|<>" for char in value
        ):
            raise SecretPoolFileError("secret_file_shell_syntax")
        env[key] = value
    return env


def _effective_uid() -> int:
    """Effective owner expected for a host secret (small testable OS seam)."""
    return os.geteuid()


def _open_secret_fd(path: Path, flags: int) -> int:
    """Descriptor-open seam; production always delegates directly to ``os.open``."""
    return os.open(path, flags)


def _read_secret_file_secure(path: Path) -> dict[str, str]:
    """Read one canonical owner-only regular file through its verified fd."""
    candidate = path.expanduser()
    if not candidate.is_absolute():
        raise SecretPoolFileError("secret_file_noncanonical")
    absolute = Path(os.path.abspath(candidate))
    try:
        canonical = candidate.resolve(strict=True)
    except OSError as exc:
        raise SecretPoolFileError("secret_file_unavailable") from exc
    if canonical != absolute:
        raise SecretPoolFileError("secret_file_symlink")
    try:
        expected = os.stat(canonical, follow_symlinks=False)
    except OSError as exc:
        raise SecretPoolFileError("secret_file_unavailable") from exc
    if not stat.S_ISREG(expected.st_mode):
        raise SecretPoolFileError("secret_file_not_regular")

    flags = (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_NONBLOCK", 0)
    )
    try:
        fd = _open_secret_fd(canonical, flags)
    except OSError as exc:
        raise SecretPoolFileError("secret_file_open") from exc
    try:
        observed = os.fstat(fd)
        if not stat.S_ISREG(observed.st_mode):
            raise SecretPoolFileError("secret_file_not_regular")
        if observed.st_uid != _effective_uid():
            raise SecretPoolFileError("secret_file_owner")
        if stat.S_IMODE(observed.st_mode) & 0o077:
            raise SecretPoolFileError("secret_file_permissions")
        if (observed.st_dev, observed.st_ino) != (expected.st_dev, expected.st_ino):
            raise SecretPoolFileError("secret_file_identity")
        # O_NONBLOCK makes a late regular-file→FIFO swap safe at open time.  It
        # has served its purpose once fstat has proved this descriptor is the
        # expected regular inode; clear it before the stream read.
        os.set_blocking(fd, True)
        with os.fdopen(
            fd, "r", encoding="utf-8", errors="strict", closefd=True
        ) as stream:
            fd = -1
            content = stream.read()
    except UnicodeError as exc:
        raise SecretPoolFileError("secret_file_encoding") from exc
    finally:
        if fd >= 0:
            os.close(fd)
    return _parse_secret_file(content)


def _write_env_file(path: Path, env: dict[str, str]) -> None:
    """Rewrite ``path`` as sorted ``KEY=VALUE`` lines, owner-only perms."""
    body = "".join(f"{k}={v}\n" for k, v in sorted(env.items()))
    path.write_text(body, encoding="utf-8")
    os.chmod(path, 0o600)


def read_pool() -> PoolRead:
    """Read the pool and say whether a MISS in it is CONCLUSIVE.

    Resolves the secret files via :func:`._envrc.resolve_secret_files`, which
    honours an explicit ``SAC_SECRETS_ENVRC`` AND — the 2026-07-18 class fix —
    falls back to the canonical ``$HOME`` default pool when the var is unset, so
    a cron / raw-ssh / federated-timer restart that never had the var exported
    still finds the bot token instead of folding (and STRIPPING) it. Files are
    descriptor-opened without symlink following, verified, and parsed as
    literal assignments; they are never shell-sourced.

    Three outcomes, and the middle one is the point (see :class:`PoolRead`):

    * secret file(s) resolved AND parsed cleanly → ``trusted=True``. A miss
      here means the slot really is not in the pool.
    * secret file(s) resolved but verification/parsing FAILED → ``trusted=False``. We
      hold the process env, which is not the pool we meant to read.
    * NO secret file resolved at all → ``trusted=False``. This is the
      relocation shape: the pool exists on disk and sac never opened it,
      because the launching process was not told where it lives. Reporting
      that as "the slot is absent" is the false negative that cost the
      operator his channel to an agent.

    Never raises — a pool that cannot be read is a verdict, not a crash, and
    the caller's missing-token message names the pool source either way.
    """
    from ._envrc import resolve_secret_files

    files = resolve_secret_files()
    if not files:
        return PoolRead(
            env=dict(os.environ),
            trusted=False,
            detail=(
                f"no canonical secret file resolved ({_pool_source_label()}), so "
                "sac read only the LAUNCHING PROCESS environment. That can prove "
                "a slot PRESENT but never proves one ABSENT — the pool file may "
                "be on this host, intact, and simply not visible to whatever "
                "started the agent (a systemd unit with no "
                f"{_SECRETS_ENVRC_VAR}, a non-interactive ssh, a cron tick)"
            ),
        )
    env = dict(os.environ)
    try:
        for path in files:
            env.update(_read_secret_file_secure(path))
    except SecretPoolFileError as exc:  # stx-allow: fallback (reason: a rejected pool read is an UNTRUSTED verdict, not a process crash; values/content are deliberately absent from the diagnostic)
        _logger().warning(
            "secrets pool: rejected secret file category=%s; using launching "
            "process environment only (slot misses are inconclusive)",
            exc.category,
        )
        return PoolRead(
            env=dict(os.environ),
            trusted=False,
            detail=(
                f"secret pool rejected (category={exc.category}); sac used only "
                "the launching process environment, so a miss is inconclusive"
            ),
        )
    return PoolRead(env=env, trusted=True, detail="")


def _pool_env() -> dict[str, str]:
    """The pool as a plain mapping — :func:`read_pool` without the verdict.

    Kept for the callers that only ever ask "is this slot here?", where a hit
    is self-validating and the trust flag adds nothing. Anything that reports a
    MISS to a human must use :func:`read_pool` instead, so it can tell "absent"
    from "never looked".
    """
    return read_pool().env


def _pool_source_label() -> str:
    """Human-readable pool location for log lines (paths only, no values)."""
    raw = os.environ.get(_SECRETS_ENVRC_VAR, "")
    if raw:
        return f"{_SECRETS_ENVRC_VAR}={raw}"
    # Class fix (2026-07-18): an unset var no longer means an empty pool — the
    # resolver falls back to the canonical ``$HOME`` default. Report THAT so the
    # missing-token WARN names where sac actually looked, not a pool it stopped
    # limiting itself to.
    from ._envrc import resolve_secret_files

    defaults = resolve_secret_files()
    if defaults:
        joined = ":".join(str(p) for p in defaults)
        return f"{_SECRETS_ENVRC_VAR} unset — using the canonical default pool {joined}"
    return (
        f"{_SECRETS_ENVRC_VAR} is UNSET and no canonical default pool files were "
        "found — pool limited to the launching process environment"
    )
