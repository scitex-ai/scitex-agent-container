"""Tests for :mod:`scitex_agent_container._listen._inline_spec_bind_translate`.

Unit tests for the PR-2 SAC-side bind translate. Pure-Python over an
injected ``parent_binds_lookup`` callable — no FS, no Registry, no
HTTP. The wired end-to-end test lives in
``test_server_inline_spec_bind_translate.py``.

Pinning:
  * the longest-prefix match rule (nested binds resolve to the
    deeper rule),
  * the boundary-safe prefix check (``/work`` does NOT match
    ``/workdir``),
  * the input-shape preservation (string -> string, dict -> dict),
  * the ``TranslateResult.skipped_reason`` taxonomy
    (``no_caller`` | ``caller_unknown`` | ``no_parent_binds``),
  * the no-op fallback whenever the parent lookup raises or returns
    a non-list (defensive — PR-1 stays the SoT for safety).

AAA + one assert per test (PA-307); no mocks (PA-306). The injected
lookup IS the production seam — tests wire fakes via a closure, not
``unittest.mock``.
"""

from __future__ import annotations

from scitex_agent_container._listen._inline_spec_bind_translate import (
    BindTranslation,
    TranslateResult,
    translate_binds_in_spec,
)

# ---------------------------------------------------------------------------
# Spec + lookup builders
# ---------------------------------------------------------------------------


def _spec(binds: list) -> dict:
    return {
        "apiVersion": "scitex-agent-container/v3",
        "kind": "Agent",
        "spec": {"apptainer": {"binds": binds}},
    }


def _lookup_returning(binds: list[str] | None):
    """Build a ``parent_binds_lookup`` that always returns ``binds``."""

    def _lookup(_caller: str) -> list[str] | None:
        return binds

    return _lookup


def _lookup_raising(exc: Exception):
    """Build a ``parent_binds_lookup`` that always raises."""

    def _lookup(_caller: str) -> list[str] | None:
        raise exc

    return _lookup


# ---------------------------------------------------------------------------
# Happy-path translation
# ---------------------------------------------------------------------------


def test_translate_rewrites_work_prefix_to_parent_host_path() -> None:
    # Arrange — parent has /home/y/proj/foo bound at /work; child
    # asks for /work/data/X which the host can't see directly.
    spec = _spec(["/work/data/X:/inside_X:ro"])
    lookup = _lookup_returning(["/home/y/proj/foo:/work"])
    # Act
    translated, _result = translate_binds_in_spec(
        spec, caller="parent-agent", parent_binds_lookup=lookup
    )
    # Assert
    assert translated["spec"]["apptainer"]["binds"] == [
        "/home/y/proj/foo/data/X:/inside_X:ro"
    ]


def test_translate_marks_bind_as_changed_in_result() -> None:
    # Arrange
    spec = _spec(["/work/data/X:/inside"])
    lookup = _lookup_returning(["/home/y/foo:/work"])
    # Act
    _, result = translate_binds_in_spec(spec, caller="p", parent_binds_lookup=lookup)
    # Assert
    assert result.binds[0].was_changed is True


def test_translate_records_matched_prefix() -> None:
    # Arrange
    spec = _spec(["/work/data/X:/inside"])
    lookup = _lookup_returning(["/home/y/foo:/work"])
    # Act
    _, result = translate_binds_in_spec(spec, caller="p", parent_binds_lookup=lookup)
    # Assert
    assert result.binds[0].matched_prefix == "/work"


def test_translate_preserves_mode_field_on_string_form() -> None:
    # Arrange — :ro / :rw must round-trip through the rewrite.
    spec = _spec(["/work/data:/inside:rw"])
    lookup = _lookup_returning(["/home/y/foo:/work"])
    # Act
    translated, _ = translate_binds_in_spec(
        spec, caller="p", parent_binds_lookup=lookup
    )
    # Assert
    assert translated["spec"]["apptainer"]["binds"][0].endswith(":rw")


