"""Parser for ``spec.apptainer`` (F-CS18).

v3-realign promoted several knobs from top-level ``spec``: ``image``,
``binds``, ``env``, ``raw_args``, ``container_workdir``. All five MUST
be wired here — missing any one of them re-introduces silent default
fallback at runtime.
"""

from __future__ import annotations


def parse_apptainer(spec: dict):
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
    # v3-realign: apptainer.binds — accepts the new shorthand
    # ``host:container[:mode]`` strings OR legacy ``{src, dst, mode}`` dicts
    # (normalised to strings). The SOURCE side (everything before the
    # first colon) is expanded against the operator's environment:
    # ``~`` -> ``$HOME``, ``$VAR`` / ``${VAR}`` -> env value. Apptainer
    # itself does NOT expand env vars in --bind, so we have to do it
    # in sac so spec.yaml stays operator-agnostic (``~/proj/foo`` works
    # for every operator without hardcoding their username).
    import os

    def _expand_bind_src(bind_str: str) -> str:
        # Split off the first colon — that's the source/target boundary.
        # The target side (and optional :mode) stays verbatim; only the
        # source needs expansion.
        if ":" not in bind_str:
            return os.path.expanduser(os.path.expandvars(bind_str))
        src, _, rest = bind_str.partition(":")
        return f"{os.path.expanduser(os.path.expandvars(src))}:{rest}"

    binds_raw = raw.get("binds", []) or []
    binds: list[str] = []
    if isinstance(binds_raw, list):
        for item in binds_raw:
            if isinstance(item, str) and item:
                binds.append(_expand_bind_src(item))
            elif isinstance(item, dict):
                src = str(item.get("src", "") or "")
                dst = str(item.get("dst", "") or "")
                mode = str(item.get("mode", "") or "")
                if src and dst:
                    src = os.path.expanduser(os.path.expandvars(src))
                    binds.append(f"{src}:{dst}:{mode}" if mode else f"{src}:{dst}")
    raw_args_raw = raw.get("raw_args", []) or []
    raw_args = [str(a) for a in raw_args_raw] if isinstance(raw_args_raw, list) else []
    return ApptainerSpec(
        image=str(raw.get("image", "") or ""),
        binds=binds,
        env={str(k): str(v) for k, v in apt_env_raw.items()},
        raw_args=raw_args,
        container_workdir=str(raw.get("container_workdir", "/work") or "/work"),
        post=str(raw.get("post", "") or ""),
        environment={str(k): str(v) for k, v in env_raw.items()},
        def_file=str(raw.get("def_file", "") or ""),
        nv=bool(raw.get("nv", False)),
        rocm=bool(raw.get("rocm", False)),
        overlay=str(raw.get("overlay", "") or ""),
    )
