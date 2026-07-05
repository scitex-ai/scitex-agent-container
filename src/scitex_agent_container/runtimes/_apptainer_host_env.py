"""Host-process env additions for the ``apptainer`` launch.

Some operators install dev CLIs (notably ``rtk`` — the Rust Token
Killer proxy) into the HOST ``~/.cargo/bin`` rather than into the SIF.
Apptainer binds the host home into the container, so the *binary* is
visible, but the SIF ships its OWN cargo prefix at ``/opt/cargo/bin``
(set in ``containers/apptainer-base.def``) and the host
``~/.cargo/bin`` is NOT on the container ``$PATH`` — so ``rtk`` inside
an agent container fails with ``/bin/sh: 1: rtk: not found``.

Fix mechanism — ``APPTAINERENV_APPEND_PATH``: apptainer reads this var
from ITS OWN process environment (the sac process that invokes
``apptainer exec``) and APPENDS the value to the container ``$PATH`` at
launch. This works even under ``--containall`` because it is an
apptainer runtime directive, NOT a container env var — so it does NOT
go through ``--env`` (a ``--env PATH=...`` would CLOBBER PATH). APPEND
(not prepend) is deliberate: the SIF's own tools (``rg`` / ``eza`` /
... in ``/opt/cargo/bin``, earlier in PATH) keep winning; only
host-only tools like ``rtk`` newly resolve — no shadowing.

This module is a pure, fully-testable helper: it computes the env
ADDITIONS (never mutates ``os.environ`` itself) so the exec sites
(:mod:`_apptainer_runtime` SDK-Popen path and :mod:`tui_session` tmux
path) can merge them into the env they hand to the ``apptainer``
process.

It also carries the mirror-image SUBTRACTION helper,
:func:`scrub_legacy_env` — apptainer passes the FULL ambient
environment of whatever host process invokes it into the container
(the exact same passthrough class that motivates
``APPTAINER_APPEND_PATH`` above), so a stale legacy env var left over
in the launching shell (e.g. ``SCITEX_TODO_AGENT`` from before the
scitex-todo 0.7.30 rename) leaks straight through regardless of what
sac's own ``--env=`` list or to_home materialization says (INCIDENT
2026-07-05). Both exec sites must pop the same denylist from the env
dict they hand to the ``apptainer``/``tmux`` subprocess before exec.
"""

from __future__ import annotations

from pathlib import Path
from typing import Mapping

from ._to_home_text import LEGACY_RENAMED_ENV_VARS

#: Denylist of legacy-renamed env var names that must never survive into
#: an apptainer launch environment, however they got into the calling
#: process's ambient env. Single source of truth shared with the
#: materialized-file guard in :mod:`_to_home_text` — a future rename only
#: needs one new entry in :data:`_to_home_text.LEGACY_RENAMED_ENV_VARS`,
#: not a fresh audit of every exec site.
LEGACY_ENV_DENYLIST = LEGACY_RENAMED_ENV_VARS

__all__ = [
    "APPTAINER_APPEND_PATH_ENV",
    "LEGACY_ENV_DENYLIST",
    "host_cargo_bin_append_env",
    "scrub_legacy_env",
]

#: The apptainer runtime directive var. Apptainer strips the
#: ``APPTAINERENV_`` prefix and appends the value to the container PATH.
APPTAINER_APPEND_PATH_ENV = "APPTAINERENV_APPEND_PATH"

#: Host-side cargo bin. ``~`` resolves against the launching operator's
#: ``$HOME`` at call time — never a hard-coded username.
_HOST_CARGO_BIN = "~/.cargo/bin"


def host_cargo_bin_append_env(base_env: Mapping[str, str]) -> dict[str, str]:
    """Return env ADDITIONS that append host ``~/.cargo/bin`` to the
    container PATH via ``APPTAINERENV_APPEND_PATH``.

    Pure — does NOT read or mutate ``os.environ``; the caller passes
    the base environment (usually ``os.environ``) and merges the
    returned dict into the env it hands to the ``apptainer`` subprocess.

    Behaviour:

    * Resolve ``~/.cargo/bin`` via ``Path("~/.cargo/bin").expanduser()``
      (host-side, at call time — no hard-coded user).
    * **Skip-if-missing**: if that directory does not exist, return an
      EMPTY dict (no key) — matching sac's other host-path conventions
      (a fresh box without cargo gets no directive, no crash).
    * If ``APPTAINERENV_APPEND_PATH`` is ALREADY set in ``base_env``,
      APPEND the cargo bin after a ``:`` separator rather than
      clobbering the pre-existing value (an empty pre-existing value is
      treated as unset so we never emit a leading ``:``).
    """
    cargo_bin = Path(_HOST_CARGO_BIN).expanduser()
    if not cargo_bin.is_dir():
        # Skip-if-missing: no cargo bin on this host → no directive.
        return {}

    cargo_bin_str = str(cargo_bin)
    existing = base_env.get(APPTAINER_APPEND_PATH_ENV, "")
    if existing:
        value = f"{existing}:{cargo_bin_str}"
    else:
        value = cargo_bin_str
    return {APPTAINER_APPEND_PATH_ENV: value}


def scrub_legacy_env(env: Mapping[str, str]) -> dict[str, str]:
    """Return a copy of ``env`` with :data:`LEGACY_ENV_DENYLIST` removed.

    Pure — does NOT mutate ``env`` (mirrors :func:`host_cargo_bin_append_env`'s
    no-mutation contract). Call this at the SAME point each exec site builds
    the environment dict it hands to the ``apptainer``/``tmux`` subprocess,
    so a legacy var present in the calling shell's ambient ``os.environ``
    (however it got there) cannot physically survive into the container —
    regardless of what sac's own ``--env=`` list or to_home materialization
    says (INCIDENT 2026-07-05: apptainer's default full-ambient-env
    passthrough leaked a stale ``SCITEX_TODO_AGENT`` straight through,
    tripping scitex-todo's old-var-present hard-reject).

    A future scitex-todo (or other) env-var rename only needs one new
    entry added to :data:`_to_home_text.LEGACY_RENAMED_ENV_VARS` — this
    function and every exec site that calls it pick the new name up for
    free, no per-site audit required.
    """
    return {k: v for k, v in env.items() if k not in LEGACY_ENV_DENYLIST}
