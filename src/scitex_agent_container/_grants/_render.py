"""Render an agent's LAUNCH SURFACE without launching it.

The surface is what a grant can change: the ``apptainer`` argv (secret
values redacted the way ``write_redacted_argv`` does), the container env
NAMES (``--env`` flags plus every ``--env-file``), and the materialised home
(file list, symlink targets, env-file key names, content digests of
non-secret files, with the render root normalised to ``<root>`` so two
renders into different directories compare equal). Two renders that compare
equal launch the same agent.

Everything is written under a caller-owned ``root`` — never under the
agent's real state dir, overlay or ``~/.scitex``:

* the state dir is PINNED to ``root/state``: each harness's production
  materialiser and ``build_run_argv`` receive it explicitly (the same
  entry points ``start`` reaches, selected by the same harness key);
* ``SCITEX_AGENT_CONTAINER_RUNTIME_DIR`` points at ``root/runtime`` during
  the render, so helpers that resolve a runtime path at call time (the hook
  manifest, ...) land there too;
* a declared overlay (``apptainer.overlay`` / ``--overlay`` raw_arg) and a
  sealed run's ``run.root`` are relocated under ``root`` on a COPY of the
  config;
* the one host-global writer on that path, ``ensure_global_settings_json``,
  only seeds ``~/.claude/settings.json`` when it is missing or broken.

The same relocation applies to every variant being compared, so a diff
between two renders is a diff caused by the variants, never by the root.
"""

from __future__ import annotations

import copy
import hashlib
import os
import re
import shutil
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator

__all__ = ["LaunchSurface", "render_surface"]

# Never digest these: compare their key NAMES (env files) or presence only.
_SECRETISH = re.compile(
    r"(^|/)(\.env|[^/]*\.env|[^/]*credentials[^/]*|[^/]*token[^/]*|[^/]*\.key|"
    r"pgpass|auth\.json|[^/]*secret[^/]*)$",
    re.IGNORECASE,
)
_ENV_LINE = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)=")


@dataclass(frozen=True)
class LaunchSurface:
    """One render. ``error`` is set when the launch would be refused."""

    argv: tuple[str, ...] = ()
    env_names: tuple[str, ...] = ()
    home: tuple[str, ...] = ()
    error: str | None = None
    notes: tuple[str, ...] = field(default=(), compare=False)

    def lines(self) -> dict[str, tuple[str, ...]]:
        """The comparable sections, for diffing and reporting."""
        return {
            "argv": self.argv,
            "env": self.env_names,
            "home": self.home,
            "error": (self.error,) if self.error else (),
        }


@contextmanager
def _runtime_dir(path: Path) -> Iterator[None]:
    # ``_runners._session_state.DEFAULT_STATE_ROOT`` snapshots the runtime
    # root at IMPORT. Import it before redirecting, so a render can never be
    # its first importer and leave the process pointing at the throwaway root.
    from .._runners import _session_state  # noqa: F401
    from .._runtime_paths import RUNTIME_DIR_ENV

    before = os.environ.get(RUNTIME_DIR_ENV)
    os.environ[RUNTIME_DIR_ENV] = str(path)
    try:
        yield
    finally:
        if before is None:
            os.environ.pop(RUNTIME_DIR_ENV, None)
        else:
            os.environ[RUNTIME_DIR_ENV] = before


def _relocate(config: Any, root: Path) -> Any:
    """A copy of ``config`` whose declared overlays and sealed-run root live
    under ``root`` — so a render never writes where a real start would."""
    out = copy.deepcopy(config)
    sandbox = getattr(out, "sandbox", None)
    if sandbox is not None:
        from dataclasses import replace

        out.sandbox = replace(sandbox, run_root=str(root / "sandbox-runs"))
    ap = getattr(out, "apptainer", None)
    if ap is None:
        return out

    def moved(value: str) -> str:
        name = re.sub(r"[^A-Za-z0-9_.-]+", "_", value.strip("/")) or "overlay"
        target = root / "overlays" / name
        return str(target) + ("/" if value.endswith("/") else "")

    if getattr(ap, "overlay", ""):
        ap.overlay = moved(str(ap.overlay))
    raw = [str(a) for a in (getattr(ap, "raw_args", None) or [])]
    for i, arg in enumerate(raw):
        if arg == "--overlay" and i + 1 < len(raw):
            raw[i + 1] = moved(raw[i + 1])
        elif arg.startswith("--overlay="):
            raw[i] = "--overlay=" + moved(arg.split("=", 1)[1])
    if raw:
        ap.raw_args = raw
    return out


def _materialize(config: Any, state_dir: Path, tui: bool) -> None:
    """The workspace materialiser ``start`` runs for this harness, with the
    state dir PINNED to ``state_dir`` (each production entry point either
    takes it as a parameter or a resolver; none is re-implemented here)."""
    from ..config._harness_registry import HERMES_TUI, OPENCODE_TUI, resolve_harness_key

    key = resolve_harness_key(config)
    if key == HERMES_TUI:
        from ..runtimes._hermes_profile import materialize_hermes_tui_profile

        materialize_hermes_tui_profile(config, state_dir=state_dir)
    elif key == OPENCODE_TUI:
        from ..runtimes._opencode_profile import materialize_opencode_profile

        materialize_opencode_profile(config, state_dir=state_dir)
    elif tui:
        from ..runtimes._tui_workspace import materialize_workspace

        materialize_workspace(config, state_dir_for_config=lambda _c: state_dir)
        if str(getattr(config, "harness", "") or "") == "hermes":
            from ..runtimes._hermes_profile import materialize_hermes_tui_profile

            materialize_hermes_tui_profile(config, state_dir=state_dir, deploy_home=False)
    else:  # headless SDK runner — ClaudeSessionRuntime._setup_workspace
        from ..runtimes._to_home_overlay import deploy_to_home_overlay
        from ..runtimes.claude_md import setup_claude_md
        from ..runtimes.claude_session import _materialize_home_layouts

        home = state_dir / "home"
        home.mkdir(parents=True, exist_ok=True)
        setup_claude_md(config, str(home))
        _materialize_home_layouts(config, str(home))
        deploy_to_home_overlay(config)


