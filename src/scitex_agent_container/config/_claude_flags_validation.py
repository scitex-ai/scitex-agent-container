"""``spec.claude.flags`` one-argv-token-per-element guard.

Extracted from ``_claude_validation.py`` for the same reason
``_startup_command_validation`` was extracted from ``_validation``: a rule
with an incident behind it reads better as its own module than as a clause,
and it keeps the caller under the 512-line cap. Called from
``validate_claude`` via ``errors.extend(validate_claude_flags(claude_block))``.

WHY THIS EXISTS — measured 2026-08-06. The ``figrecipe`` agent was unbootable
for 15 days (dead since 2026-07-22, assumed to be a dead a2a sidecar). A
restart printed the real cause::

    error: unknown option '--effort ultracode'

Its spec listed ``--effort ultracode`` as ONE element of
``spec.claude.flags``. Each element becomes one argv token, so claude received
that whole string as a single option name and the inner process exited during
boot. Every restart in those 15 days failed identically, and nothing surfaced
it — the agent simply stayed unreachable.

Two properties made this expensive rather than merely wrong. The YAML looks
right: ``- --effort ultracode`` reads exactly like a command line, and the
list-of-argv-tokens contract is invisible at the point of authoring. And the
failure is observable only in boot stderr, which nobody reads until an agent
has been missing for weeks.

THE MATCHER (keyed to the LEADING DASH, deliberately not to whitespace):

  * An entry NOT starting with ``-`` is a VALUE, and its spaces are payload.
    Three live capsule specs pass ``{"mcpServers": {}}`` this way. A
    whitespace-keyed rule would reject all three and block their boots — the
    same harm as the bug, inverted, which is why the axis matters more than
    the symptom here.
  * ``--flag=value`` is one legitimate token even when the value contains
    spaces (``--mcp-config={"mcpServers": {}}``). So the glued case is the one
    whose FIRST whitespace comes BEFORE any ``=``: a flag, a separator, then
    something that should have been its own element.

The refusal REJECTS rather than auto-splits: an author who wrote
``--foo "a b"`` meaning a quoted value would be silently given different
semantics by a helpful splitter, and a spec that boots differently from what
it says is the class of problem this module exists to remove.

SECOND RULE — an inline ``--mcp-config`` JSON blob may not carry a secret
======================================================================
``spec.claude.flags`` is appended VERBATIM to the inner ``claude`` argv
(``runtimes._apptainer_inner_argv_tui._tui_runner_argv``, and the legacy
``_runners._tmux.claude_code`` path). That argv becomes the launcher's
command line, and ``/proc/<pid>/cmdline`` is world-readable on Linux while
``/proc/<pid>/environ`` is owner-only — so a secret written into a flag is a
secret published to every local user.

PR #1055 closed exactly this leak on the SDK side, where claude-agent-sdk was
serialising the whole MCP config (env blocks included) into the child's argv;
the fix writes a 0600 file and passes the PATH. An inline ``--mcp-config
{...}`` in ``spec.claude.flags`` re-opens the identical hole through a field
nothing was checking, and it does so BELOW the apptainer secret sweep:
``runtimes._apptainer_secret_env.redact_secret_env_to_file`` runs over the
FLAG region only, before the SIF and the inner command are appended — and
even if it ran later it would not help, because its recogniser matches
``--env KEY=VALUE`` pairs and an MCP blob is neither.

So the check belongs HERE, at the spec boundary, where the flag list is still
a clean list of tokens and every downstream argv consumer is covered at once.

It REFUSES rather than externalising the blob to a 0600 file. sac cannot know
which host directory backs this agent's container ``$HOME`` at validation
time, and materialising into the wrong one yields an agent that boots with a
silently empty MCP config — trading a loud, fixable spec error for the quiet
failure mode this package works hardest to avoid. The operator's fix is one
line: put the JSON in a file (``to_home/`` materialises it into the container
home) and pass the PATH, which ``--mcp-config`` already accepts.

The rule fires ONLY on a secret-shaped KEY inside a server's ``env`` block, so
the three live capsule specs passing ``{"mcpServers": {}}`` — and any inline
blob with no credentials in it — keep booting untouched.
"""

from __future__ import annotations

import json

from .._state._meta.secrets import _SECRET_ENV

#: The flag whose value may be an inline config blob rather than a path.
_MCP_CONFIG_FLAG = "--mcp-config"


def _is_glued_flag(entry: str) -> bool:
    """True when ``entry`` is a flag and its value crammed into ONE argv token.

    See the module docstring for the incident and the full matcher rationale.
    """
    if not entry.startswith("-"):
        return False  # a bare VALUE; its spaces are payload, not a separator
    first_space = min(
        (i for i, ch in enumerate(entry) if ch.isspace()),
        default=-1,
    )
    if first_space < 0:
        return False  # no whitespace at all — an ordinary flag
    equals = entry.find("=")
    # ``--flag=value with spaces`` is legitimate; ``--flag value`` is not.
    return equals < 0 or first_space < equals


