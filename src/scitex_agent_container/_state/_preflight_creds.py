"""Pre-dispatch OAuth credential expiry check.

If the OAuth ``accessToken`` an agent will authenticate with is already
expired (or about to expire), the in-container ``claude`` 401s and exits
before rendering — surfacing only as an empty pane with no cause. This
module catches that condition *before* dispatch fires so the operator
gets a clear, actionable message instead.

WHICH FILE the gate reads is the whole point (outage 2026-08-10). An
agent authenticates with the credential ITS SPEC declares, and for most
of the fleet that is NOT the lead's ``~/.claude/.credentials.json``:

* ``spec.claude.credentials_files`` (the account POOL) — or the singular
  ``spec.claude.credentials_file`` treated as a 1-element pool — is
  collapsed to ONE picked entry at launch by
  :func:`_lifecycle._start_preflight._rotate_to_healthy_account`, and
  :func:`runtimes._apptainer_auth_bind.credentials_file_bind` binds THAT
  file at the container's ``$HOME/.claude/.credentials.json``. For a
  designated file :func:`runtimes._apptainer_auth.auth_argv` returns
  early, so ``~/.claude/`` is not bound for such an agent at all.
* ``spec.claude.account`` resolves to the stored snapshot at
  ``<account-store>/<account>/.credentials.json``
  (:func:`runtimes._apptainer_creds.resolve_cred_file`); that snapshot's
  DIRECTORY is what gets dir-bound at ``/tmp/sac-claude``.
* ONLY a fully-unpinned spec (no pool, no file, no account, no provider)
  falls back to the lead's ``~/.claude/`` — which IS still dir-bound at
  ``/tmp/sac-claude`` with ``CLAUDE_CONFIG_DIR`` pointed at it for that
  branch. So the lead file remains load-bearing, but it is one artefact
  among several rather than *the* authoritative one.

:func:`check_spec_oauth_credentials` therefore resolves the same
candidate list the runtime will, in the same order, and proceeds when
ANY candidate is usable — the launch-time picker then makes the
quota-aware choice among them and fails loud on its own health model if
it rejects them all. It refuses only when EVERY declared candidate is
expired/missing/malformed, and its message names each one. Before this,
the gate always read ``~/.claude/.credentials.json`` and so refused
every start on a host whose lead token had lapsed while a dozen agents
ran happily on fresh pool credentials — a false negative, not a guard.

Hard rules (no silent fallbacks):

* Missing credentials file → :class:`FileNotFoundError` with the path
  in the message.
* Unparseable JSON → :class:`ValueError`.
* JSON missing the ``claudeAiOauth`` field → :class:`ValueError`.
* ``claudeAiOauth`` missing the ``expiresAt`` field → :class:`ValueError`.
* Token already expired → :class:`RuntimeError` with seconds-ago count.
* Token expiring within 5 minutes → :class:`RuntimeError` with seconds-
  remaining count.

The check is skipped entirely when the operator opts out of OAuth by
setting ``ANTHROPIC_API_KEY`` or ``SAC_ANTHROPIC_API_KEY`` in env (the
API-key auth path; see ``runtimes/_sdk_common.provision_anthropic_auth``).
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

# Skew threshold: refuse to dispatch when the token has less than this
# many seconds of life left. Five minutes is the practical lower bound —
# claude-code's own refresh loop wakes roughly at the 5-min mark, and a
# fresh agent starting under a sub-300s token is virtually guaranteed
# to fail mid-session.
EXPIRY_SKEW_SECONDS = 300


def _default_credentials_path() -> Path:
    """Return the lead-host live credentials path.

    The FALLBACK candidate only — it is what a fully-unpinned spec (no
    ``credentials_files`` / ``credentials_file`` / ``account`` /
    ``provider``) actually authenticates with, via the ``~/.claude/``
    dir-bind at ``/tmp/sac-claude``. A spec that declares any credential
    of its own never reaches this path; see the module docstring.

    Kept as a helper (not a module-level constant) so tests can monkey-
    free-replace it via the ``creds_path`` parameter without touching
    ``~/.claude/`` on disk.
    """
    return Path.home() / ".claude" / ".credentials.json"


def _api_key_env_is_set() -> bool:
    """True when the operator has opted into the API-key auth path.

    ``ANTHROPIC_API_KEY`` is the SDK-native env var; ``SAC_ANTHROPIC_API_KEY``
    is sac's preferred form that gets mirrored into ``ANTHROPIC_API_KEY``
    by ``runtimes/_sdk_common.provision_anthropic_auth`` at runtime. Either
    indicates the operator is not relying on the OAuth ``credentials.json``
    so the expiry check is moot.
    """
    return bool(
        os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("SAC_ANTHROPIC_API_KEY")
    )


def check_oauth_token_expiry(
    creds_path: Path | None = None,
    *,
    now: float | None = None,
    skew_seconds: int = EXPIRY_SKEW_SECONDS,
) -> None:
    """Raise if the lead-side OAuth token is missing, malformed, or expired.

    Parameters
    ----------
    creds_path
        Path to the credentials JSON. Defaults to
        ``~/.claude/.credentials.json`` on the lead host. Tests pass an
        explicit ``tmp_path`` to avoid touching the operator's real file.
    now
        Override for the wall clock (unix seconds). Defaults to
        ``time.time()``. Tests use this to pin the comparison instant.
    skew_seconds
        Refuse to dispatch when the token has fewer than this many
        seconds of life remaining. Defaults to
        :data:`EXPIRY_SKEW_SECONDS` (300 = 5 minutes).

    Raises
    ------
    FileNotFoundError
        When the credentials file does not exist. The message includes
        the absolute path so the operator knows where ``claude login``
        is expected to write.
    ValueError
        When the file is not valid JSON, or when it is valid JSON but
        does not contain the expected ``claudeAiOauth.expiresAt`` shape.
    RuntimeError
        When the token has already expired, or when its remaining
        lifetime is below ``skew_seconds``. The message tells the
        operator how to refresh it (``claude login``).
    """
    if _api_key_env_is_set():
        return

    path = creds_path if creds_path is not None else _default_credentials_path()
    now_ts = now if now is not None else time.time()

    if not path.is_file():
        raise FileNotFoundError(
            f"OAuth credentials file not found at {path!s}. "
            "Run `claude login` to create it, or export "
            "ANTHROPIC_API_KEY / SAC_ANTHROPIC_API_KEY to use the "
            "API-key auth path instead."
        )

    raw = path.read_text(encoding="utf-8")
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(
            f"OAuth credentials file at {path!s} is not valid JSON: {exc}. "
            "Run `claude login` to regenerate it."
        ) from exc

    if not isinstance(data, dict) or "claudeAiOauth" not in data:
        raise ValueError(
            f"OAuth credentials file at {path!s} is missing the "
            "`claudeAiOauth` field. Run `claude login` to regenerate it."
        )

    oauth = data["claudeAiOauth"]
    if not isinstance(oauth, dict) or "expiresAt" not in oauth:
        raise ValueError(
            f"OAuth credentials file at {path!s} is missing the "
            "`claudeAiOauth.expiresAt` field. Run `claude login` to "
            "regenerate it."
        )

    expires_at_raw = oauth["expiresAt"]
    # claude-code writes expiresAt as a unix-MILLISECOND timestamp (not
    # seconds), so divide by 1000 before comparing. We accept either
    # representation by detecting the magnitude — any value > 1e12 is
    # clearly milliseconds (would otherwise place us in year ~33000+).
    try:
        expires_at_num = float(expires_at_raw)
    except (TypeError, ValueError) as exc:
        raise ValueError(
            f"OAuth credentials file at {path!s} has non-numeric "
            f"`claudeAiOauth.expiresAt={expires_at_raw!r}`. Run "
            "`claude login` to regenerate it."
        ) from exc

    expires_at_seconds = (
        expires_at_num / 1000.0 if expires_at_num > 1e12 else expires_at_num
    )

    delta = expires_at_seconds - now_ts

    if delta <= 0:
        seconds_ago = int(-delta)
        raise RuntimeError(
            f"OAuth token in {path!s} expired {seconds_ago} seconds ago. "
            "Run `claude login` to refresh."
        )

    if delta < skew_seconds:
        seconds_left = int(delta)
        raise RuntimeError(
            f"OAuth token in {path!s} expires in {seconds_left} seconds "
            f"(< {skew_seconds // 60} min). Refresh with `claude login` "
            "before launching long-lived agents."
        )


def spec_credential_candidates(
    claude_spec: object, *, home: Path | None = None
) -> tuple[list[tuple[str, Path]], bool]:
    """Return ``(candidates, declared_by_spec)`` for one agent's claude spec.

    Each candidate is an ``(origin, path)`` pair: ``origin`` is the spec
    field (with its list index) the path came from, so a failure message
    can say WHICH declaration is broken rather than just quoting a path.

    The order is NOT invented here — it mirrors
    :func:`_lifecycle._start_preflight._rotate_to_healthy_account`, the
    function that actually decides what the agent runs on, entry point by
    entry point:

    1. ``credentials_files`` (plural) — the account POOL, in declared
       order. The launch-time picker chooses one of exactly these.
    2. else ``credentials_file`` (singular) — treated as a 1-element pool
       by that same function.
    3. else ``account`` — the stored snapshot at
       ``<account-store>/<account>/.credentials.json``, resolved through
       the same :func:`_state.account_store._store_path` cascade
       :func:`runtimes._apptainer_creds.resolve_cred_file` uses.
    4. else the lead's ``~/.claude/.credentials.json`` — the ONLY case
       where ``declared_by_spec`` is ``False``.

    ``home`` is the test seam for (3) and (4); production passes ``None``
    → ``Path.home()``.
    """
    raw_pool = getattr(claude_spec, "credentials_files", None) or []
    files = [str(p).strip() for p in raw_pool if str(p).strip()]
    single = str(getattr(claude_spec, "credentials_file", "") or "").strip()
    if files:
        return (
            [
                (f"spec.claude.credentials_files[{idx}]", Path(raw).expanduser())
                for idx, raw in enumerate(files)
            ],
            True,
        )
    if single:
        return [("spec.claude.credentials_file", Path(single).expanduser())], True

    account = str(getattr(claude_spec, "account", "") or "").strip()
    if account:
        from .account_store import _store_path

        _home = home if home is not None else Path.home()
        snapshot = _store_path(None, _home) / account / ".credentials.json"
        return [(f"spec.claude.account={account!r}", snapshot)], True

    if home is not None:
        return [
            ("~/.claude/.credentials.json", home / ".claude" / ".credentials.json")
        ], False
    return [("~/.claude/.credentials.json", _default_credentials_path())], False


def check_spec_oauth_credentials(
    config: object,
    *,
    now: float | None = None,
    skew_seconds: int = EXPIRY_SKEW_SECONDS,
    home: Path | None = None,
) -> Path | None:
    """Raise unless SOME credential the agent may actually use is usable.

    ``config`` is an :class:`~scitex_agent_container.config.AgentConfig`
    (anything exposing ``.claude`` and ``.name`` works). Candidates come
    from :func:`spec_credential_candidates`; each is put through
    :func:`check_oauth_token_expiry`.

    Passing is an ANY, not an ALL: the pool exists precisely so one
    expired account does not ground the agent, and the launch-time
    quota-aware picker (which applies its own, stricter health model and
    fails loud by itself) decides which entry actually gets bound. This
    gate only has to establish that a start is not doomed before it
    starts.

    Returns the first usable path, or ``None`` when the check was skipped
    because ``ANTHROPIC_API_KEY`` / ``SAC_ANTHROPIC_API_KEY`` is set.

    Raises
    ------
    FileNotFoundError, ValueError, RuntimeError
        For an UNDECLARED spec the single default-path failure propagates
        verbatim, so the no-spec-credential case keeps exactly the message
        and exception type it has always had.
    RuntimeError
        When a spec DECLARES credentials and every one of them fails. The
        message names each candidate, its path, and its own reason — the
        operator has to see which accounts to refresh, not just the first.
    """
    if _api_key_env_is_set():
        return None

    claude_spec = getattr(config, "claude", None)
    candidates, declared = spec_credential_candidates(claude_spec, home=home)

    if not declared:
        _origin, path = candidates[0]
        check_oauth_token_expiry(path, now=now, skew_seconds=skew_seconds)
        return path

    failures: list[str] = []
    for origin, path in candidates:
        try:
            check_oauth_token_expiry(path, now=now, skew_seconds=skew_seconds)
        except (FileNotFoundError, ValueError, RuntimeError) as exc:
            failures.append(f"  - {origin} ({path!s}): {exc}")
            continue
        return path

    name = str(getattr(config, "name", "") or "") or "<unnamed>"
    detail = "\n".join(failures)
    raise RuntimeError(
        f"agent {name!r}: every credential its spec declares is unusable "
        f"({len(failures)} candidate(s) checked, none passed):\n{detail}\n"
        "Fix: `claude /login` to one of those accounts then "
        "`sac accounts sync-live` on this host, or export "
        "ANTHROPIC_API_KEY / SAC_ANTHROPIC_API_KEY to use the API-key "
        "auth path instead."
    )


__all__ = [
    "EXPIRY_SKEW_SECONDS",
    "check_oauth_token_expiry",
    "check_spec_oauth_credentials",
    "spec_credential_candidates",
]