def test_translate_omits_mode_separator_when_no_mode_present() -> None:
    # Arrange — bind submitted without mode; emitter should not
    # inject a trailing ``:`` that didn't exist.
    spec = _spec(["/work/data:/inside"])
    lookup = _lookup_returning(["/home/y/foo:/work"])
    # Act
    translated, _ = translate_binds_in_spec(
        spec, caller="p", parent_binds_lookup=lookup
    )
    # Assert
    assert translated["spec"]["apptainer"]["binds"][0] == "/home/y/foo/data:/inside"


# ---------------------------------------------------------------------------
# Longest-prefix + boundary safety
# ---------------------------------------------------------------------------


def test_translate_picks_longest_prefix_when_nested_binds_match() -> None:
    # Arrange — parent has both /work AND /work/data bound. A child
    # bind /work/data/X must resolve via the deeper rule
    # (/work/data → /scratch/data) not the shallow one.
    spec = _spec(["/work/data/X:/inside"])
    lookup = _lookup_returning(["/home/y/foo:/work", "/scratch/data:/work/data"])
    # Act
    translated, _ = translate_binds_in_spec(
        spec, caller="p", parent_binds_lookup=lookup
    )
    # Assert
    assert translated["spec"]["apptainer"]["binds"][0].startswith("/scratch/data/X")


def test_translate_does_not_match_path_with_shared_prefix_chars() -> None:
    # Arrange — /work does NOT match /workdir; a naive
    # str.startswith would silently mangle this. The original
    # bind must pass through unchanged.
    spec = _spec(["/workdir/X:/inside"])
    lookup = _lookup_returning(["/home/y/foo:/work"])
    # Act
    translated, _ = translate_binds_in_spec(
        spec, caller="p", parent_binds_lookup=lookup
    )
    # Assert
    assert translated["spec"]["apptainer"]["binds"][0] == "/workdir/X:/inside"


def test_translate_matches_exact_prefix_when_no_tail() -> None:
    # Arrange — child binds the parent's bare /work mount point
    # itself (no subpath). Should still translate to the parent's
    # host root.
    spec = _spec(["/work:/inside"])
    lookup = _lookup_returning(["/home/y/foo:/work"])
    # Act
    translated, _ = translate_binds_in_spec(
        spec, caller="p", parent_binds_lookup=lookup
    )
    # Assert
    assert translated["spec"]["apptainer"]["binds"][0] == "/home/y/foo:/inside"


# ---------------------------------------------------------------------------
# No-op pass-through
# ---------------------------------------------------------------------------


def test_translate_no_op_when_host_src_does_not_match_any_prefix() -> None:
    # Arrange — child bind is already host-visible (e.g. /tmp/X).
    # No rule fires; original is preserved verbatim.
    spec = _spec(["/tmp/X:/inside"])
    lookup = _lookup_returning(["/home/y/foo:/work"])
    # Act
    translated, _ = translate_binds_in_spec(
        spec, caller="p", parent_binds_lookup=lookup
    )
    # Assert
    assert translated["spec"]["apptainer"]["binds"][0] == "/tmp/X:/inside"


def test_translate_no_op_marks_was_changed_false() -> None:
    # Arrange
    spec = _spec(["/tmp/X:/inside"])
    lookup = _lookup_returning(["/home/y/foo:/work"])
    # Act
    _, result = translate_binds_in_spec(spec, caller="p", parent_binds_lookup=lookup)
    # Assert
    assert result.binds[0].was_changed is False


def test_translate_no_op_records_empty_matched_prefix() -> None:
    # Arrange
    spec = _spec(["/tmp/X:/inside"])
    lookup = _lookup_returning(["/home/y/foo:/work"])
    # Act
    _, result = translate_binds_in_spec(spec, caller="p", parent_binds_lookup=lookup)
    # Assert
    assert result.binds[0].matched_prefix == ""


def test_translate_mixed_translatable_and_passthrough_binds() -> None:
    # Arrange — one /work bind (translatable) + one /tmp bind
    # (already host-visible). Both kept in input order; only the
    # translatable one is rewritten.
    spec = _spec(["/tmp/X:/x", "/work/data:/d"])
    lookup = _lookup_returning(["/home/y/foo:/work"])
    # Act
    translated, _ = translate_binds_in_spec(
        spec, caller="p", parent_binds_lookup=lookup
    )
    # Assert
    assert translated["spec"]["apptainer"]["binds"] == [
        "/tmp/X:/x",
        "/home/y/foo/data:/d",
    ]


