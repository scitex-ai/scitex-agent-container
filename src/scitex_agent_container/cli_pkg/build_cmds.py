"""Build and validation commands: check and validate."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import click
from scitex_dev.status import Verdict

from ..config import load_config, resolve_config, validate_config
from ._agents_check_result import CheckFeedback, CheckResult, Severity
from ._helpers import _json_flag, agent_name_complete, console


def _finding(
    code: str,
    subject: str,
    verdict: Verdict,
    severity: Severity,
    message: str,
    *,
    path: str | None,
    observed: str | None = None,
    expected: str | None = None,
    remedy: str | None = None,
) -> CheckFeedback:
    return CheckFeedback(
        code=code,
        path=path,
        subject=subject,
        verdict=verdict,
        severity=severity,
        message=message,
        observed=observed,
        expected=expected,
        remedy=remedy,
    )


def collect_check_result(name_or_path: str) -> CheckResult:
    """Run the preflight and return its sole machine/human result model."""
    checks: list[CheckFeedback] = []
    try:
        config_path = Path(resolve_config(name_or_path)).resolve()
    except Exception:  # stx-allow: fallback (resolution failures are findings)
        checks.append(
            _finding(
                "spec.resolve",
                name_or_path,
                Verdict.NOT_OK,
                Severity.ERROR,
                "Error resolving agent spec.",
                path=None,
                observed="No readable spec could be resolved from the supplied subject.",
                expected="An agent name or an existing spec.yaml path.",
                remedy="Pass a defined agent name or the path to its spec.yaml.",
            )
        )
        return CheckResult.from_checks(subject=name_or_path, path=None, checks=checks)

    path_text = str(config_path)
    checks.append(
        _finding(
            "spec.resolve",
            name_or_path,
            Verdict.OK,
            Severity.INFO,
            "Agent spec resolved.",
            path=path_text,
            observed=path_text,
            expected="An existing spec.yaml path.",
        )
    )

    errors = validate_config(config_path)
    if errors:
        checks.append(
            _finding(
                "spec.validate",
                "spec.yaml",
                Verdict.NOT_OK,
                Severity.ERROR,
                "Config validation failed.",
                path=path_text,
                observed="\n".join(str(error) for error in errors),
                expected="A valid, explicit scitex-agent-container/v3 spec.",
                remedy="Fix the listed schema errors and run this check again.",
            )
        )
        return CheckResult.from_checks(
            subject=name_or_path, path=path_text, checks=checks
        )

    checks.append(
        _finding(
            "spec.validate",
            "spec.yaml",
            Verdict.OK,
            Severity.INFO,
            "Config validation passed.",
            path=path_text,
            observed="No schema errors.",
            expected="A valid, explicit scitex-agent-container/v3 spec.",
        )
    )

    try:
        # Style advisories are collected below so both renderers consume the
        # same result. load_config's default avoids printing that advice.
        config = load_config(config_path, advise=False)
    except Exception as exc:  # stx-allow: fallback (load failure is a finding)
        checks.append(
            _finding(
                "spec.load",
                "spec.yaml",
                Verdict.NOT_OK,
                Severity.ERROR,
                "Error loading validated config.",
                path=path_text,
                observed=type(exc).__name__,
                expected="The validated spec can be loaded as an AgentConfig.",
                remedy="Inspect the spec and report this loader error if validation passes.",
            )
        )
        return CheckResult.from_checks(
            subject=name_or_path, path=path_text, checks=checks
        )

    checks.append(
        _finding(
            "spec.load",
            config.name,
            Verdict.OK,
            Severity.INFO,
            "Config loaded.",
            path=path_text,
            observed=f"agent={config.name}; runtime={config.runtime or 'apptainer'}",
            expected="A loadable AgentConfig.",
        )
    )

    checks.extend(_check_startup_prompts(config, path_text))
    checks.append(_check_backend(path_text))
    checks.append(_check_python(path_text))
    checks.extend(_check_bind_targets(config, path_text))
    checks.append(_check_raw_args(config, path_text))
    checks.append(_check_host_route(config, path_text))
    checks.append(_check_provider_auth(config, path_text))
    return CheckResult.from_checks(subject=config.name, path=path_text, checks=checks)


@click.command()
@click.argument("name_or_path", type=str, shell_complete=agent_name_complete)
@click.pass_context
def check(ctx: click.Context, name_or_path: str) -> None:
    """Run preflight checks for an agent deployment.

    Validates the YAML spec, then probes runtime dependencies, routing, and
    provider authentication. ``sac --json agents check NAME`` emits one stable
    JSON object; human output is rendered from that same object.
    """
    result = collect_check_result(name_or_path)
    if _json_flag(ctx, False):
        click.echo(json.dumps(result.to_dict(), ensure_ascii=False, indent=2))
    else:
        _render_check_result(result)
    if not result.ok:
        ctx.exit(1)


def _render_check_result(result: CheckResult) -> None:
    """Render the structured result without recomputing any verdict."""
    console.print(f"[blue]Checking {result.subject}...[/blue]")
    colours = {
        Severity.INFO: "green",
        Severity.WARNING: "yellow",
        Severity.ERROR: "red",
    }
    labels = {
        Verdict.OK: "OK",
        Verdict.NOT_OK: "WARN",
        Verdict.UNKNOWN: "UNKNOWN",
    }
    for item in result.checks:
        label = "FAIL" if item.blocks_deploy else labels[item.verdict]
        colour = colours[item.severity]
        console.print(
            f"  {item.subject + ':':30s} [{colour}]{label} ({item.message})[/{colour}]"
        )
        if item.observed and item.verdict is not Verdict.OK:
            console.print(f"    Observed: {item.observed}")
        if item.expected and item.verdict is not Verdict.OK:
            console.print(f"    Expected: {item.expected}")
        if item.remedy:
            console.print(f"    Remedy: {item.remedy}")
    if result.ok:
        suffix = " (with warnings)." if result.status == "warning" else "."
        console.print(f"[green]Ready to deploy{suffix}[/green]")
    else:
        console.print(
            "[red]Preflight checks failed. Fix the issues above before deploying.[/red]"
        )


def _check_startup_prompts(config, path: str) -> list[CheckFeedback]:
    """Collect authoring advice without ever copying prompt text."""
    from ..config import _STARTUP_PROMPT_WARN_CHARS, _STARTUP_PROMPT_WARN_LINES

    findings: list[CheckFeedback] = []
    for index, prompt in enumerate(getattr(config, "startup_prompts", ()) or ()):
        text = str(prompt)
        chars = len(text)
        lines = text.count("\n") + 1
        if chars <= _STARTUP_PROMPT_WARN_CHARS and lines <= _STARTUP_PROMPT_WARN_LINES:
            continue
        findings.append(
            _finding(
                "spec.startup-prompt.length",
                f"startup_prompts[{index}]",
                Verdict.NOT_OK,
                Severity.WARNING,
                "Startup prompt is long for a per-boot instruction.",
                path=path,
                observed=f"{chars} characters; {lines} lines",
                expected=(
                    f"At most {_STARTUP_PROMPT_WARN_CHARS} characters and "
                    f"{_STARTUP_PROMPT_WARN_LINES} lines."
                ),
                remedy="Move durable role and workflow prose to CLAUDE.md or skills.",
            )
        )
    return findings


def _check_backend(path: str) -> CheckFeedback:
    binary = shutil.which("apptainer")
    if binary:
        return _finding(
            "runtime.backend",
            "apptainer",
            Verdict.OK,
            Severity.INFO,
            "Container backend is available.",
            path=path,
            observed=binary,
            expected="apptainer available on PATH.",
        )
    return _finding(
        "runtime.backend",
        "apptainer",
        Verdict.NOT_OK,
        Severity.ERROR,
        "apptainer not found.",
        path=path,
        observed="No apptainer executable on PATH.",
        expected="apptainer available on PATH.",
        remedy="Install Apptainer or add its executable directory to PATH.",
    )


def _check_python(path: str) -> CheckFeedback:
    try:
        proc = subprocess.run(
            ["python3", "--version"], capture_output=True, text=True, timeout=5
        )
    except FileNotFoundError:
        return _finding(
            "runtime.python",
            "python",
            Verdict.NOT_OK,
            Severity.ERROR,
            "python3 not found.",
            path=path,
            observed="No python3 executable on PATH.",
            expected="python3 --version exits with code 0.",
            remedy="Install Python 3 or add its executable directory to PATH.",
        )
    except subprocess.TimeoutExpired:
        return _finding(
            "runtime.python",
            "python",
            Verdict.UNKNOWN,
            Severity.ERROR,
            "Python probe timed out.",
            path=path,
            observed="python3 --version did not finish within 5 seconds.",
            expected="python3 --version exits with code 0 within 5 seconds.",
            remedy="Run python3 --version directly and inspect why it blocks.",
        )
    if proc.returncode == 0:
        return _finding(
            "runtime.python",
            "python",
            Verdict.OK,
            Severity.INFO,
            "Python is available.",
            path=path,
            observed=proc.stdout.strip() or "python3 exited 0",
            expected="python3 --version exits with code 0.",
        )
    return _finding(
        "runtime.python",
        "python",
        Verdict.NOT_OK,
        Severity.ERROR,
        "python3 --version failed.",
        path=path,
        observed=f"process exit code {proc.returncode}",
        expected="process exit code 0.",
        remedy="Run python3 --version directly and repair the Python installation.",
    )


_HOST_MIRRORING_TARGET_PREFIXES = ("/home/", "/Users/", "/root/")


def _check_bind_targets(config, path: str) -> list[CheckFeedback]:
    ap = getattr(config, "apptainer", None)
    binds = list(getattr(ap, "binds", None) or []) if ap is not None else []
    findings: list[CheckFeedback] = []
    for bind in binds:
        target = _bind_target(str(bind))
        if target and any(
            target.startswith(prefix) for prefix in _HOST_MIRRORING_TARGET_PREFIXES
        ):
            findings.append(
                _finding(
                    "isolation.bind-target",
                    target,
                    Verdict.NOT_OK,
                    Severity.WARNING,
                    f"Bind target {target} mirrors a host path.",
                    path=path,
                    observed=target,
                    expected="A container-canonical target under /srv, /work, /opt, or /data.",
                    remedy="Change the container-side bind target unless host mirroring is intentional.",
                )
            )
    if not findings:
        findings.append(
            _finding(
                "isolation.bind-target",
                "bind targets",
                Verdict.OK,
                Severity.INFO,
                "Bind targets follow the container-canonical convention.",
                path=path,
                observed=f"{len(binds)} bind(s) checked",
                expected="Targets under /srv, /work, /opt, or /data.",
            )
        )
    return findings


def _check_raw_args(config, path: str) -> CheckFeedback:
    from ..runtimes._apptainer_argv_guard import find_missing_value

    ap = getattr(config, "apptainer", None)
    raw = list(getattr(ap, "raw_args", None) or []) if ap is not None else []
    malformed = find_missing_value(raw)
    if malformed is None:
        detail = "none declared" if not raw else f"{len(raw)} token(s) checked"
        return _finding(
            "runtime.raw-args",
            "raw_args",
            Verdict.OK,
            Severity.INFO,
            "Apptainer raw_args are well formed.",
            path=path,
            observed=detail,
            expected="Every value-taking flag is followed by a value.",
        )
    index, flag, following = malformed
    next_description = "end of raw_args" if following is None else "another option"
    return _finding(
        "runtime.raw-args",
        "raw_args",
        Verdict.NOT_OK,
        Severity.ERROR,
        f"Apptainer flag {flag} is missing its required value.",
        path=path,
        observed=f"index {index}; followed by {next_description}",
        expected="Every value-taking flag is followed by a value.",
        remedy=f"Give {flag} its value or delete the orphan flag.",
    )


def _check_host_route(config, path: str) -> CheckFeedback:
    from .lifecycle._host_chain import UNROUTABLE, chain_hosts

    spec_host = getattr(getattr(config, "hosts_spec", None), "host", None)
    if not chain_hosts(spec_host):
        return _finding(
            "routing.host",
            "host",
            Verdict.OK,
            Severity.INFO,
            "Agent is unpinned and starts on this machine.",
            path=path,
            observed="No host pin.",
            expected="No pin, this machine, or a registered peer.",
        )
    try:
        from .._state.host_config import load as _load_host_config
        from ..config._host import resolve_hostname
        from .lifecycle._host_identity import _local_host_names

        peers = _load_host_config().peers
        current_host = resolve_hostname()
        local_names = _local_host_names(current_host)
    except Exception as exc:
        return _finding(
            "routing.host",
            str(spec_host),
            Verdict.UNKNOWN,
            Severity.WARNING,
            "Host pin could not be verified.",
            path=path,
            observed=f"Host registry unavailable ({type(exc).__name__}).",
            expected="The pin names this machine or a registered peer.",
            remedy="Inspect `sac host list` and the local hostname, then rerun the check.",
        )

    from .lifecycle._host_routing import resolve_spec_host_route

    route = resolve_spec_host_route(
        spec_host, current_host, peers, local_names=local_names
    )
    if route.kind == UNROUTABLE and not peers:
        return _finding(
            "routing.host",
            str(spec_host),
            Verdict.UNKNOWN,
            Severity.WARNING,
            "Host pin cannot be judged without registered peers.",
            path=path,
            observed="The pin is non-local and the peer table is empty.",
            expected="The pin names this machine or a registered peer.",
            remedy="Register the fleet peers or verify this pin on the target host.",
        )
    if route.kind == UNROUTABLE:
        return _finding(
            "routing.host",
            str(spec_host),
            Verdict.NOT_OK,
            Severity.ERROR,
            "Host pin is not routable.",
            path=path,
            observed=f"{spec_host} is neither local nor a registered peer.",
            expected="The pin names this machine or a registered peer.",
            remedy="Correct spec.host or register the intended peer.",
        )
    where = "this machine" if route.kind == "local" else f"peer {route.peer}"
    return _finding(
        "routing.host",
        str(spec_host),
        Verdict.OK,
        Severity.INFO,
        "Host pin is routable.",
        path=path,
        observed=f"{route.host} resolves to {where}.",
        expected="The pin names this machine or a registered peer.",
    )


def _check_provider_auth(config, path: str) -> CheckFeedback:
    from ._provider_auth_probe import (
        INACTIVE,
        OK,
        REJECTED,
        UNRESOLVED,
        probe_provider_auth,
    )

    verdict = probe_provider_auth(config)
    provider = getattr(getattr(config, "claude", None), "provider", None)
    env_name = str(getattr(provider, "auth_token_env", "") or "provider credential")
    if verdict.state == INACTIVE:
        return _finding(
            "provider.auth",
            "provider key",
            Verdict.OK,
            Severity.INFO,
            "No provider override is declared.",
            path=path,
            observed="Provider authentication probe is inactive.",
            expected="No override, or a working configured credential.",
        )
    if verdict.state == OK:
        return _finding(
            "provider.auth",
            env_name,
            Verdict.OK,
            Severity.INFO,
            "Provider credential was accepted.",
            path=path,
            observed=f"HTTP {verdict.status_code}; invalid control credential rejected.",
            expected="The configured credential is accepted and the control is rejected.",
        )
    if verdict.state in (REJECTED, UNRESOLVED):
        observed = (
            f"Provider returned HTTP {verdict.status_code}."
            if verdict.state == REJECTED
            else "The configured environment variable resolved to no credential."
        )
        return _finding(
            "provider.auth",
            env_name,
            Verdict.NOT_OK,
            Severity.ERROR,
            "Provider credential failed authentication.",
            path=path,
            observed=observed,
            expected="A non-empty credential accepted by the configured provider.",
            remedy=f"Set {env_name} to the credential used by this provider and rerun the check.",
        )
    # An unreachable or indiscriminate provider is absence of a reliable
    # verdict, not evidence against the key. Do not serialize URLs or keys.
    return _finding(
        "provider.auth",
        env_name,
        Verdict.UNKNOWN,
        Severity.WARNING,
        "Provider authentication could not be verified.",
        path=path,
        observed=f"probe state={verdict.state}",
        expected="The backend accepts the configured credential and rejects a control.",
        remedy="Check provider reachability and authentication enforcement, then rerun.",
    )


def _bind_target(bind: str) -> str:
    parts = bind.split(":")
    if len(parts) < 2:
        return ""
    if len(parts) >= 3 and parts[-1] in {"ro", "rw"}:
        return parts[-2]
    return parts[1]


@click.command()
@click.argument("name_or_path", type=str)
def validate(name_or_path: str) -> None:
    """Validate a YAML config file."""
    try:
        config_path = resolve_config(name_or_path)
    except Exception as exc:  # stx-allow: fallback (human-only legacy command)
        console.print(f"[red]Error: {exc}[/red]")
        raise click.exceptions.Exit(1) from None
    errors = validate_config(config_path)
    if not errors:
        console.print(f"[green]Config is valid: {config_path}[/green]")
        return
    console.print(f"[red]Config validation failed: {config_path}[/red]")
    for error in errors:
        console.print(f"  [red]- {error}[/red]")
    raise click.exceptions.Exit(1)


__all__ = ["check", "collect_check_result", "validate"]
