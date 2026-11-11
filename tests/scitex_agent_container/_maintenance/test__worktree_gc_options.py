"""Python cleanup providers reject invalid thresholds before inspecting repos."""

import pytest

from scitex_agent_container._maintenance import gc_repo, gc_repos

_INVALID = [
    {"min_age_hours": float("nan")},
    {"min_age_hours": float("inf")},
    {"min_age_hours": -float("inf")},
    {"min_age_hours": -1},
    {"cap": -1},
]


@pytest.mark.parametrize("options", _INVALID)
@pytest.mark.parametrize("apply", [False, True])
def test_single_repo_refuses_invalid_thresholds(tmp_path, options, apply):
    # Arrange
    repo = tmp_path / "owned-missing-repo"

    # Act
    # Assert
    with pytest.raises(ValueError, match="min_age_hours|cap"):
        gc_repo(repo, apply=apply, **options)


@pytest.mark.parametrize("options", _INVALID)
@pytest.mark.parametrize("apply", [False, True])
@pytest.mark.parametrize("count", [0, 1])
def test_bulk_cleanup_refuses_invalid_thresholds(tmp_path, options, apply, count):
    # Arrange
    repos = [tmp_path / "owned-missing-repo" for _ in range(count)]

    # Act
    # Assert
    with pytest.raises(ValueError, match="min_age_hours|cap"):
        gc_repos(repos, apply=apply, **options)
