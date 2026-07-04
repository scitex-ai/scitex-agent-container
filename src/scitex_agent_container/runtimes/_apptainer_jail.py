"""Jailed-capsule mount-boundary assert (HIGH-PRIORITY security guardrail).

A rogue, un-jailed capsule once walked Spartan's shared GPFS with
``find /`` — a repeat risks losing cluster access. This module enforces,
at the sac apptainer-launch layer (non-bypassable, BEFORE exec), that a
"jailed" capsule can NEVER mount a shared / heavy-metadata filesystem. A
rogue ``find /`` inside a jailed capsule must see only the SIF rootfs +
node-local binds.

A capsule is JAILED when EITHER:

  * its spec resolves to the ``solver`` named group
    (``metadata.labels`` → :func:`config._group_resolver.group_from_labels`)
    — auto-on, non-bypassable; OR
  * ``spec.apptainer.jail: true`` — opt-in for other capsule types.

For a jailed capsule the launch command-builder ENFORCES, before exec:

  1. ``--containall`` is FORCED into the argv (drops apptainer's default
     ``$HOME`` / ``$PWD`` / ``/tmp`` auto-binds) — added if absent, even
     under ``relaxed``.
  2. Every operator-controlled bind SOURCE is ``os.path.realpath``-resolved
     (to catch symlinks that point a benign-looking path at a shared FS —
     e.g. ``~/.cache -> /data/gpfs/...``) and REFUSED if it resolves under
     a forbidden shared-FS prefix. Sources checked:
       * ``spec.apptainer.binds`` entries,
       * ``spec.apptainer.raw_args`` ``--bind`` / ``-B`` entries,
       * the ``APPTAINER_BIND`` / ``SINGULARITY_BIND`` /
         ``APPTAINER_BINDPATH`` / ``SINGULARITY_BINDPATH`` env vars
         (``--containall`` drops apptainer's *default* auto-binds but an
         env var can RE-ADD one that the explicit-args check would miss).
  3. The ``--pwd`` (effective cwd / workdir) is realpath-checked the same
     way.

Failure is FAIL-LOUD: a :class:`RuntimeError` naming the offending bind,
its realpath, and the matched prefix — never a silent strip. The assert
is NON-BYPASSABLE (the spec cannot opt it off).

``realpath`` tolerates a not-yet-existing bind source: node-local sources
like ``$TMPDIR/workdir`` are created AT launch, so the leaf may be absent
when this runs. ``os.path.realpath`` resolves the existing parent chain
(following symlinks) and appends the missing leaf WITHOUT failing — so a
missing leaf launches cleanly while a symlinked existing parent that
resolves under a forbidden prefix is still caught. We deliberately avoid
``Path.resolve(strict=True)`` / ``os.stat`` which require existence.

Prefix matching is PATH-COMPONENT-AWARE (``os.path.commonpath``), so
``/homework`` does NOT match the ``/home`` prefix — a bare ``startswith``
would false-positive. A source is refused when it is at/under a forbidden
prefix OR an ANCESTOR of one: ``--bind /:/host`` or ``--bind /data:/data``
would mount the whole shared FS (including ``/data/gpfs``) and is strictly
worse than ``--bind /data/gpfs/x`` — so both directions are caught
(``commonpath ∈ {source, prefix}``). Disjoint node-local subtrees
(``/tmp``, ``$TMPDIR/workdir``) share only a higher ancestor equal to
neither side, so they still pass.
"""

from __future__ import annotations

import os

__all__ = [
    "DEFAULT_FORBIDDEN_PREFIXES",
    "ENV_BIND_VARS",
    "FORBIDDEN_PREFIXES_ENV",
    "SOLVER_GROUP",
    "enforce_jail",
    "forbidden_prefixes",
    "is_jailed",
    "match_forbidden",
    "scrub_bind_env",
]

# The named group that auto-triggers the jail (non-bypassable).
SOLVER_GROUP = "solver"

# Forbidden shared / heavy-metadata filesystem prefixes. A jailed
# capsule's bind sources (realpath-resolved) must not land under any of
# these. Override via ``SAC_JAIL_FORBIDDEN_PREFIXES`` (os.pathsep-separated
# — i.e. ``:``-separated on POSIX) for site-specific mount layouts.
DEFAULT_FORBIDDEN_PREFIXES: tuple[str, ...] = (
    "/data/gpfs",
    "/data/scratch",
    "/home",
)
FORBIDDEN_PREFIXES_ENV = "SAC_JAIL_FORBIDDEN_PREFIXES"

