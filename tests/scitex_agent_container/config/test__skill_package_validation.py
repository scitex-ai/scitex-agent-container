"""The spec skill-package allowlist has a small, static shape."""

from scitex_agent_container.config._skill_package_validation import (
    validate_skill_packages,
)


def test_absent_or_empty_allowlist_exposes_no_implicit_packages():
    assert validate_skill_packages({}) == validate_skill_packages(
        {"skill_packages": []}
    ) == []


def test_paths_globs_non_strings_and_duplicates_fail_loud():
    errors = validate_skill_packages(
        {"skill_packages": ["../secret", "scitex-*", 7, "scitex", "scitex"]}
    )

    assert errors == [
        "spec.skill_packages[0] must be a non-empty package ID using only "
        "letters, digits, '.', '_' or '-' (paths and globs are not accepted)",
        "spec.skill_packages[1] must be a non-empty package ID using only "
        "letters, digits, '.', '_' or '-' (paths and globs are not accepted)",
        "spec.skill_packages[2] must be a non-empty package ID using only "
        "letters, digits, '.', '_' or '-' (paths and globs are not accepted)",
        "spec.skill_packages[4] duplicates package ID 'scitex'",
    ]
