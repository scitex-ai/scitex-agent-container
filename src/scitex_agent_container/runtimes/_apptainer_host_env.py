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
"""

from __future__ import annotations

from pathlib import Path
from typing import Mapping

__all__ = [
    "APPTAINER_APPEND_PATH_ENV",
    "host_cargo_bin_append_env",
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