# Env vars apptainer/singularity read to ADD binds even under
# ``--containall``. Validated (fail-loud) AND scrubbed for a jailed
# capsule so no env-injected bind survives.
ENV_BIND_VARS: tuple[str, ...] = (
    "APPTAINER_BIND",
    "SINGULARITY_BIND",
    "APPTAINER_BINDPATH",
    "SINGULARITY_BINDPATH",
)


# ----------------------------------------------------------------------
# Trigger detection
# ----------------------------------------------------------------------
def is_jailed(config) -> bool:
    """Return True iff ``config`` describes a jailed capsule.

    Jailed when EITHER the resolved named group is ``solver`` (auto-on,
    non-bypassable) OR ``spec.apptainer.jail`` is truthy (opt-in).
    """
    ap = getattr(config, "apptainer", None)
    if ap is not None and bool(getattr(ap, "jail", False)):
        return True
    labels = getattr(config, "labels", None)
    try:
        from ..config._group_resolver import group_from_labels

        grp = group_from_labels(labels)
    except Exception:
        grp = ""
    return str(grp).strip().lower() == SOLVER_GROUP


# ----------------------------------------------------------------------
# Prefix resolution + component-aware match
# ----------------------------------------------------------------------
def forbidden_prefixes() -> list[str]:
    """Return the normalised forbidden shared-FS prefixes.

    Honours the ``SAC_JAIL_FORBIDDEN_PREFIXES`` override (os.pathsep-
    separated); falls back to :data:`DEFAULT_FORBIDDEN_PREFIXES`.
    """
    raw = os.environ.get(FORBIDDEN_PREFIXES_ENV, "").strip()
    if raw:
        parts = [p for p in raw.split(os.pathsep) if p.strip()]
    else:
        parts = list(DEFAULT_FORBIDDEN_PREFIXES)
    return [os.path.normpath(p) for p in parts]


def _is_under(realpath: str, prefix: str) -> bool:
    """Component-aware "does ``realpath`` intersect the forbidden subtree".

    Returns True when ``realpath`` is at/under ``prefix`` (e.g.
    ``/data/gpfs/x`` vs ``/data/gpfs``) OR an ANCESTOR of ``prefix``
    (e.g. ``/data`` or ``/`` vs ``/data/gpfs``) — an ancestor bind mounts
    the ENTIRE shared FS including ``prefix``, so it is strictly worse and
    must also be refused (HPC reviewer, PR #529). The predicate is
    ``commonpath ∈ {realpath, prefix}``.

    Uses ``os.path.commonpath`` (path-component-aware) so ``/homework``
    does NOT match the ``/home`` prefix (a bare ``startswith`` would), and
    a disjoint node-local subtree (``/tmp``, ``$TMPDIR/workdir``) whose
    commonpath with ``prefix`` is a shared ancestor equal to NEITHER side
    still passes. Both args must be normalised absolute paths.
    """
    try:
        cp = os.path.commonpath([realpath, prefix])
    except ValueError:
        # Different roots / a relative path slipped in — no match.
        return False
    return cp == prefix or cp == realpath


def match_forbidden(source: str, prefixes: list[str]) -> tuple[str, str] | None:
    """Realpath ``source`` and return ``(realpath, matched_prefix)`` if it
    lands under any forbidden ``prefixes``, else ``None``.

    ``os.path.realpath`` resolves symlinks in the existing parent chain
    and tolerates a missing leaf (no ``strict=True`` / ``stat``), so a
    not-yet-created node-local source resolves cleanly.
    """
    if not source:
        return None
    rp = os.path.realpath(source)
    for pref in prefixes:
        if _is_under(rp, pref):
            return rp, pref
    return None


# ----------------------------------------------------------------------
# Bind-spec parsing
# ----------------------------------------------------------------------
def _bind_source(bind_spec: str) -> str:
    """Host source (part before the first ``:``) of a bind spec."""
    return str(bind_spec).split(":", 1)[0].strip()


def _split_bind_specs(value: str) -> list[str]:
    """Split a comma-separated ``--bind`` value into individual specs."""
    return [s for s in str(value).split(",") if s.strip()]


def _raw_args_bind_sources(raw_args) -> list[str]:
    """Bind sources declared via ``--bind`` / ``-B`` in ``raw_args``.

    Handles the space-separated (``--bind X``), ``=`` (``--bind=X``,
    ``-B=X``), attached (``-BX``) and comma-multiplexed (``--bind a:b,c:d``)
    forms.
    """
    out: list[str] = []
    args = [str(a) for a in (raw_args or [])]
    i = 0
    while i < len(args):
        a = args[i]
        val: str | None = None
        if a in ("--bind", "-B"):
            if i + 1 < len(args):
                val = args[i + 1]
                i += 1
        elif a.startswith("--bind="):
            val = a[len("--bind=") :]
        elif a.startswith("-B="):
            val = a[len("-B=") :]
        elif a.startswith("-B") and len(a) > 2:
            val = a[2:]
        if val:
            out.extend(_bind_source(s) for s in _split_bind_specs(val))
        i += 1
    return out


