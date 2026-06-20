"""Fail-loud guard: the credentials file-bind must have a delivered placeholder.

Companion to :mod:`_apptainer_auth`. apptainer FILE binds require the
in-container destination to ALREADY EXIST on the underlying filesystem;
:func:`_apptainer_auth.ensure_credentials_bind_target` pre-creates that
empty placeholder at the host path backing the container ``$HOME``. But
that delivery only works when the backing path is actually the filesystem
apptainer mounts at the container HOME. Two relaxed-spec layouts break it
SILENTLY today, surfacing as the cryptic apptainer FATAL::

    mount … /home/agent/.claude/.credentials.json … destination doesn't
    exist in container

1. **Relaxed ``--home`` + NO usable overlay upper-home.** A
   ``raw_args`` ``--home /home/agent`` makes apptainer mount a FRESH
   tmpfs at the container HOME (verified via ``mount``: ``tmpfs on
   /home/agent``). That tmpfs SHADOWS the workspace-home bind
   (``<state>/home`` → ``/home/agent``), so a placeholder written there
   is invisible in the container. The only host path that survives the
   shadow is the overlay upper-home (bound OVER ``--home`` last in
   :func:`_apptainer_build_argv.build_run_argv`). When the overlay is an
   ``.img`` loopback (can't host an ``upper/``) or absent, there is NO
   upper-home bind → the placeholder is lost → FATAL.

2. **Placeholder creation failed.** :func:`ensure_credentials_bind_target`
   swallows an ``OSError`` on the placeholder ``touch`` (best-effort: a
   write failure must not by itself block launch). If it returned ``None``
   while a bind WILL still be emitted, the destination does not exist →
   FATAL.

This guard runs at argv-assembly time (before the ``:rw`` file-bind is
appended) and RAISES a clear, actionable error naming the missing
placeholder path and the concrete fix, instead of leaving the operator
to decode apptainer's mount-FATAL. It is the highest-value / lowest-risk
half of the overlay-credential migration (handoff item 2, 2026-06-20):
it does not change ANY launch that already works — it only converts a
guaranteed cryptic FATAL into a precise diagnostic.
"""

from __future__ import annotations

from pathlib import Path

from ..config import AgentConfig

__all__ = [
    "CredentialPlaceholderUndeliverableError",
    "assert_credentials_placeholder_delivers",
]


class CredentialPlaceholderUndeliverableError(RuntimeError):
    """A credentials file-bind will be emitted but its host placeholder
    won't reach the container ``$HOME``.

    Raising this BEFORE the ``apptainer exec`` replaces the cryptic
    ``destination doesn't exist in container`` FATAL (the file-bind
    landing on a shadowed/absent host path) with a message that names the
    overlay layout problem and the fix. See the module docstring for the
    two layouts this catches.
    """


def _has_home_override(config: AgentConfig) -> bool:
    """True when ``spec.apptainer.raw_args`` declares an explicit ``--home``.

    A raw-arg ``--home <path>`` makes apptainer mount a fresh tmpfs at the
    container HOME, shadowing the workspace-home bind — the trigger for
    layout #1. (The managed ``--home`` that hardened mode auto-prepends is
    NOT in raw_args and does not create this shadow, so we read raw_args
    only, mirroring :func:`_to_home_overlay.resolve_container_home`.)
    """
    ap = getattr(config, "apptainer", None)
    raw = list(getattr(ap, "raw_args", None) or []) if ap is not None else []
    return "--home" in raw


def assert_credentials_placeholder_delivers(
    config: AgentConfig,
    *,
    bind_flags: list[str],
    placeholder: Path | None,
    overlay_upper_home: Path | None,
) -> None:
    """Fail loud if the credentials placeholder won't back the container HOME.

    No-op when ``bind_flags`` is empty (no credentials bind is emitted —
    provider backend, or neither ``spec.claude.credentials_file`` nor
    ``account`` set), so launches that need no credential are untouched.

    ``placeholder`` is the return of
    :func:`_apptainer_auth.ensure_credentials_bind_target` for this launch;
    ``overlay_upper_home`` is :func:`_to_home_overlay.resolve_overlay_upper_home`
    (``None`` for non-relaxed / ``.img`` / no-overlay specs).

    Raises :class:`CredentialPlaceholderUndeliverableError` when:

      * the placeholder could not be created at all (``placeholder is
        None`` despite a bind being emitted — the swallowed-``OSError``
        path), or
      * the spec declares a relaxed ``--home`` override (fresh tmpfs at the
        container HOME shadows the workspace-home bind) AND there is no
        usable overlay upper-home directory to carry the placeholder past
        the shadow.
    """
    if not bind_flags:
        return

    dest = _bind_dest(bind_flags)
    upper_ok = overlay_upper_home is not None and overlay_upper_home.is_dir()

    if placeholder is None:
        raise CredentialPlaceholderUndeliverableError(
            f"agent {getattr(config, 'name', '?')!r}: credentials file-bind "
            f"target {dest} has no host-side placeholder — apptainer FILE "
            "binds require the destination to pre-exist, so the launch would "
            f"FATAL with 'destination {dest} doesn't exist in container'. "
            "The placeholder write was attempted but failed (see the "
            "preceding 'credentials bind-target placeholder ... could not be "
            "created' warning). Fix: ensure the overlay upper-home / "
            "workspace-home is writable, or remove "
            "spec.claude.credentials_file/account if no credential is needed."
        )

    if _has_home_override(config) and not upper_ok:
        raise CredentialPlaceholderUndeliverableError(
            f"agent {getattr(config, 'name', '?')!r}: spec.apptainer.raw_args "
            "declares --home, which mounts a fresh tmpfs at the container "
            f"$HOME and SHADOWS the workspace-home bind. The credentials "
            f"file-bind target {dest} would then have no backing host path "
            "(its placeholder landed at the shadowed workspace-home "
            f"{placeholder}), so the launch FATALs with 'destination {dest} "
            "doesn't exist in container'. The only host path that survives "
            "the --home shadow is the overlay upper-home, but none is usable "
            f"(overlay_upper_home={overlay_upper_home!r}: absent, or an .img "
            "loopback that cannot host an upper/ directory). Fix: migrate to "
            "a DIRECTORY overlay (spec.apptainer.overlay: <dir>, or raw_args "
            "--overlay <dir> with no .img suffix) so SAC mirrors the "
            "credential placeholder into <overlay>/upper/<home>/; or drop the "
            "raw_args --home and let the managed workspace-home bind deliver "
            "it; or remove spec.claude.credentials_file/account."
        )


def _bind_dest(bind_flags: list[str]) -> str:
    """In-container destination of ``["--bind", "<src>:<dst>:rw"]`` → ``<dst>``.

    Mirrors :func:`_apptainer_auth._bind_destination` (kept local so this
    guard module has no back-import into the larger auth module). Returns
    the canonical credentials path when the shape is unrecognised so the
    diagnostic still names a sensible destination.
    """
    fallback = "$HOME/.claude/.credentials.json"
    if len(bind_flags) < 2:
        return fallback
    body = bind_flags[1]
    for opt in (":rw", ":ro"):
        if body.endswith(opt):
            body = body[: -len(opt)]
            break
    parts = body.rsplit(":", 1)
    return parts[1] if len(parts) == 2 else fallback