# ---------------------------------------------------------------------------
# Dict-form bind preservation
# ---------------------------------------------------------------------------


def test_translate_preserves_dict_form_on_rewrite() -> None:
    # Arrange — caller submitted a dict-form bind. Translate must
    # return a dict (not a stringified host:dst:mode) so the YAML
    # the inline-spec handler writes preserves the operator's form.
    spec = _spec([{"src": "/work/data", "dst": "/inside", "mode": "ro"}])
    lookup = _lookup_returning(["/home/y/foo:/work"])
    # Act
    translated, _ = translate_binds_in_spec(
        spec, caller="p", parent_binds_lookup=lookup
    )
    # Assert
    assert isinstance(translated["spec"]["apptainer"]["binds"][0], dict)


def test_translate_rewrites_src_in_dict_form() -> None:
    # Arrange
    spec = _spec([{"src": "/work/data", "dst": "/inside", "mode": "ro"}])
    lookup = _lookup_returning(["/home/y/foo:/work"])
    # Act
    translated, _ = translate_binds_in_spec(
        spec, caller="p", parent_binds_lookup=lookup
    )
    # Assert
    assert translated["spec"]["apptainer"]["binds"][0]["src"] == "/home/y/foo/data"


def test_translate_keeps_other_dict_keys_intact() -> None:
    # Arrange — a hypothetical extra key (e.g. an annotation)
    # should survive the rewrite untouched.
    spec = _spec(
        [{"src": "/work/data", "dst": "/inside", "mode": "ro", "note": "hello"}]
    )
    lookup = _lookup_returning(["/home/y/foo:/work"])
    # Act
    translated, _ = translate_binds_in_spec(
        spec, caller="p", parent_binds_lookup=lookup
    )
    # Assert
    assert translated["spec"]["apptainer"]["binds"][0]["note"] == "hello"


# ---------------------------------------------------------------------------
# skipped_reason taxonomy
# ---------------------------------------------------------------------------


def test_skip_reason_no_caller_when_caller_is_none() -> None:
    # Arrange — operator-submitted spec, no caller field. Translate
    # must be a no-op tagged ``no_caller``.
    spec = _spec(["/work/X:/x"])
    lookup = _lookup_returning(["/home/y/foo:/work"])
    # Act
    _, result = translate_binds_in_spec(spec, caller=None, parent_binds_lookup=lookup)
    # Assert
    assert result.skipped_reason == "no_caller"


def test_skip_reason_caller_unknown_when_lookup_returns_none() -> None:
    # Arrange — caller is not a known SAC-managed agent.
    spec = _spec(["/work/X:/x"])
    lookup = _lookup_returning(None)
    # Act
    _, result = translate_binds_in_spec(
        spec, caller="ghost", parent_binds_lookup=lookup
    )
    # Assert
    assert result.skipped_reason == "caller_unknown"


def test_skip_reason_caller_unknown_when_lookup_raises() -> None:
    # Arrange — parent's spec is unreadable / config-invalid /
    # any other exception. Must collapse to ``caller_unknown``
    # rather than 500-ing the spawn.
    spec = _spec(["/work/X:/x"])
    lookup = _lookup_raising(RuntimeError("config load failed"))
    # Act
    _, result = translate_binds_in_spec(
        spec, caller="broken", parent_binds_lookup=lookup
    )
    # Assert
    assert result.skipped_reason == "caller_unknown"


def test_skip_reason_no_parent_binds_when_parent_has_empty_binds() -> None:
    # Arrange — parent exists but has no apptainer.binds at all
    # (e.g. a non-apptainer runtime). Translate is a no-op.
    spec = _spec(["/work/X:/x"])
    lookup = _lookup_returning([])
    # Act
    _, result = translate_binds_in_spec(
        spec, caller="parentless", parent_binds_lookup=lookup
    )
    # Assert
    assert result.skipped_reason == "no_parent_binds"


def test_caller_known_true_when_lookup_succeeds() -> None:
    # Arrange — parent record located.
    spec = _spec(["/work/X:/x"])
    lookup = _lookup_returning(["/home/y/foo:/work"])
    # Act
    _, result = translate_binds_in_spec(
        spec, caller="parent", parent_binds_lookup=lookup
    )
    # Assert
    assert result.caller_known is True


