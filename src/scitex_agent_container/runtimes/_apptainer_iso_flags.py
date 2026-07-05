"""D1/D5 isolation-flag prepend computation for the apptainer runtime.

See ``docs/adr/0001-isolation-hardening.md``. Pulled out of
``_apptainer_runtime.py`` so the per-flag logic lives in one place and
the runtime file stays under sac's 512-line cap.

The function is pure: it reads ``config.apptainer`` and returns the
list of flags that ``build_run_argv`` should prepend right after
``apptainer exec``. Each flag is skipped when:

* ``apptainer.relaxed: true`` — operator opted out of hardened mode, OR
* the operator already declared the flag in ``apptainer.raw_args``, OR
* (flag-specific) the flag is incompatible with another declared option.

``--writable-tmpfs`` is additionally skipped when ``apptainer.overlay``
is set — apptainer rejects the combination.

Env-cleanliness is DECOUPLED from ``relaxed`` (operator directive
2026-07-05). ``--cleanenv`` is ALWAYS applied (unless the operator
declared it in ``raw_args``) — i.e. it is NOT gated on ``not relaxed``
like the filesystem-isolation flags. Rationale: ``relaxed`` is about
FILESYSTEM / bind isolation (whether ``--containall`` drops the default
``$HOME`` / ``$PWD`` / ``/tmp`` auto-binds, whether ``--home`` /
``--writable-tmpfs`` are forced). Whether the container inherits the
launching process's AMBIENT environment is an ORTHOGONAL concern: the
container's env must come ONLY from sac's explicit ``--env`` /
``--env-file`` flags + apptainer's own ``APPTAINERENV_*`` directives +
the SIF ``%environment`` — NEVER from ambient host-process passthrough.
Without ``--cleanenv``, apptainer forwards the ENTIRE ambient process
env into the container, so any stale var in the launching shell/daemon
(of ANY name) rides through into the agent. Making ``--cleanenv``
unconditional is a GENERIC clean-environment launch that needs zero
knowledge of any specific downstream variable name. Verified safe for
relaxed agents: ``--cleanenv`` still honours the ``APPTAINERENV_*``
injection directives (e.g. sac's ``APPTAINERENV_APPEND_PATH`` cargo-bin
append), so relaxed agents keep everything they actually rely on — only
the un-asked-for ambient passthrough is dropped.
"""

from __future__ import annotations


def compute_iso_prepend(config) -> list[str]:
    """Return the auto-prepend isolation flags for ``apptainer exec``."""
    ap = getattr(config, "apptainer", None)
    relaxed = bool(getattr(ap, "relaxed", False)) if ap else False
    raw = list(getattr(ap, "raw_args", None) or []) if ap else []
    overlay = (getattr(ap, "overlay", "") or "") if ap is not None else ""
    fakeroot_decl = bool(getattr(ap, "fakeroot", False)) if ap else False

    op_containall = any("--containall" in a or a == "--contain" for a in raw)
    op_cleanenv = any(a == "--cleanenv" for a in raw)
    op_writable_tmpfs = any(a == "--writable-tmpfs" for a in raw)
    op_home = any(a == "--home" for a in raw)
    op_fakeroot = any(a == "--fakeroot" for a in raw)

    out: list[str] = []
    if (not relaxed) and not op_containall:
        out.append("--containall")
    # --cleanenv is DECOUPLED from `relaxed` (operator directive
    # 2026-07-05): always applied unless operator-declared in raw_args.
    # See module docstring — env-cleanliness is orthogonal to the
    # filesystem-isolation `relaxed` opt-out, and is the generic,
    # name-agnostic mechanism that stops ANY ambient host var (of any
    # name) from forwarding into the container.
    if not op_cleanenv:
        out.append("--cleanenv")
    if (not relaxed) and not op_writable_tmpfs and not overlay:
        out.append("--writable-tmpfs")
    # D5: canonical operator-independent HOME. Matches the preflight
    # invariant ``$HOME == /home/agent``.
    if (not relaxed) and not op_home:
        out += ["--home", "/home/agent"]
    if fakeroot_decl and not op_fakeroot:
        out.append("--fakeroot")
    return out


__all__ = ["compute_iso_prepend"]
