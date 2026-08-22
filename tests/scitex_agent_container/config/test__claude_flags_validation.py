"""``spec.claude.flags`` must carry one argv token per element.

INCIDENT 2026-08-06. ``figrecipe`` had been unreachable since 2026-07-22 and
was assumed to be a dead a2a sidecar. It was not: a restart printed

    error: unknown option '--effort ultracode'

Its spec listed ``--effort ultracode`` as ONE element of ``spec.claude.flags``.
Each element becomes one argv token, so claude got that whole string as a
single option name and the inner process exited during boot. Every restart in
those 15 days failed the same way, silently — the agent simply stayed
unreachable, and the real cause was invisible until someone ran a restart and
read the boot stderr.

What makes it worth a validator rather than a fixed spec: nothing about the
YAML looks wrong. ``- --effort ultracode`` reads exactly like a command line.
The list-of-argv-tokens contract is invisible at the point of authoring, and
the failure surfaces only at boot, in a log nobody reads until an agent has
been missing for two weeks.

These tests pin the RULE (which shapes are refused) and, just as importantly,
the shapes that must NOT be refused — a false positive here blocks an agent
boot, which is the same harm in the other direction.

Real ``validate_claude`` on real dicts, no mocks.
"""

from __future__ import annotations

import pytest

from scitex_agent_container.config._claude_validation import validate_claude


def _errors_for(flags: object) -> list[str]:
    return validate_claude({"claude": {"model": "haiku", "flags": flags}})


# --------------------------------------------------------------------------
# The incident itself
# --------------------------------------------------------------------------


def test_the_figrecipe_entry_is_rejected() -> None:
    """Regression pin: the exact element that killed figrecipe for 15 days."""
    # Arrange
    flags = ["--dangerously-skip-permissions", "--effort ultracode"]
    # Act
    errors = _errors_for(flags)
    # Assert
    assert any("--effort ultracode" in e for e in errors)


def test_the_error_names_the_index_and_offers_the_split() -> None:
    """A refusal must say what to write instead, not merely that it is wrong."""
    # Arrange
    flags = ["--dangerously-skip-permissions", "--effort ultracode"]
    # Act
    errors = _errors_for(flags)
    # Assert
    assert any("flags[1]" in e and "--effort" in e and "ultracode" in e for e in errors)


def test_the_offered_split_actually_clears_the_condition() -> None:
    """The hint must produce a VALID spec — a repair hint that still fails is
    worse than none, because it burns the one attempt the author will make."""
    # Arrange — precisely what the error message tells the author to write.
    flags = ["--dangerously-skip-permissions", "--effort", "ultracode"]
    # Act
    errors = _errors_for(flags)
    # Assert
    assert errors == []


# --------------------------------------------------------------------------
# Shapes that must NOT be refused (false positives block a boot too)
# --------------------------------------------------------------------------


def test_a_bare_value_containing_spaces_is_allowed() -> None:
    """Three live capsule specs pass this exact JSON as a flags element.

    It contains a space, but it is a VALUE — the space is payload and never
    reaches an option parser. Keying the rule on whitespace alone would have
    rejected all three.
    """
    # Arrange
    flags = ["--mcp-config", '{"mcpServers": {}}']
    # Act
    errors = _errors_for(flags)
    # Assert
    assert errors == []


def test_the_equals_spelling_with_a_spaced_value_is_allowed() -> None:
    """``--flag=value`` is one legitimate token even when the value has spaces."""
    # Arrange
    flags = ['--mcp-config={"mcpServers": {}}']
    # Act
    errors = _errors_for(flags)
    # Assert
    assert errors == []


@pytest.mark.parametrize(
    "flag",
    ["--dangerously-skip-permissions", "--effort", "-v", "--verbose", "ultracode"],
)
def test_ordinary_single_tokens_are_allowed(flag: str) -> None:
    """Control: if any of these were refused, the guard is over-broad."""
    # Arrange
    flags = [flag]
    # Act
    errors = _errors_for(flags)
    # Assert
    assert errors == []


def test_absent_flags_produce_no_error() -> None:
    """Control: the rule must be silent when there is nothing to check."""
    # Arrange
    spec = {"claude": {"model": "haiku"}}
    # Act
    errors = validate_claude(spec)
    # Assert
    assert errors == []


# --------------------------------------------------------------------------
# Shape errors
# --------------------------------------------------------------------------


def test_a_string_instead_of_a_list_is_rejected() -> None:
    """``flags: --effort ultracode`` (no list) is the same mistake, one level up."""
    # Arrange
    flags = "--effort ultracode"
    # Act
    errors = _errors_for(flags)
    # Assert
    assert any("must be a list" in e for e in errors)


def test_a_non_string_element_is_rejected() -> None:
    # Arrange
    flags = ["--effort", 3]
    # Act
    errors = _errors_for(flags)
    # Assert
    assert any("flags[1]" in e and "string" in e for e in errors)


