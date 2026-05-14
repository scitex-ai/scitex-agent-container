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
    if (not relaxed) and not op_cleanenv:
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
