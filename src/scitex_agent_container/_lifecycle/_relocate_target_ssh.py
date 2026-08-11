"""The target-side I/O the last three phases need: sac's own paths, a marker, a boot, a search.

Same discipline as :mod:`_relocate_transport_ssh` — marker lines rather than
exit codes, three-valued answers, and an exec that never happened is never a
measurement. Kept apart from :mod:`_relocate_effects_target`, which owns the
DECISIONS, so each stays under the module cap and so the question "what did we
actually ask the target" has one place to look.

WHICH ``$HOME`` THIS ASKS FOR, AND WHY IT IS THE RIGHT ONE HERE. The transcript
home is deliberately NOT probed — it follows the CONTAINER's home, the spec
decides it, and :mod:`_relocate_transcript_home` derives it (asking the target
would name the ssh user's home, a real directory the runner never reads). The
paths in THIS module are the opposite case: ``~/.scitex/agent-container/agents``
and ``~/.scitex/agent-container/runtime`` are sac's own host-side trees, which
sac on that machine resolves against the ssh user's ``$HOME``. So here the
target's ``$HOME`` is not a guess standing in for the real answer — it IS the
answer, and asking the target for it is how the coordinator and the target's own
sac agree on one path instead of two.

THE BRIEF TRAVELS AS base64 AND THAT IS NOT DECORATION. It is multi-line prose
containing quotes, colons and angle brackets, and it crosses ssh — which hands
ONE string to a remote login shell that re-parses it. Any quoting scheme that
survives that is a scheme someone will eventually get wrong by editing the
brief's wording. base64 is a single word of ``[A-Za-z0-9+/=]``: there is nothing
in it for a shell to interpret, so the text that arrives is the text that left,
whatever the brief later says.
"""

from __future__ import annotations

import base64
from typing import Callable

from ._relocate_shell import Shell, marked, one_marked, quote

__all__ = [
    "MARK_BOOT",
    "MARK_HOME",
    "MARK_HOSTNAME",
    "MARK_MATCH",
    "MARK_SID",
    "MARK_TREE",
    "deliver_prompt",
    "list_tree",
    "read_session_marker",
    "search_transcripts",
    "start_standby",
    "target_home",
    "target_hostname",
    "write_session_marker",
]

MARK_HOME = "TX-HOME="
MARK_HOSTNAME = "TX-HOSTNAME="
MARK_SID = "TX-SID="
MARK_BOOT = "TX-BOOT="
MARK_MATCH = "TX-MATCH="
MARK_TREE = "TX-TREE="

#: A ``sac agents start`` on a cold host pulls an image check, a preflight and a
#: container launch. Generous, because the cost of being impatient here is an
#: UNKNOWN on the phase that boots the agent.
START_TIMEOUT_S = 600.0
#: Delivering one prompt to a live sidecar. Short: this is a POST, and a long
#: wait here would be waiting for the agent's TURN, which is not what acceptance
#: means and not what this measures.
DELIVER_TIMEOUT_S = 180.0

#: The literal that means "the marker file is not there". A distinct word rather
#: than an empty value, so "the script answered, and the answer is absent" is
#: never confused with "the script printed nothing", which is not an answer.
SID_ABSENT = "(absent)"


def target_home(
    shell: Shell, *, exec_fn: Callable[..., dict] | None = None
) -> str | None:
    """The ssh user's ``$HOME`` on ``shell``, or ``None`` if it did not answer."""
    body = f"printf '{MARK_HOME}%s\\n' \"$HOME\""
    value = one_marked(shell.run(body, exec_fn=exec_fn), MARK_HOME)
    return (value or "").rstrip("/") or None


def target_hostname(
    shell: Shell, *, exec_fn: Callable[..., dict] | None = None
) -> str | None:
    """What ``hostname`` prints ON the target, or ``None`` if it did not answer.

    The proof-of-work question asks the agent to run exactly this command, so
    the expected answer must be computed by running exactly this command — not
    by assuming the fleet's name for the host is what the machine calls itself.
    They differ often enough that assuming would fail a healthy relocation while
    reporting the target's loop as broken.
    """
    body = f"printf '{MARK_HOSTNAME}%s\\n' \"$(hostname)\""
    value = one_marked(shell.run(body, exec_fn=exec_fn), MARK_HOSTNAME)
    return (value or "").strip() or None