def _inline_mcp_blob(flags: list, index: int) -> tuple[str, int] | None:
    """``(json_text, width)`` when ``flags[index]`` starts an INLINE mcp config.

    Handles both spellings the flag list allows — split
    (``["--mcp-config", "{...}"]``, width 2) and glued
    (``["--mcp-config={...}"]``, width 1). Returns ``None`` for a PATH value,
    which is the safe form and the one this rule steers authors toward: a
    filesystem path never begins with ``{``.
    """
    entry = flags[index]
    if not isinstance(entry, str):
        return None
    if entry == _MCP_CONFIG_FLAG:
        if index + 1 >= len(flags) or not isinstance(flags[index + 1], str):
            return None
        value, width = flags[index + 1], 2
    elif entry.startswith(_MCP_CONFIG_FLAG + "="):
        value, width = entry[len(_MCP_CONFIG_FLAG) + 1 :], 1
    else:
        return None
    return (value, width) if value.lstrip().startswith("{") else None


def _secret_env_keys_in_blob(json_text: str) -> list[str]:
    """Secret-shaped ``mcpServers.<name>.env`` KEY names inside ``json_text``.

    Returns ``"<server>.<KEY>"`` labels — NAMES only, never values: this list
    is rendered into an error message that reaches logs and terminals, and a
    message that quotes the credential to complain about its exposure would
    be its own disclosure.

    Unparseable JSON yields no findings. That is not a gap this rule needs to
    close: ``claude`` rejects a malformed ``--mcp-config`` itself, loudly, and
    guessing at the contents of something we cannot parse would produce
    false refusals on valid specs.
    """
    try:
        parsed = json.loads(json_text)
    except (ValueError, TypeError):
        return []
    servers = parsed.get("mcpServers") if isinstance(parsed, dict) else None
    if not isinstance(servers, dict):
        return []
    found: list[str] = []
    for name, entry in servers.items():
        if not isinstance(entry, dict):
            continue
        env = entry.get("env")
        if not isinstance(env, dict):
            continue
        for key, value in env.items():
            if _SECRET_ENV.search(str(key)) and str(value or "").strip():
                found.append(f"{name}.{key}")
    return found


def validate_claude_flags(claude_block: dict) -> list[str]:
    """Reject a ``spec.claude.flags`` element that glues a flag to its value.

    Returns a list of error strings (empty = valid), matching the
    ``list[str]`` contract of the other ``config._*_validation`` siblings.
    Defensive: any unexpected shape collapses to "nothing to check".
    """
    errors: list[str] = []
    if not isinstance(claude_block, dict):
        return errors
    flags = claude_block.get("flags")
    if flags is None:
        return errors
    if not isinstance(flags, list):
        errors.append(
            "spec.claude.flags must be a list of individual argv tokens, got "
            f"{type(flags).__name__}"
        )
        return errors
    for index, entry in enumerate(flags):
        if not isinstance(entry, str):
            errors.append(
                "spec.claude.flags[%d] must be a string argv token, got %r"
                % (index, entry)
            )
            continue
        if _is_glued_flag(entry):
            flag, _, value = entry.partition(" ")
            errors.append(
                f"spec.claude.flags[{index}] glues a flag to its value in one "
                f"argv token:\n    {entry!r}\n"
                "Every flags element is passed as ONE argv token, so claude "
                "receives this whole string as a single option name, fails "
                f"with \"unknown option '{entry}'\", and EXITS DURING BOOT. "
                "That is how figrecipe stayed dead for 15 days (2026-07-22 to "
                "2026-08-06): the restart failed the same way every time and "
                "nothing surfaced it. Split it into two elements:\n"
                f"    - {flag}\n    - {value.strip()}\n"
                "(Use the --flag=value spelling instead if the value itself "
                "contains spaces.)"
            )
    errors.extend(_validate_no_inline_secret_mcp_config(flags))
    return errors


def _validate_no_inline_secret_mcp_config(flags: list) -> list[str]:
    """Refuse an inline ``--mcp-config`` blob carrying a credential.

    See the module docstring for why this is a refusal and why it lives at
    the spec boundary rather than in the apptainer argv sweep.
    """
    errors: list[str] = []
    index = 0
    while index < len(flags):
        blob = _inline_mcp_blob(flags, index)
        if blob is None:
            index += 1
            continue
        json_text, width = blob
        leaked = _secret_env_keys_in_blob(json_text)
        if leaked:
            errors.append(
                f"spec.claude.flags[{index}] passes an INLINE {_MCP_CONFIG_FLAG} "
                "JSON blob whose server env block(s) carry secret-shaped "
                f"key(s): {', '.join(sorted(leaked))}.\n"
                "Every flags element is appended verbatim to the inner claude "
                "argv, and a process argv is WORLD-READABLE via "
                "/proc/<pid>/cmdline (unlike /proc/<pid>/environ, which the "
                "kernel restricts to the owning uid). Launching this spec "
                "would publish those values to every local user for the life "
                "of the agent — the same disclosure PR #1055 removed from the "
                "SDK path, which now writes a 0600 file and passes its path.\n"
                "Fix: move the JSON into a file and pass the PATH, which "
                f"{_MCP_CONFIG_FLAG} already accepts:\n"
                "    to_home/.mcp.json          (materialised into the "
                "container $HOME by sac)\n"
                f"    flags: [{_MCP_CONFIG_FLAG}, /home/agent/.mcp.json]\n"
                "Only the KEY NAMES are shown above; the values are withheld "
                "because this message reaches logs."
            )
        index += width
    return errors


__all__ = ["validate_claude_flags"]