# --------------------------------------------------------------------------
# An INLINE ``--mcp-config`` blob may not carry a credential
#
# THE HOLE THIS PINS. ``spec.claude.flags`` is appended VERBATIM to the inner
# claude argv, and a process argv is world-readable via /proc/<pid>/cmdline.
# PR #1055 had just removed exactly this disclosure from the SDK path (the
# MCP config now goes to a 0600 file and only the PATH travels) — but an
# inline ``--mcp-config {...}`` written into spec.claude.flags re-opened it
# through a field nothing checked, and BELOW the apptainer secret sweep:
# ``redact_secret_env_to_file`` runs over the FLAG region only, before the
# SIF and the inner command are appended.
#
# Reordering that sweep would NOT have closed this. Its recogniser matches
# ``--env KEY=VALUE`` pairs; an MCP blob is not one, so the sweep would have
# walked past it wherever it ran. The check has to understand the blob, which
# is why it lives here, at the spec boundary, where the flag list is still a
# clean list of tokens and every downstream argv consumer is covered at once.
#
# Measured with a real spec + real build_run_argv: before, a token-shaped
# sentinel inside the blob's env block was visible in ``ps -eo args`` on the
# launched process (1 pid, /proc/<pid>/cmdline mode 0444); after, the spec is
# refused at load and no argv is built at all (0 pids).
# --------------------------------------------------------------------------

_SECRET_BLOB = (
    '{"mcpServers": {"demo": {"type": "stdio", "command": "/bin/true", '
    '"env": {"DEMO_API_KEY": "ZZZ-sentinel-mcp-not-a-real-token"}}}}'
)


def test_inline_mcp_config_carrying_a_secret_is_rejected() -> None:
    """The split spelling: ``["--mcp-config", "{...}"]``."""
    # Arrange
    flags = ["--strict-mcp-config", "--mcp-config", _SECRET_BLOB]
    # Act
    errors = _errors_for(flags)
    # Assert
    assert any("--mcp-config" in e and "DEMO_API_KEY" in e for e in errors)


def test_glued_inline_mcp_config_carrying_a_secret_is_rejected() -> None:
    """The glued spelling must not be a way around the same rule."""
    # Arrange
    flags = [f"--mcp-config={_SECRET_BLOB}"]
    # Act
    errors = _errors_for(flags)
    # Assert
    assert any("DEMO_API_KEY" in e for e in errors)


def test_the_refusal_never_quotes_the_secret_value() -> None:
    """A message about an exposed credential must not restate it.

    This text reaches logs and terminals; naming the KEY is what makes the
    error actionable, and withholding the VALUE is what keeps the complaint
    from becoming a second disclosure.
    """
    # Arrange
    flags = ["--mcp-config", _SECRET_BLOB]
    # Act
    errors = _errors_for(flags)
    # Assert
    assert not any("ZZZ-sentinel-mcp-not-a-real-token" in e for e in errors)


def test_the_refusal_points_at_the_file_path_form() -> None:
    """The fix must be in the message: pass a PATH, which the flag accepts."""
    # Arrange
    flags = ["--mcp-config", _SECRET_BLOB]
    # Act
    errors = _errors_for(flags)
    # Assert
    assert any(".mcp.json" in e for e in errors)


# --------------------------------------------------------------------------
# ...and the shapes that must keep booting. A false positive here blocks a
# live agent, which is the same harm inverted.
# --------------------------------------------------------------------------


def test_the_live_capsule_empty_inline_blob_is_accepted() -> None:
    """Three live capsule specs pass exactly this; it carries no credential."""
    # Arrange
    flags = ["--strict-mcp-config", "--mcp-config", '{"mcpServers": {}}']
    # Act
    errors = _errors_for(flags)
    # Assert
    assert errors == []


def test_an_inline_blob_with_a_non_secret_env_is_accepted() -> None:
    """The rule keys on a SECRET-shaped name, not on having an env block."""
    # Arrange
    blob = (
        '{"mcpServers": {"demo": {"type": "stdio", "command": "/bin/true", '
        '"env": {"DEMO_BASE_URL": "https://example.invalid"}}}}'
    )
    # Act
    errors = _errors_for(["--mcp-config", blob])
    # Assert
    assert errors == []


def test_an_mcp_config_given_as_a_path_is_accepted() -> None:
    """The safe form the refusal steers authors toward must stay legal."""
    # Arrange
    flags = ["--mcp-config", "/home/agent/.mcp.json"]
    # Act
    errors = _errors_for(flags)
    # Assert
    assert errors == []


def test_an_unparseable_inline_blob_is_not_refused_by_this_rule() -> None:
    """claude rejects malformed JSON itself, loudly and with a better message.

    Guessing at the contents of something we cannot parse would only produce
    false refusals on valid specs.
    """
    # Arrange
    flags = ["--mcp-config", "{not json at all"]
    # Act
    errors = _errors_for(flags)
    # Assert
    assert errors == []