def list_tree(
    shell: Shell,
    directory: str,
    *,
    exec_fn: Callable[..., dict] | None = None,
) -> tuple[tuple[str, int | None], ...] | None:
    """Every FILE under ``directory``, as ``(relative path, bytes)``, sorted.

    The completeness check for a carried DIRECTORY, and the reason a directory
    needs one at all: a per-file comparison of the names you thought to name
    proves nothing about the file you forgot. Measured on the 2026-08-11 canary
    — carrying only ``spec.yaml`` left ``to_home/`` behind, and sac's own
    spec-drift guard refused the boot rather than starting an agent whose
    to_home layer had silently vanished. Comparing the whole tree on both sides
    is what makes "the spec arrived" mean the spec rather than one file of it.

    ``None`` when the listing could not be taken; ``()`` when the directory is
    not there. Those differ, and the caller must keep them apart.

    A size that could not be read comes back as ``None`` rather than 0, so an
    unreadable file is never quietly recorded as an empty one.
    """
    body = (
        f"if [ -d {quote(directory)} ]; then\n"
        f"  printf '{MARK_HOME}yes\\n'\n"
        f"  cd {quote(directory)} 2>/dev/null || exit 0\n"
        f"  find . -type f | LC_ALL=C sort | while IFS= read -r __f; do\n"
        f'    printf \'{MARK_TREE}%s\\t%s\\n\' "$__f" "$(wc -c < "$__f" 2>/dev/null)"\n'
        f"  done\n"
        f"else\n"
        f"  printf '{MARK_HOME}no\\n'\n"
        f"fi"
    )
    run = shell.run(body, exec_fn=exec_fn)
    state = one_marked(run, MARK_HOME)
    if state is None:
        return None
    if state == "no":
        return ()
    out: list[tuple[str, int | None]] = []
    for raw in marked(run, MARK_TREE):
        name, _, size = raw.partition("\t")
        name = name.strip()
        if not name:
            continue
        out.append((name, int(size.strip()) if size.strip().isdigit() else None))
    return tuple(sorted(out))


def read_session_marker(
    shell: Shell,
    state_dir: str,
    *,
    exec_fn: Callable[..., dict] | None = None,
) -> str | None:
    """The target's ``<state_dir>/session_id``, :data:`SID_ABSENT`, or ``None``.

    Three answers, and the caller must keep them apart: an id (the target has
    booted before and owns a session), :data:`SID_ABSENT` (first boot — the only
    state in which seeding is legal), and ``None`` (not measured, which refuses).
    Seeding over an existing marker would discard whatever the target did with
    it; see :func:`._session_carry.plan_session_carry`.
    """
    marker = f"{state_dir}/session_id"
    body = (
        f"if [ -f {quote(marker)} ]; then\n"
        f"  printf '{MARK_SID}%s\\n' \"$(cat {quote(marker)} 2>/dev/null)\"\n"
        f"else\n"
        f"  printf '{MARK_SID}{SID_ABSENT}\\n'\n"
        f"fi"
    )
    value = one_marked(shell.run(body, exec_fn=exec_fn), MARK_SID)
    if value is None:
        return None
    return value.strip() or None


def write_session_marker(
    shell: Shell,
    state_dir: str,
    session_uuid: str,
    *,
    exec_fn: Callable[..., dict] | None = None,
) -> str | None:
    """Seed the marker, then READ IT BACK. Returns what the target now holds.

    The read-back is a second, independent observation in the same round trip,
    for the same reason the source's stop is verified rather than trusted: a
    write that exits 0 is the shell's opinion, and what the next boot resumes is
    the file's. ``None`` when the script did not answer.

    The append-only ``session_id_history`` is written too, mirroring
    :func:`.._runners._session_id.write_session_id`, so a later fork of this id
    stays auditable exactly as it would for an agent that never moved.
    """
    if not session_uuid:
        raise ValueError(
            "write_session_marker needs the session id to seed — an empty marker "
            "would make the target resume nothing while looking seeded"
        )
    marker = f"{state_dir}/session_id"
    history = f"{state_dir}/session_id_history"
    body = (
        f"mkdir -p {quote(state_dir)} 2>/dev/null\n"
        f"printf '%s' {quote(session_uuid)} > {quote(marker)} 2>/dev/null\n"
        f"if ! grep -qxF -- {quote(session_uuid)} {quote(history)} 2>/dev/null; then\n"
        f"  printf '%s\\n' {quote(session_uuid)} >> {quote(history)} 2>/dev/null\n"
        f"fi\n"
        f"if [ -f {quote(marker)} ]; then\n"
        f"  printf '{MARK_SID}%s\\n' \"$(cat {quote(marker)} 2>/dev/null)\"\n"
        f"else\n"
        f"  printf '{MARK_SID}{SID_ABSENT}\\n'\n"
        f"fi"
    )
    value = one_marked(shell.run(body, exec_fn=exec_fn), MARK_SID)
    if value is None:
        return None
    return value.strip() or None


