"""Tests for the ``--version`` fast path.

Two properties matter here. First, the line must carry a token that MOVES
when the code moves — a git sha when one is knowable, a content hash
otherwise — so the scheme never degrades to a bare declared version, not
even in a wheel built with no ``.git`` anywhere. Second, it must stay
parseable by the scripts that already shell it.
"""

from __future__ import annotations

from pathlib import Path

from scitex_agent_container._provenance import identity, package_dir
from scitex_agent_container._provenance._identity import format_terse, short_id


class TestShortId:
    def test_a_known_commit_is_shown_as_a_g_prefixed_sha(self):
        # Arrange
        info = {"commit": "082d2fe949118fe0b13e7bac2b5ecd966846167b"}

        # Act
        found = short_id(info)

        # Assert
        assert found == "g082d2fe9"

    def test_falls_back_to_the_content_hash_without_a_commit(self):
        # Arrange — a wheel built outside any checkout still has an
        # identity: the hash of the code it was built from.
        info = {"commit": None, "code_hash": "c6c986b1f49b77c7175eaea5cf74198d"}

        # Act
        found = short_id(info)

        # Assert
        assert found == "hc6c986b1"

    def test_says_unknown_rather_than_guessing(self):
        # Arrange — an artifact built before any of this existed.
        info = {"commit": None, "code_hash": None}

        # Act
        found = short_id(info)

        # Assert
        assert found == "unknown"


class TestFormatTerse:
    def test_the_version_stays_the_third_whitespace_field(self):
        # Arrange — scripts already run `sac --version | cut -d' ' -f3`;
        # keeping click's `<prog>, version <X.Y.Z>` prefix keeps them working.
        info = {
            "version": "0.21.13",
            "commit": "a" * 40,
            "code_hash": None,
            "built_at": None,
            "install": "wheel",
            "origin": "/opt/venv-sac/lib/python3.12/site-packages/scitex_agent_container",
        }

        # Act
        line = format_terse(info)

        # Assert
        assert line.split()[2] == "0.21.13"

    def test_the_line_names_where_the_module_was_loaded_from(self):
        # Arrange — the difference between site-packages and a worktree was
        # the entire content of a false "1087 passed", and it was invisible.
        origin = "/opt/venv-sac/lib/python3.12/site-packages/scitex_agent_container"
        info = {
            "version": "0.21.13",
            "commit": "a" * 40,
            "code_hash": None,
            "built_at": None,
            "install": "wheel",
            "origin": origin,
        }

        # Act
        line = format_terse(info)

        # Assert
        assert line.endswith(f"from {origin}")

    def test_the_line_carries_the_commit(self):
        # Arrange
        info = {
            "version": "0.21.13",
            "commit": "082d2fe949118fe0b13e7bac2b5ecd966846167b",
            "code_hash": None,
            "built_at": None,
            "install": "src",
            "origin": "/w/src/scitex_agent_container",
        }

        # Act
        line = format_terse(info)

        # Assert
        assert "g082d2fe9" in line


class TestIdentity:
    def test_origin_points_at_the_package_that_was_imported(self):
        # Arrange
        expected = str(Path(package_dir()))

        # Act
        info = identity()

        # Assert
        assert info["origin"] == expected

    def test_the_loaded_code_always_has_a_moving_identity(self):
        # Arrange — the whole requirement, as an invariant that must hold
        # for BOTH install kinds: editable (live .git) and wheel/SIF (the
        # stamp baked at build). If this ever reads "unknown", --version is
        # back to echoing a declared string that cannot answer "is my fix
        # deployed?".
        info = identity()

        # Act
        found = short_id(info)

        # Assert
        assert found != "unknown"

    def test_the_install_kind_is_always_classified(self):
        # Arrange
        info = identity()

        # Act
        found = info["install"]

        # Assert
        assert found in {"src", "wheel", "unknown"}

# EOF