def _pwd_value(argv: list[str]) -> str | None:
    """The ``--pwd`` value in ``argv`` (space or ``=`` form), or None."""
    for i, a in enumerate(argv):
        if a == "--pwd" and i + 1 < len(argv):
            return argv[i + 1]
        if a.startswith("--pwd="):
            return a[len("--pwd=") :]
    return None


def _env_bind_sources(env) -> list[tuple[str, str]]:
    """``(var, source)`` for every bind declared in the bind env vars."""
    out: list[tuple[str, str]] = []
    for var in ENV_BIND_VARS:
        val = env.get(var, "")
        if not val:
            continue
        for spec in _split_bind_specs(val):
            out.append((var, _bind_source(spec)))
    return out


# ----------------------------------------------------------------------
# Env scrub (launch environment)
# ----------------------------------------------------------------------
def scrub_bind_env(env: dict) -> None:
    """Remove the bind env vars from ``env`` in place.

    Belt-and-suspenders alongside :func:`enforce_jail`'s fail-loud
    validation: for a jailed capsule NO env-injected bind may survive, so
    the launch environment is stripped of these vars entirely (even
    benign ones) before exec.
    """
    for var in ENV_BIND_VARS:
        env.pop(var, None)


# ----------------------------------------------------------------------
# Enforcement
# ----------------------------------------------------------------------
def _refuse(name, label, source, realpath, prefix, prefixes) -> RuntimeError:
    return RuntimeError(
        f"JAILED capsule {name!r}: REFUSING to launch — {label} has host "
        f"source {source!r} which realpath-resolves to {realpath!r}, under "
        f"the forbidden shared-filesystem prefix {prefix!r}. A jailed "
        "capsule (solver group, or spec.apptainer.jail=true) must NEVER "
        "mount a shared / heavy-metadata filesystem "
        f"(forbidden prefixes: {prefixes}). This assert is non-bypassable "
        "and fail-loud. Rebind the mount to a node-local path "
        "(e.g. under $TMPDIR)."
    )


def enforce_jail(config, argv: list[str], *, env=None) -> None:
    """Enforce the jailed-capsule mount boundary on ``argv`` in place.

    No-op for a non-jailed capsule. For a jailed one:

      * FORCES ``--containall`` into ``argv`` if absent (inserted right
        after ``apptainer exec``);
      * realpath-checks every operator-controlled bind source
        (spec binds + raw_args ``--bind`` + the bind env vars) and the
        ``--pwd`` against the forbidden prefixes, raising a fail-loud
        :class:`RuntimeError` on the first hit.

    ``env`` defaults to ``os.environ``; pass a dict in tests.
    """
    if not is_jailed(config):
        return
    if env is None:
        env = os.environ
    name = getattr(config, "name", "<unknown>")
    prefixes = forbidden_prefixes()

    # 1. Force --containall (drops default $HOME/$PWD/tmp auto-binds).
    if "--containall" not in argv:
        insert_at = 2 if argv[:2] == ["apptainer", "exec"] else 0
        argv.insert(insert_at, "--containall")

    # 2. Collect every operator-controlled bind source.
    ap = getattr(config, "apptainer", None)
    checks: list[tuple[str, str]] = []
    for b in (getattr(ap, "binds", None) or []) if ap is not None else []:
        checks.append((f"spec.apptainer.binds entry {b!r}", _bind_source(str(b))))
    raw_args = getattr(ap, "raw_args", None) if ap is not None else None
    for src in _raw_args_bind_sources(raw_args):
        checks.append((f"raw_args --bind {src!r}", src))
    for var, src in _env_bind_sources(env):
        checks.append((f"${var} bind {src!r}", src))

    for label, source in checks:
        hit = match_forbidden(source, prefixes)
        if hit is not None:
            rp, pref = hit
            raise _refuse(name, label, source, rp, pref, prefixes)

    # 3. --pwd / effective workdir.
    pwd = _pwd_value(argv)
    if pwd is None:
        pwd = str(getattr(config, "workdir", "") or "")
    hit = match_forbidden(pwd, prefixes)
    if hit is not None:
        rp, pref = hit
        raise _refuse(name, f"--pwd {pwd!r}", pwd, rp, pref, prefixes)
