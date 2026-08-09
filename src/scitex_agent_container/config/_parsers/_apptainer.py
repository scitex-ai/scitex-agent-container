"""Parser for ``spec.apptainer`` (F-CS18).

v3-realign promoted several knobs from top-level ``spec``: ``image``,
``binds``, ``env``, ``raw_args``, ``container_workdir``. All five MUST
be wired here — missing any one of them re-introduces silent default
fallback at runtime.
"""

from __future__ import annotations


def parse_apptainer(spec: dict, *, source_path: str = ""):
    """Parse spec.apptainer (F-CS18).

    Three optional fields wire the apptainer-specific build extension:

      * ``post`` — shell snippet for apptainer's ``%post`` block.
      * ``environment`` — KEY=VAL dict for ``%environment``.
      * ``def_file`` — path to a hand-authored ``.def`` (overrides the
        synthesised one).

    Empty / missing block → default ApptainerSpec (no extension).
    """
    from .._types import ApptainerSpec

    raw = spec.get("apptainer", {}) or {}
    if not isinstance(raw, dict):
        return ApptainerSpec()
    env_raw = raw.get("environment", {}) or {}
    if not isinstance(env_raw, dict):
        env_raw = {}
    # v3-realign: apptainer.env (engine-scoped env vars, promoted from
    # top-level spec.env per §3).
    apt_env_raw = raw.get("env", {}) or {}
    if not isinstance(apt_env_raw, dict):
        apt_env_raw = {}
    # v3-realign: apptainer.binds — accepts ``host:container[:mode]``
    # strings, the legacy ``{src, dst, mode}`` dict, and the declared-intent
    # mapping (``source``/``dest``/``mode`` + ``required``/``ensure``/
    # ``hosts``). Shape, source expansion (``~`` -> ``$HOME``, ``$VAR`` ->
    # env value; apptainer expands neither) and validation all live in
    # ``config._bind_intent`` so this parser stays a wiring layer.
    #
    # ``binds`` keeps carrying ONE flat string per entry whatever the shape,
    # so every downstream consumer (the jail guardrail's forbidden-prefix
    # scan, the fleet-default de-dup) is untouched; ``bind_intents`` carries
    # the same entries' conditions for the launch-time resolver.
    from .._bind_intent import parse_bind_entries

    bind_intents = parse_bind_entries(raw.get("binds"), source_path=source_path)
    binds: list[str] = [intent.spec for intent in bind_intents]
    raw_args_raw = raw.get("raw_args", []) or []
    raw_args = [str(a) for a in raw_args_raw] if isinstance(raw_args_raw, list) else []
    return ApptainerSpec(
        image=str(raw.get("image", "") or ""),
        binds=binds,
        bind_intents=bind_intents,
        env={str(k): str(v) for k, v in apt_env_raw.items()},
        raw_args=raw_args,
        container_workdir=str(raw.get("container_workdir", "/work") or "/work"),
        post=str(raw.get("post", "") or ""),
        environment={str(k): str(v) for k, v in env_raw.items()},
        def_file=str(raw.get("def_file", "") or ""),
        nv=bool(raw.get("nv", False)),
        rocm=bool(raw.get("rocm", False)),
        # ``--fakeroot`` (uid 0 inside via userns). Previously dropped by
        # the parser → the YAML key silently no-op'd. Parse it so the
        # curated iso-prepend can emit ``--fakeroot`` (always in a valid
        # position — see _apptainer_iso_flags + _apptainer_argv_guard).
        fakeroot=bool(raw.get("fakeroot", False)),
        overlay=str(raw.get("overlay", "") or ""),
        overlay_size=str(raw.get("overlay_size", "") or ""),
        overlay_create_if_missing=bool(raw.get("overlay_create_if_missing", True)),
        # /tmp scratch sizing. Absent key → dataclass default "2G".
        # Explicit "" (or null) → opt-out (legacy 64 MB session tmpfs).
        # `raw.get(k, default) or default` would collapse "" to the
        # default, so distinguish "absent" from "present-but-empty".
        tmpfs_size=(str(raw["tmpfs_size"]) if raw.get("tmpfs_size") is not None else "")
        if "tmpfs_size" in raw
        else "2G",
        relaxed=bool(raw.get("relaxed", False)),
        # JAILED-capsule mount-boundary opt-in (security guardrail — see
        # runtimes/_apptainer_jail.py). Solver-group specs are jailed
        # automatically regardless of this flag; other capsule types set
        # ``jail: true`` to opt in.
        jail=bool(raw.get("jail", False)),
        nested_build=bool(raw.get("nested_build", False)),
    )
