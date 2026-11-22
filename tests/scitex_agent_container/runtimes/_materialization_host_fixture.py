"""Owned host inputs for materialization tests; production readers stay real."""

import os

import pytest


@pytest.fixture
def materialization_host(tmp_path, env_save_restore):
    """Give each selected test an empty host home and a real empty pool file.

    Tests may override these defaults with env_save_restore as usual. Clearing
    inherited tokens and path overrides prevents operator host state from being
    injected into temporary agent homes. The existing env fixture restores all
    touched keys after each test; state-floor and provenance guards stay active.
    """
    home = tmp_path / "materialization-host"
    home.mkdir()
    pool = home / "empty-pool.src"
    pool.write_text("# owned empty secrets pool for materialization tests\n")
    env_save_restore.set("HOME", str(home))
    for key in ("SAC_HOST_CLAUDE_DIR", "SAC_TO_HOME_BASELINE",
                "SAC_USER_TO_HOME_BASELINE", "GITHUB_TOKEN", "GH_TOKEN"):
        env_save_restore.delete(key)
    for key in tuple(os.environ):
        if key == "CCT_BOT_TOKEN" or key.startswith("CCT_BOT_TOKEN_"):
            env_save_restore.delete(key)
    env_save_restore.set("SAC_SECRETS_ENVRC", str(pool))
    yield home
