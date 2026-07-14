"""Tests for the ``--version`` fast path.

Two properties matter here. First, the line must carry a token that MOVES
when the code moves — a git sha when one is knowable, a content hash
otherwise — so the scheme never degrades to a bare declared version, not
even in a wheel built with no ``.git`` anywhere. Second, it must stay
parseable by the scripts that already shell it.
"""

from __future__ import annotations

from pathlib import Path

from scitex_agent_container._provenance import (
    identity,
    origin_mismatch,
    package_dir,
)
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


class TestOriginMismatch:
    """The guard that certifies every other test in this repo.

    A guard only ever seen to PASS is a hope. Two shipped in this fleet on
    2026-07-14 alone: a "fail-loud" version gate that returned exit 0 on the
    artifact it existed to reject, and a pin-check that was a substring match
    on "0.3" — so it rejected every STRONGER pin and froze the fleet's
    containers for months while looking like diligence. So the first thing
    asserted here is that this one REJECTS.

    Note `_audit.audit()` cannot stand in for these: under a bare `pytest` the
    import resolves to site-packages AND site-packages IS the installed
    distribution, so its `shadowed` check sees `origin == installed`, fires
    nothing, and returns ok=True (measured) — on the very case its docstring
    cites. Hence a separate check that takes the repo root as a parameter.
    """

    def test_rejects_the_installed_site_packages_copy(self, tmp_path: Path):
        # Arrange — a root the loaded module cannot possibly live under, which
        # is precisely the bare-`pytest` condition: the import came from
        # /opt/venv-sac/.../site-packages, the tests live here.
        foreign_root = tmp_path / "some-other-repo"

        # Act
        verdict = origin_mismatch(foreign_root)

        # Assert
        assert verdict is not None

    def test_names_the_path_it_actually_imported(self, tmp_path: Path):
        # Arrange
        foreign_root = tmp_path / "some-other-repo"

        # Act
        verdict = origin_mismatch(foreign_root)

        # Assert — half the failure mode is not knowing WHICH package ran.
        assert str(package_dir()) in verdict

    def test_names_the_path_it_expected(self, tmp_path: Path):
        # Arrange
        foreign_root = tmp_path / "some-other-repo"

        # Act
        verdict = origin_mismatch(foreign_root)

        # Assert — the other half is not knowing what it SHOULD have been.
        assert str(foreign_root.resolve() / "src") in verdict

    def test_says_the_version_string_is_a_fossil(self, tmp_path: Path):
        # Arrange — nobody may "improve" this into a version comparison. The
        # site-packages copy on this host reported 0.21.13 against 0.21.20 in
        # the tree; two host binaries reported 0.21.11 and 0.21.13 while
        # executing the same working tree. A version check is fooled by all of
        # them; a path check by none.
        foreign_root = tmp_path / "some-other-repo"

        # Act
        verdict = origin_mismatch(foreign_root)

        # Assert
        assert "fossil" in verdict.lower()

    def test_accepts_the_repo_that_is_actually_under_test(self):
        # Arrange — the other direction of the proof. A guard that cannot pass
        # is as useless as one that cannot fail: it just gets disabled. This is
        # the assertion that would have caught the original bug, and it is live
        # in tests/conftest.py::pytest_sessionstart on every run.
        project_root = Path(__file__).resolve().parents[3]

        # Act
        verdict = origin_mismatch(project_root)

        # Assert
        assert verdict is None


# EOF