def start_standby(
    shell: Shell,
    agent: str,
    *,
    exec_fn: Callable[..., dict] | None = None,
    timeout_s: float = START_TIMEOUT_S,
):
    """``sac agents start`` the agent on ``shell``, in CONTINUE mode.

    ``--session continue`` is passed EXPLICITLY rather than left to the spec's
    role default, and that is the whole point of the phase: a relocation carried
    a conversation and verified it by byte and line count, so the boot that
    follows must resume it. Inheriting a default here would let a spec whose
    role happens to resolve to ``fresh`` start the agent with no memory — which
    is the 2026-08-07 outcome, produced by an omission rather than a failure,
    and indistinguishable from success from the outside.

    ``--no-redispatch`` IS LOAD-BEARING AND WAS MEASURED. ``sac agents start``
    routes by the spec's ``host:`` pin
    (:func:`..cli_pkg.lifecycle._host_routing.resolve_spec_host_peer`), and the
    spec a relocation carries still pins the host being LEFT. Without this flag
    the start we issue ON THE TARGET ssh's straight back to the SOURCE and boots
    the agent there — measured on the 2026-08-11 canary, which reported
    ``'canary-resume-test' started on 'ywata-note-win'`` from a command running
    on scitex-compute-04. The relocation has already decided the placement; the
    flag says so, and it is the same one ``sac --on <peer> agents start`` passes
    for the same reason.

    ``--yes`` because there is no tty: without it the CLI previews the plan and
    refuses. Returns the run; the CALLER must still observe liveness separately,
    because a start command exiting 0 is the command's opinion and what the
    transport's precondition cares about is the tmux server's.
    """
    body = (
        f"sac agents start {quote(agent)} --session continue --no-redispatch "
        f"--yes --json 2>&1; "
        f"printf '{MARK_BOOT}%s\\n' \"$?\""
    )
    return shell.run(body, exec_fn=exec_fn, timeout_s=timeout_s)


def deliver_prompt(
    shell: Shell,
    agent: str,
    prompt: str,
    *,
    exec_fn: Callable[..., dict] | None = None,
    timeout_s: float = DELIVER_TIMEOUT_S,
):
    """Hand ``prompt`` to the agent's live sidecar ON its own host.

    The text is base64'd across the wire (see the module docstring) and decoded
    into a file on the target, which is then read into the CLI's positional
    argument. Nothing about the prompt's content can reach a shell as syntax.

    Returns the run. Acceptance is the CLI's exit status, printed on a marker
    line rather than inferred: ``sac agents send`` streams, so its stdout is the
    agent's business and only the marker is ours.
    """
    encoded = base64.b64encode(prompt.encode("utf-8")).decode("ascii")
    payload = f"/tmp/sac-relocate-brief-{agent}.txt"
    body = (
        f"printf '%s' {quote(encoded)} | base64 -d > {quote(payload)} 2>/dev/null\n"
        f'sac agents send {quote(agent)} "$(cat {quote(payload)})" --no-stream 2>&1; '
        f"printf '{MARK_BOOT}%s\\n' \"$?\""
    )
    return shell.run(body, exec_fn=exec_fn, timeout_s=timeout_s)


def search_transcripts(
    shell: Shell,
    directory: str,
    needle: str,
    *,
    exec_fn: Callable[..., dict] | None = None,
) -> tuple[str, ...] | None:
    """Every line under ``directory``'s ``*.jsonl`` containing ``needle``.

    ``None`` when the search could not be run at all — which the caller must
    report as NOT OBSERVED rather than as "no reply". An empty tuple is the
    other thing entirely: the search ran, the files were there, and the needle
    was not in them.

    ``grep -F`` because the needle is a nonce, not a pattern; a nonce that
    happened to contain a regex metacharacter would otherwise search for
    something else. Every ``*.jsonl`` in the directory is searched rather than
    only the carried one: an agent resuming a session may FORK it to a new id
    and write its reply into a file this relocation never named.
    """
    body = (
        f"if [ -d {quote(directory)} ]; then\n"
        f"  printf '{MARK_HOME}searched\\n'\n"
        f"  grep -h -F -- {quote(needle)} {quote(directory)}/*.jsonl 2>/dev/null | "
        f"sed 's/^/{MARK_MATCH}/'\n"
        f"else\n"
        f"  printf '{MARK_HOME}nodir\\n'\n"
        f"fi"
    )
    run = shell.run(body, exec_fn=exec_fn)
    state = one_marked(run, MARK_HOME)
    if state is None:
        return None
    if state == "nodir":
        return ()
    return tuple(marked(run, MARK_MATCH))
