"""Tmux session management utilities."""

from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path
from typing import Callable

from ._host_cwd import resolve_host_cwd

# Inter-keystroke delay inside send_keys() and settle delay before
# Enter inside send_text_and_submit(). These defeat the race where
# Claude Code's TUI (Ink / React) hasn't finished rendering the
# previous key before the next one arrives — the symptom the user
# reported as "text lands but the Enter submit fails".
#
# Both values are overridable per-host via env vars so a slow HPC
# login shell (Spartan) can raise them without a code change.
_DEFAULT_INTER_KEY_DELAY_S = float(os.environ.get("SAC_KEY_DELAY_S", "0.1"))
_DEFAULT_SUBMIT_SETTLE_S = float(os.environ.get("SAC_SUBMIT_SETTLE_S", "0.3"))

# Structural fix for the Ink-drop race (lead a2a
# ``910ff436642948eb85f8b3100204ed9b``, 2026-06-14): the interactive
# claude TUI occasionally drops a keystroke that arrives mid-render
# (the React/Ink renderer eats the input event before binding the
# next listener). The cure is observation-based — wait for the
# input-ready marker before sending, then verify the sent text
# echoed back in the pane before committing Enter, then retry the
# whole text send if the echo never appeared.
#
# Default marker: the claude TUI prints ``? for shortcuts`` in its
# footer when the input field is bound and accepting keystrokes.
# Overridable per call so non-claude callers can pass a different
# signal.
_DEFAULT_INPUT_READY_MARKER = "? for shortcuts"


class TuiInputNotReadyError(RuntimeError):
    """Raised when the TUI input-ready marker never appeared.

    The wait_for_input_ready primitive raises this instead of timing
    out silently so the caller reports "TUI never mounted its input
    field" rather than a generic timeout — the operator can then tell
    at a glance whether auth failed (login picker stuck) vs. render
    hung (Ink wedged).
    """


class TuiKeystrokeDropError(RuntimeError):
    """Raised when send_text_and_submit_verified exhausted its retries
    without observing the sent text echo in the pane.

    The Ink renderer dropped every send. Fail loud rather than silently
    submitting an empty Enter (which the TUI would treat as "no input"
    and the operator would see as "agent ignored the prompt").
    """