def test_caller_known_false_when_lookup_returns_none() -> None:
    # Arrange
    spec = _spec(["/work/X:/x"])
    lookup = _lookup_returning(None)
    # Act
    _, result = translate_binds_in_spec(
        spec, caller="ghost", parent_binds_lookup=lookup
    )
    # Assert
    assert result.caller_known is False


# ---------------------------------------------------------------------------
# Defensive — spec shape + bind shape robustness
# ---------------------------------------------------------------------------


def test_translate_handles_spec_with_no_apptainer_block() -> None:
    # Arrange — a spec carrying no apptainer.binds (e.g. a docker
    # runtime). No-op; returns the input dict unchanged.
    spec = {
        "apiVersion": "scitex-agent-container/v3",
        "kind": "Agent",
        "spec": {"workdir": "/tmp"},
    }
    lookup = _lookup_returning(["/home/y/foo:/work"])
    # Act
    translated, _ = translate_binds_in_spec(
        spec, caller="p", parent_binds_lookup=lookup
    )
    # Assert
    assert translated == spec


def test_translate_passes_through_malformed_bind_string() -> None:
    # Arrange — a string without a ``:`` separator is malformed.
    # Translate must NOT mangle it (PR-1's preflight will reject
    # it as ``bind_unresolvable`` downstream).
    spec = _spec(["this-is-not-a-bind"])
    lookup = _lookup_returning(["/home/y/foo:/work"])
    # Act
    translated, _ = translate_binds_in_spec(
        spec, caller="p", parent_binds_lookup=lookup
    )
    # Assert
    assert translated["spec"]["apptainer"]["binds"] == ["this-is-not-a-bind"]


def test_translate_passes_through_dict_bind_missing_src() -> None:
    # Arrange — dict missing ``src``. Passes through; PR-1 will
    # surface the ``bind_unresolvable``.
    spec = _spec([{"dst": "/inside", "mode": "ro"}])
    lookup = _lookup_returning(["/home/y/foo:/work"])
    # Act
    translated, _ = translate_binds_in_spec(
        spec, caller="p", parent_binds_lookup=lookup
    )
    # Assert
    assert translated["spec"]["apptainer"]["binds"][0] == {
        "dst": "/inside",
        "mode": "ro",
    }


def test_translate_does_not_mutate_input_spec() -> None:
    # Arrange — caller's spec dict should not be aliased into the
    # returned dict (the call site discards the return on rare
    # error paths and must not see a partially-rewritten input).
    spec = _spec(["/work/data:/inside"])
    lookup = _lookup_returning(["/home/y/foo:/work"])
    # Act
    translate_binds_in_spec(spec, caller="p", parent_binds_lookup=lookup)
    # Assert
    assert spec["spec"]["apptainer"]["binds"] == ["/work/data:/inside"]


def test_translate_returns_dataclass_result() -> None:
    # Arrange
    spec = _spec(["/work/X:/x"])
    lookup = _lookup_returning(["/home/y/foo:/work"])
    # Act
    _, result = translate_binds_in_spec(spec, caller="p", parent_binds_lookup=lookup)
    # Assert
    assert isinstance(result, TranslateResult)


def test_translate_per_bind_result_is_dataclass() -> None:
    # Arrange
    spec = _spec(["/work/X:/x"])
    lookup = _lookup_returning(["/home/y/foo:/work"])
    # Act
    _, result = translate_binds_in_spec(spec, caller="p", parent_binds_lookup=lookup)
    # Assert
    assert isinstance(result.binds[0], BindTranslation)


def test_translate_handles_parent_binds_containing_garbage_entries() -> None:
    # Arrange — parent's binds list has a malformed entry mixed
    # with a good one. The good rule must still fire.
    spec = _spec(["/work/X:/x"])
    lookup = _lookup_returning(["not-a-bind", "/home/y/foo:/work"])
    # Act
    translated, _ = translate_binds_in_spec(
        spec, caller="p", parent_binds_lookup=lookup
    )
    # Assert
    assert translated["spec"]["apptainer"]["binds"] == ["/home/y/foo/X:/x"]