def _launch_argv(runtime: Any, config: Any, state_dir: Path) -> list[str]:
    """``build_run_argv`` through the adapter ``start`` selects, state pinned."""
    from ..runtimes._apptainer_runtime import ApptainerContainerRuntime

    if callable(getattr(runtime, "_default_argv", None)):  # TUI family
        from ..runtimes._apptainer_build_argv import build_run_argv

        sif = ApptainerContainerRuntime().resolve_sif(config)
        sif_path = sif if sif is not None else Path("<unresolved>.sif")
        argv = build_run_argv(config, state_dir=state_dir, sif_path=sif_path, tui=True)
        harness = str(getattr(config, "harness", "") or "")
        if harness == "hermes":
            from ..runtimes._hermes_profile import validate_hermes_tui_profile

            validate_hermes_tui_profile(config, state_dir=state_dir, launch_argv=argv)
        if harness == "opencode":
            from ..runtimes._opencode_profile import validate_opencode_profile

            validate_opencode_profile(config, state_dir=state_dir, launch_argv=argv)
        return argv
    container = runtime
    factory = getattr(runtime, "_container_runtime_for", None)
    if callable(factory):
        container = factory(config)
    sif = container.resolve_sif(config)
    sif_path = sif if sif is not None else Path(config.image or "<unresolved>.sif")
    return container.build_run_argv(config, state_dir=state_dir, sif_path=sif_path)


def _stage(errors: list[str], fn, *args) -> Any:
    """Run one render stage; a refusal is recorded, not raised, so later
    stages still render (a start would stop at the FIRST refusal — the
    surface keeps every refusal visible instead of hiding what follows)."""
    from .._state._meta.secrets import _redact_env_entry

    try:
        return fn(*args)
    except Exception as exc:  # stx-allow: fallback (a refusal IS the surface)
        errors.append(f"{type(exc).__name__}: {_redact_env_entry(str(exc))}")
        return None


def _env_file_names(path: Path) -> list[str]:
    try:
        text = path.read_text(errors="replace")
    except OSError:
        return []
    return sorted({m.group(1) for m in map(_ENV_LINE.match, text.splitlines()) if m})


def _argv_env_names(argv: list[str], root: Path) -> tuple[str, ...]:
    names: set[str] = set()
    for i, arg in enumerate(argv[:-1]):
        if arg == "--env":
            names.add(argv[i + 1].split("=", 1)[0])
        elif arg == "--env-file":
            rel = _rel(Path(argv[i + 1]), root)
            names.update(
                f"{n} (env-file {rel})" for n in _env_file_names(Path(argv[i + 1]))
            )
    return tuple(sorted(names))


def _rel(path: Path, root: Path) -> str:
    try:
        return str(path.relative_to(root))
    except ValueError:
        return str(path)


def _home_lines(root: Path) -> tuple[str, ...]:
    out: list[str] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames.sort()
        base = Path(dirpath)
        for name in sorted(filenames) + [
            d for d in dirnames if (base / d).is_symlink()
        ]:
            path = base / name
            rel = _rel(path, root)
            if path.is_symlink():
                out.append(f"{rel} -> {_rel(Path(os.readlink(path)), root)}")
                continue
            if _SECRETISH.search(rel):
                keys = ",".join(_env_file_names(path)) if "env" in name else ""
                out.append(f"{rel} [secret-shaped; keys: {keys or '-'}]")
                continue
            try:
                body = path.read_bytes().replace(str(root).encode(), b"<root>")
                digest = hashlib.sha256(body).hexdigest()[:16]
            except OSError:
                digest = "unreadable"
            out.append(f"{rel} sha256:{digest}")
    return tuple(out)


def render_surface(config: Any, *, root: Path) -> LaunchSurface:
    """Render ``config``'s launch surface into a FRESH ``root`` (wiped first).

    Every stage runs even after an earlier refusal (see :func:`_stage`), so
    the surface shows each refusal AND what the later stages would render.
    """
    from .._lifecycle._runtime_select import _get_runtime
    from .._state._meta.secrets import _redact_env_entry

    root = Path(root)
    if root.exists():
        shutil.rmtree(root)
    root.mkdir(parents=True)
    work = _relocate(config, root)
    state_dir = root / "state"
    state_dir.mkdir()
    errors: list[str] = []
    argv: list[str] = []
    with _runtime_dir(root / "runtime"):
        runtime = _stage(errors, _get_runtime, work)
        if runtime is not None:
            tui = callable(getattr(runtime, "_default_argv", None))
            _stage(errors, _materialize, work, state_dir, tui)
            argv = _stage(errors, _launch_argv, runtime, work, state_dir) or []
    return LaunchSurface(
        argv=tuple(_rel_arg(_redact_env_entry(a), root) for a in argv),
        env_names=_argv_env_names(argv, root),
        home=_home_lines(root),
        error=_rel_arg(" | ".join(errors), root) or None,
    )


def _rel_arg(arg: str, root: Path) -> str:
    """Show render-root paths as ``<root>/…`` so reports stay readable."""
    return arg.replace(str(root), "<root>")