class TmuxManager:
    """Helpers for tmux session lifecycle."""

    @staticmethod
    def exists(session_name: str) -> bool:
        """Check if a tmux session exists."""
        result = subprocess.run(
            ["tmux", "has-session", "-t", session_name],
            capture_output=True,
            text=True,
        )
        return result.returncode == 0

    @staticmethod
    def start(
        session_name: str,
        command: str,
        workdir: str,
        env_exports: str = "",
        venv: str = "",
        session_env: dict[str, str] | None = None,
    ) -> bool:
        """Launch a command inside a new detached tmux session.

        Args:
            session_name: Name for the tmux session.
            command: Shell command to execute.
            workdir: Working directory for the command.
            env_exports: Newline-separated export statements to prepend
                to the shell script (legacy belt-and-suspenders path —
                see :param:`session_env` for the structural fix).
            venv: Path to virtualenv to activate before running command.
            session_env: Mapping of env vars passed via ``tmux
                new-session -e KEY=VAL`` so the values land on the
                session itself and propagate to every pane / child
                process IRRESPECTIVE of whether a login shell init
                file would overwrite a same-named export inside the
                script. This is the operator-mandated path for HOME +
                CLAUDE_CONFIG_DIR on the TUI runtime (lead a2a
                ``8f910ea7e78e4e0b959ce087376e542b``, 2026-06-14:
                live agents launched without HOME pointing at the
                staged $STATE/home dropped to interactive OAuth login
                because the inner ``claude`` read the operator's
                real ``~/.claude/`` instead of the staged one).

        Returns:
            True if the tmux session was created successfully.
        """
        # Must precede the workdir-relative venv resolution and the shell
        # script's ``cd`` so all three agree on the effective pane cwd.
        workdir = resolve_host_cwd(workdir)

        venv_activate = ""
        if venv:
            venv_path = Path(venv)
            if not venv_path.is_absolute() and not venv.startswith("~"):
                # Workspace-relative: resolve under workdir on target host.
                activate = Path(workdir) / venv_path / "bin" / "activate"
            else:
                activate = venv_path.expanduser() / "bin" / "activate"
            venv_activate = f"source '{activate}' || exit 1\n"

        # Env snapshot file (lead a2a 4303f855, 2026-06-14): the
        # /proc/<pid>/environ + ps-walk verify in TuiSessionRuntime
        # could mis-attribute to another claude under another tmux
        # session. Writing the env to a known per-session file
        # IMMEDIATELY before ``exec command`` gives SAC a structural
        # source-of-truth for verification independent of any PID
        # hunting.
        env_snapshot_path = f"/tmp/sac-tui-env-{session_name}.txt"
        shell_script = (
            f"cd '{workdir}' || exit 1\n"
            f"{venv_activate}"
            f"{env_exports}\n"
            f"export CLAUDE_DISABLE_AUTO_UPDATE=1\n"
            f"env > '{env_snapshot_path}' 2>/dev/null || true\n"
            f"exec {command}\n"
        )

        # Apptainer launches (the TUI runtime wraps ``apptainer exec``
        # in this tmux PTY) need the host ``~/.cargo/bin`` appended to the
        # CONTAINER PATH so host-only cargo CLIs (e.g. rtk) resolve. The
        # mechanism is apptainer's ``APPTAINERENV_APPEND_PATH`` directive,
        # read from the apptainer HOST-process env — here, the tmux pane
        # process that ``exec apptainer``s. We route it through the SAME
        # ``-e KEY=VAL`` session-env channel below. Gated on an apptainer
        # command so non-apptainer tmux callers are untouched; the pure
        # helper skips-if-missing + appends-not-clobbers (see
        # ``runtimes._apptainer_host_env``). No-op when ``~/.cargo/bin``
        # is absent or the command is not an apptainer launch.
        effective_session_env = dict(session_env or {})
        if command.lstrip().startswith("apptainer "):
            from ...runtimes._apptainer_host_env import host_cargo_bin_append_env

            effective_session_env.update(host_cargo_bin_append_env(os.environ))

        # Build argv with optional ``-e KEY=VAL`` pairs first; bash
        # without ``-l`` so login init files cannot overwrite the
        # tmux-side env. The script's belt-and-suspenders exports
        # stay as a no-cost defense for any consumer that still
        # passes env_exports without session_env.
        argv: list[str] = ["tmux", "new-session", "-d", "-s", session_name]
        if effective_session_env:
            for key, value in effective_session_env.items():
                argv += ["-e", f"{key}={value}"]
        argv += ["bash", "-c", shell_script]
        subprocess.run(argv, check=False)

        time.sleep(2)
        return TmuxManager.exists(session_name)

    @staticmethod
    def stop(session_name: str) -> bool:
        """Terminate a tmux session.

        Returns True if the session was alive and has been terminated.
        """
        if not TmuxManager.exists(session_name):
            return False

        subprocess.run(
            ["tmux", "kill-session", "-t", session_name],
            capture_output=True,
            check=False,
        )

        time.sleep(0.5)
        return not TmuxManager.exists(session_name)

    @staticmethod
    def session_activity(session_name: str) -> int | None:
        """Unix-epoch stamp of the session's last pane activity, or
        ``None`` when absent. RESPONSIVENESS signal (advances only on
        pane I/O) — NOT liveness. See :func:`_tmux_probe.session_activity`.
        """
        from ._tmux_probe import session_activity as _sa

        return _sa(session_name)

    @staticmethod
    def pane_pid(session_name: str) -> int | None:
        """PID of the process in the session's active pane (identity
        liveness signal), or ``None`` when absent. See
        :func:`_tmux_probe.pane_pid`."""
        from ._tmux_probe import pane_pid as _pane_pid

        return _pane_pid(session_name)

    @staticmethod
    def pane_dead(session_name: str) -> bool | None:
        """Whether the active pane's process exited but the pane is
        retained, or ``None`` when absent. See
        :func:`_tmux_probe.pane_dead`."""
        from ._tmux_probe import pane_dead as _pane_dead

        return _pane_dead(session_name)

    @staticmethod
    def capture_content(session_name: str) -> str:
        """Capture current pane content via capture-pane."""
        result = subprocess.run(
            ["tmux", "capture-pane", "-t", session_name, "-p"],
            capture_output=True,
            text=True,
        )
        return result.stdout if result.returncode == 0 else ""

    @staticmethod
    def capture_logs(session_name: str, lines: int = 50) -> str:
        """Capture recent output from a tmux session."""
        result = subprocess.run(
            [
                "tmux",
                "capture-pane",
                "-t",
                session_name,
                "-p",
                "-S",
                str(-lines),
            ],
            capture_output=True,
            text=True,
        )
        if result.returncode == 0:
            return result.stdout
        return ""

    @staticmethod
    def send_keys(
        session_name: str,
        *keys: str,
        inter_key_delay_s: float | None = None,
        sleep_fn: Callable[[float], None] = time.sleep,
    ) -> None:
        """Send keys to a tmux session, one ``send-keys`` call per key.

        A small delay between keys defeats the race where the TUI has
        not yet rendered the previous keystroke before the next one
        arrives. Symptom without the delay: sending ``["2", "Enter"]``
        to a radio selector lands "2" but loses Enter when the
        selector's re-render is still in flight.

        Parameters
        ----------
        session_name:
            tmux target passed verbatim to ``-t``.
        keys:
            One or more keystrokes / tmux keyword names (``"Enter"``,
            ``"C-c"``, etc.) or raw text arguments.
        inter_key_delay_s:
            Seconds to sleep between keystrokes. ``None`` (default)
            uses ``_DEFAULT_INTER_KEY_DELAY_S`` which is read from
            ``SAC_KEY_DELAY_S`` at import time. No sleep after
            the last key.
        sleep_fn:
            Injected sleep — tests pass a stub to avoid real waits.
        """
        delay = (
            _DEFAULT_INTER_KEY_DELAY_S
            if inter_key_delay_s is None
            else inter_key_delay_s
        )
        key_list = list(keys)
        for i, key in enumerate(key_list):
            subprocess.run(
                ["tmux", "send-keys", "-t", session_name, key],
                check=False,
                capture_output=True,
            )
            if delay > 0 and i < len(key_list) - 1:
                sleep_fn(delay)

    @staticmethod
    def send_text_literal(
        session_name: str,
        text: str,
        *,
        runner: Callable[..., object] = subprocess.run,
    ) -> None:
        """Paste ``text`` into the pane LITERALLY (``send-keys -l``), NO submit.

        The ``-l`` (literal) flag is REQUIRED for the containerized Ink/React
        ``claude`` TUI: without it the TUI silently DROPS the keystrokes (the
        pane stays byte-identical, nothing lands). Source-verified recovery
        recipe: ``_skills/scitex-agent-container/45_agent-to-agent-recovery-
        tmux.md`` — ``-l`` for TEXT, then a SEPARATE named ``Enter`` (never
        ``-l``) to submit. Submit-free by design so the caller can send that
        ``Enter`` ONLY once the pane is idle (see
        :func:`runtimes._tui_compose.verify_submit_by_advancement`), never into
        the BUSY boot window where the Ink TUI eats it. ``runner`` is an
        injection seam (tests pass a recording callable — no mocks).
        """
        runner(
            ["tmux", "send-keys", "-t", session_name, "-l", text],
            check=False,
            capture_output=True,
        )

    @staticmethod
    def send_text_and_submit(
        session_name: str,
        text: str,
        *,
        settle_s: float | None = None,
        sleep_fn: Callable[[float], None] = time.sleep,
        runner: Callable[..., object] = subprocess.run,
    ) -> None:
        """Send message text LITERALLY, let the TUI settle, then press Enter.

        Preferred over ``send_keys(session, text + "\\r")``: tmux treats a
        trailing ``\\r`` as raw input and Claude Code's TUI drops it during an
        active re-render ("text arrived but submit never fired"). Sends the text
        first LITERALLY (``-l``, via :meth:`send_text_literal`, so the
        containerized Ink TUI does not silently drop it), settles, then issues a
        separate ``Enter`` keystroke (a named key, NEVER ``-l``).

        ``settle_s`` (``None`` → ``_DEFAULT_SUBMIT_SETTLE_S``,
        ``SAC_SUBMIT_SETTLE_S``-overridable) is the text→Enter gap; ``sleep_fn``
        / ``runner`` are injection seams for tests.
        """
        settle = _DEFAULT_SUBMIT_SETTLE_S if settle_s is None else settle_s
        TmuxManager.send_text_literal(session_name, text, runner=runner)
        if settle > 0:
            sleep_fn(settle)
        runner(
            ["tmux", "send-keys", "-t", session_name, "Enter"],
            check=False,
            capture_output=True,
        )

    @staticmethod
    def wait_for_input_ready(
        session_name: str,
        *,
        marker: str = _DEFAULT_INPUT_READY_MARKER,
        timeout_s: float = 30.0,
        poll_s: float = 0.25,
        sleep_fn: Callable[[float], None] = time.sleep,
        capture_fn: Callable[[str], str] | None = None,
    ) -> bool:
        """Block until ``marker`` appears in the session pane content.

        Structural fix for the Ink-drop race (lead a2a
        ``910ff436642948eb85f8b3100204ed9b``): the bundled claude TUI
        takes a beat to mount its input field after the welcome banner
        renders, and any keystroke sent during that mount window is
        silently eaten. Callers gate ``send_text_and_submit_verified``
        on this primitive so the FIRST send always lands on a bound
        input.

        ``marker`` defaults to ``? for shortcuts`` — claude's footer
        text once input is bound. Non-claude callers (bash stand-ins,
        screen runners) pass their own signal.

        ``capture_fn`` injects the pane-capture step so unit tests can
        feed a deterministic transcript without invoking real tmux.
        Defaults to :meth:`capture_content`.

        Raises :class:`TuiInputNotReadyError` on timeout — never
        returns False (a False return would let a caller silently
        continue and submit into a not-yet-bound input). Returns True
        when the marker is observed.
        """
        capture = capture_fn or TmuxManager.capture_content
        deadline = time.monotonic() + timeout_s
        last = ""
        while time.monotonic() < deadline:
            last = capture(session_name)
            if marker in last:
                return True
            if poll_s > 0:
                sleep_fn(poll_s)
        raise TuiInputNotReadyError(
            f"TUI input-ready marker {marker!r} not seen in pane "
            f"{session_name!r} within {timeout_s:.1f}s. Last pane "
            f"content:\n{last}"
        )

    @staticmethod
    def send_text_and_submit_verified(
        session_name: str,
        text: str,
        *,
        max_resends: int = 3,
        echo_wait_s: float = 2.0,
        poll_s: float = 0.25,
        sleep_fn: Callable[[float], None] = time.sleep,
        capture_fn: Callable[[str], str] | None = None,
        send_text_fn: Callable[[str, str], None] | None = None,
        send_enter_fn: Callable[[str], None] | None = None,
        echo_excerpt_len: int = 20,
    ) -> int:
        """Send ``text`` then Enter, retrying if the TUI dropped the keys.

        Algorithm — purely observation-driven, no sleep-hacks:

          1. send-keys the text.
          2. poll capture-pane until the first ``echo_excerpt_len`` chars
             of ``text`` echo back in the pane (proves the TUI accepted
             and rendered the keystrokes). Time-bounded by ``echo_wait_s``.
          3. on success → send Enter as a separate keystroke, return.
          4. on echo-not-seen → re-send the text, retry up to
             ``max_resends`` times. Each retry restarts the echo poll
             from scratch.
          5. on all retries dropped → raise :class:`TuiKeystrokeDropError`.

        The Enter is sent as its own keystroke (not as part of the text)
        because tmux's ``send-keys text\\r`` is interpreted as raw input
        — claude's TUI occasionally swallows the trailing CR if it
        re-renders mid-keystroke. A separate Enter call hits the input
        field after the text has visibly settled.

        Returns the 1-indexed attempt number that succeeded — surfaces
        to the operator as "delivered on retry N" telemetry without
        coupling the caller to a counter.

        All injection seams (``capture_fn``, ``send_text_fn``,
        ``send_enter_fn``, ``sleep_fn``) match the existing module
        pattern (see :meth:`send_keys`); tests use them to skip
        subprocess calls.
        """
        capture = capture_fn or TmuxManager.capture_content
        send_text = send_text_fn or (
            lambda session, payload: subprocess.run(
                ["tmux", "send-keys", "-t", session, payload],
                check=False,
                capture_output=True,
            )
        )
        send_enter = send_enter_fn or (
            lambda session: subprocess.run(
                ["tmux", "send-keys", "-t", session, "Enter"],
                check=False,
                capture_output=True,
            )
        )

        excerpt = text[:echo_excerpt_len]
        last_pane = ""
        for attempt in range(1, max_resends + 2):
            send_text(session_name, text)
            # Do-while: always capture at least once immediately after
            # the send, then poll until the echo deadline. This makes
            # ``echo_wait_s=0.0`` mean "one capture per attempt" rather
            # than "no captures at all" (which would silently bypass
            # the verification).
            last_pane = capture(session_name)
            if excerpt and excerpt in last_pane:
                send_enter(session_name)
                return attempt
            echo_deadline = time.monotonic() + echo_wait_s
            while time.monotonic() < echo_deadline:
                if poll_s > 0:
                    sleep_fn(poll_s)
                last_pane = capture(session_name)
                if excerpt and excerpt in last_pane:
                    send_enter(session_name)
                    return attempt
            # Echo never appeared within echo_wait_s; loop and resend.
        raise TuiKeystrokeDropError(
            f"TUI dropped {max_resends + 1} send attempts of {text!r} "
            f"into pane {session_name!r}. Echo excerpt {excerpt!r} never "
            f"appeared. Last pane content:\n{last_pane}"
        )

    @staticmethod
    def attach(session_name: str) -> None:
        """Attach to a tmux session (replaces current process)."""
        os.execvp("tmux", ["tmux", "attach", "-t", session_name])
