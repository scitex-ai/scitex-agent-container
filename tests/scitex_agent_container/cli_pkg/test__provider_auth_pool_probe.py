"""Declared-pool diagnostics use the same controlled native inference proof."""

import pytest

from scitex_agent_container._lifecycle._hermes_restart_preflight import (
    reset_probe_cache,
)
from scitex_agent_container.cli_pkg._provider_auth_probe import (
    OK,
    PROBE_FAILED,
    probe_provider_auth,
)
from scitex_agent_container.config import AgentConfig
from scitex_agent_container.config._hermes_failover import HermesFailoverSpec
from scitex_agent_container.config._provider_types import ProviderSpec
from tests.scitex_agent_container.cli_pkg.test__provider_auth_probe import (
    _ENV_NAME,
    _GOOD_KEY,
    _serve_hermes_chat,
)


@pytest.mark.parametrize("accept_all,expected", [(False, OK), (True, PROBE_FAILED)])
def test_real_native_http_pool_probe_is_shared_and_discriminates_invalid_keys(
    tmp_path, monkeypatch, accept_all, expected
):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv(_ENV_NAME, _GOOD_KEY)
    reset_probe_cache()
    try:
        with _serve_hermes_chat(accept_all=accept_all, completion=True) as (
            url,
            observed,
        ):
            config = AgentConfig(
                name="synthetic-pool", harness="hermes", runtime="tui", workdir="/work"
            )
            config.engine_key = "muse"
            config.model = "muse-spark-1.3-contributor"
            config.claude.provider = ProviderSpec(
                base_url=url + "/v1", auth_token_env=_ENV_NAME
            )
            config.hermes_failover = HermesFailoverSpec(accounts={"muse": [_ENV_NAME]})
            first = probe_provider_auth(config, timeout=5)
            second = probe_provider_auth(config, timeout=5)
            assert (
                first.state,
                second.state,
                len(observed),
                _GOOD_KEY not in first.detail,
            ) == (
                expected,
                expected,
                1 if accept_all else 2,
                True,
            )
    finally:
        reset_probe_cache()
