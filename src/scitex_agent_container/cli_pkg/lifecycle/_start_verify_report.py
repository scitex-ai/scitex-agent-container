#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Post-``agent_start`` reporting for ``sac agents start`` — the launch verdict.

Extracted from ``_start_single.py`` (512-line per-file cap) together with
the NEW post-launch verification it now performs (v4 migration step 1):
the green "started" line only prints once :mod:`_lifecycle._launch_verify`
observed evidence the agent actually came up — never SUCC on an
unverified launch. On a verified failure the boot-log tail (the real
error text — e.g. the ``[Errno 98]`` line) is printed to the caller's
terminal along with the FILE it was read from; on an in-window
non-answer the wording says "could not verify" and names where to look.
Both of those return ``False`` so the caller exits non-zero.

The pre-existing report shapes (``--json`` statuses ``dry_run_ok`` /
``already_running`` / ``started``, the human dry-run and started lines,
the manual-TUI-acceptance warning) are preserved; ``--json`` gains a
``verify`` sub-object on real launches and the honest statuses
``start-failed`` / ``start-unverified`` when the launch did not verify.
Error paths go through ``system_msg`` — sac's scitex-logging surface —
so ``ERRO`` marks the verified failure and ``FAIL`` the failed check.
"""

from __future__ import annotations

from typing import Any, Callable

import click

from ..._lifecycle._launch_verify import (
    SKIPPED,
    UNVERIFIED,
    VERIFIED_FAILED,
    VERIFIED_UP,
    LaunchVerdict,
    verify_launch,
)
from .._helpers import console, system_msg

#: verdict status -> the ``--json`` ``status`` field. ``skipped`` maps to
#: the historical ``started`` (nothing contradicts it and scripts keyed
#: on ``started`` must not break when verification does not apply); the
#: two negative verdicts get their own honest statuses.
_JSON_STATUS = {
    VERIFIED_UP: "started",
    SKIPPED: "started",
    VERIFIED_FAILED: "start-failed",
    UNVERIFIED: "start-unverified",
}


def _indent(text: str, prefix: str = "      ") -> str:
    """Indent a multi-line body so it reads as one sub-section (mirrors
    ``_start_failure_diag._indent_block``)."""
    return "\n".join(f"{prefix}{line}" for line in (text.splitlines() or [""]))


def _echo_boot_log_tail(verdict: LaunchVerdict, *, style: str) -> None:
    """Headline via ``system_msg`` (leveled), tail body VERBATIM.

    The body deliberately bypasses ``system_msg``: the console helper
    strips ``[tag]``-shaped rich markup before logging, and the exact
    text this report exists to surface is bracket-shaped —
    ``[Errno 98]`` matches the tag regex and would be silently eaten
    (measured: the pre-fix render showed ``OSError:  error while
    attempting to bind``). ``click.echo(..., err=True)`` keeps it
    byte-exact and on stderr, where the listen's detached-launch
    adoption also captures it into the STARTUP_FAILED marker.
    """
    system_msg(f"boot log tail ({verdict.log_path}):", style=style)
    click.echo(_indent(verdict.log_tail), err=True)


def _emit_report_json(
    emit: Callable[[dict], None],
    config: Any,
    *,
    status: str,
    dry_run: bool,
    host: str,
    host_workdir: str,
    container_workdir: str,
    verify: dict | None,
) -> None:
    """The structured ``--json`` report — same fields as before, plus
    ``verify`` when a launch verdict exists."""
    from ..._state.port_allocator import get_port as _get_port
    from ..._state.state_db import now_iso as _now_iso

    _raw_port = getattr(getattr(config, "a2a", None), "port", None)
    _resolved_port: int | None = (
        None if (dry_run or _raw_port is None) else _get_port(config.name)
    )
    payload = {
        "name": config.name,
        "status": status,
        "host": host,
        "host_workdir": host_workdir,
        "container_workdir": container_workdir,
        "dry_run": dry_run,
        "a2a_port": _resolved_port,
        "started_at": None if dry_run else _now_iso(),
    }
    if verify is not None:
        payload["verify"] = verify
    emit(payload)


def _warn_manual_accept(config: Any, host: str, *, dry_run: bool) -> None:
    """The pre-existing manual-TUI-acceptance warning, verbatim."""
    if (
        not dry_run
        and not config.claude.auto_accept
        and any(
            df in f
            for f in config.claude.flags
            for df in (
                "--dangerously-skip-permissions",
                "--dangerously-load-development-channels",
            )
        )
    ):
        console.print(
            f"[yellow]auto_accept: false — manual TUI acceptance required on {host}[/yellow]"
        )


def _skip_reason(*, foreground: bool, one_shot: bool) -> str | None:
    """Why verification does not apply to this launch, or ``None``.

    In-SIF: ``agent_start`` brokered the spawn to the host listen, whose
    own ``sac agents start`` subprocess runs this same verification where
    the evidence lives (a container-side probe of host state dirs could
    only ever read an empty directory — the ``_restart_verify`` lesson).
    """
    if foreground:
        return (
            "foreground run attached this terminal to the agent directly "
            "(nothing detached to verify)"
        )
    if one_shot:
        return "--one-shot runs its turn to completion and exits by design"
    from ..._lifecycle._in_sif_broker import is_in_sif

    if is_in_sif():
        return (
            "start was brokered to the host `sac listen` — the evidence "
            "lives on the host, whose own start subprocess verifies there"
        )
    return None


def report_start_result(
    config: Any,
    *,
    noop: bool,
    dry_run: bool,
    foreground: bool,
    one_shot: bool,
    as_json: bool,
    emit: Callable[[dict], None],
    launched_at: float,
    host: str,
    host_workdir: str,
    container_workdir: str,
    location: str,
    verify_fn: Callable[..., LaunchVerdict] | None = None,
) -> bool:
    """Verify the launch (bounded) and report the outcome. False = exit 1.

    ``verify_fn`` is the injection seam for the verdict producer (a real
    callable with :func:`verify_launch`'s keyword shape; production is
    ``verify_launch`` itself).
    """
    # No-op / dry-run: nothing was launched, so there is nothing to
    # verify — report exactly as before.
    if dry_run or noop:
        if as_json:
            _emit_report_json(
                emit,
                config,
                status="dry_run_ok" if dry_run else "already_running",
                dry_run=dry_run,
                host=host,
                host_workdir=host_workdir,
                container_workdir=container_workdir,
                verify=None,
            )
        else:
            if not noop:
                system_msg(
                    f"[bold]{config.name}[/bold] dry-run prepared the workspace for",
                    style="green",
                )
            _warn_manual_accept(config, host, dry_run=dry_run)
        return True

    reason = _skip_reason(foreground=foreground, one_shot=one_shot)
    if reason is not None:
        verdict = LaunchVerdict(SKIPPED, reason, None, "", 0.0, None)
    else:
        verdict = (verify_fn or verify_launch)(config, launched_at=launched_at)

    if as_json:
        _emit_report_json(
            emit,
            config,
            status=_JSON_STATUS[verdict.status],
            dry_run=False,
            host=host,
            host_workdir=host_workdir,
            container_workdir=container_workdir,
            verify=verdict.as_dict(),
        )
        return verdict.ok

    if foreground:
        # Agent stdout often lacks a trailing newline.
        click.echo("")
    if verdict.status == VERIFIED_UP:
        system_msg(
            f"[bold]{config.name}[/bold] started [dim]({location})[/dim] — "
            f"{verdict.evidence}",
            style="green",
        )
    elif verdict.status == SKIPPED:
        # Launched, but unverifiable BY DESIGN here — say so instead of
        # asserting an accomplishment nothing observed.
        system_msg(
            f"[bold]{config.name}[/bold] launched [dim]({location})[/dim] — "
            f"launch verification skipped: {verdict.evidence}",
            style="info",
        )
    elif verdict.status == VERIFIED_FAILED:
        system_msg(
            f"{config.name}: launch did NOT verify — {verdict.evidence}",
            style="error",
        )
        if verdict.log_tail:
            _echo_boot_log_tail(verdict, style="error")
        elif verdict.log_path:
            system_msg(
                f"boot log ({verdict.log_path}) exists but is empty — the "
                "runtime died before writing anything",
                style="error",
            )
        else:
            system_msg(
                "no boot log was written — the runtime never launched far "
                "enough to leave one",
                style="error",
            )
    else:  # UNVERIFIED — worded as "could not verify", never "failed".
        system_msg(
            f"{config.name}: launch NOT verified — {verdict.evidence}",
            style="fail",
        )
        looked_at = " and ".join(
            p for p in (verdict.heartbeat_path, verdict.log_path) if p
        )
        if looked_at:
            system_msg(
                f"cannot tell whether the agent is up; go deeper via: {looked_at}",
                style="fail",
            )
        if verdict.log_tail:
            _echo_boot_log_tail(verdict, style="fail")
    _warn_manual_accept(config, host, dry_run=False)
    return verdict.ok


__all__ = ["report_start_result"]
