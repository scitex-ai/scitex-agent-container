"""Resolve authored Python paths without confusing container and host names."""

from pathlib import Path


def _is_relative_path(p: str) -> bool:
    """True when ``p`` is a relative path (not absolute, not ~-prefixed)."""
    return bool(p) and not p.startswith("/") and not p.startswith("~")


def resolve_python_venv(
    venv: str | list[str] | None, *, container_namespace: bool = False
) -> str:
    """Resolve the declared venv in its filesystem namespace.

    The exact contracted /uvwork/venv-agent path belongs to the target
    container, not the caller. Its real launch gate qualifies that union.
    All other absolute/home paths retain their existing host checks.

    Accepts:
      * empty/None: no venv activation (returns "").
      * single string: literal path; must exist or RuntimeError.
        Relative paths (no leading / or ~) are returned as-is and
        resolved at start time relative to the workspace dir on the
        target host — launcher-side existence check is skipped.
      * list of strings: explicit fallback chain — first existing
        absolute/home path wins; relative paths are returned at
        first occurrence (no launcher-side check).
        If none exist/match, raises RuntimeError.

    The fallback chain is intentionally per-agent (in the YAML), not a
    sac-internal default — different agents may want different chains,
    and putting it in the YAML keeps the precedence visible to readers.
    """
    if venv is None or venv == "" or venv == []:
        return ""

    if isinstance(venv, str):
        if _is_relative_path(venv) or (
            container_namespace and venv == "/uvwork/venv-agent"
        ):
            # Relative or contracted container path: qualify on the target.
            return venv
        if (Path(venv).expanduser() / "bin" / "activate").exists():
            return venv
        raise RuntimeError(
            f"python-venv {venv!r} has no bin/activate on this host. "
            "Set an existing path or use a list for a fallback chain."
        )

    if isinstance(venv, list):
        if not all(isinstance(p, str) for p in venv):
            raise RuntimeError(f"python-venv list must contain strings, got: {venv!r}")
        for candidate in venv:
            if _is_relative_path(candidate) or (
                container_namespace and candidate == "/uvwork/venv-agent"
            ):
                # Preserve this declared target choice instead of host fallback.
                return candidate
            if (Path(candidate).expanduser() / "bin" / "activate").exists():
                return candidate
        raise RuntimeError(
            f"python-venv chain {venv!r} matched no existing venv on this "
            "host. Create one of these paths or extend the chain."
        )

    raise RuntimeError(
        f"python-venv must be a string or list of strings, got "
        f"{type(venv).__name__}: {venv!r}"
    )
