"""``config.yaml`` may not key a peer on a name that is designed to move.

Companion to ``test_moving_alias.py``, which pins the registry itself. This
file pins where sac ENFORCES it, and — just as important — where it does not:

* ``Config.validate`` reports a moving-alias key, so ``sac host add/set``
  reverts the write and ``sac host validate`` goes red.
* ``PeersMap`` turns the post-migration ``KeyError('nas')`` into a message
  that says which machine to name instead. That covers every sac dispatch
  path at once, not only ssh.
* ``load`` stays tolerant. A config that still says ``nas`` keeps loading and
  keeps resolving — breaking it at import time would take the fleet down to
  fix a name.

NO MOCKS — real config files written to ``tmp_path`` and read by the real
loader.
"""

from __future__ import annotations

from pathlib import Path

from scitex_agent_container._state.host_config import PeersMap, load
from scitex_agent_container._state.moving_alias import MovingAliasError


def _write(path: Path, body: str) -> Path:
    """Write ``body`` as a config.yaml at ``path`` and return the path."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    return path


def _lookup_failure(peers: PeersMap, key: str) -> KeyError:
    """Return the error raised by ``peers[key]``; fail if the lookup succeeds.

    Written as a helper rather than ``pytest.raises`` so each test can keep a
    single assertion — the raises-block itself counts as one, which would
    leave the message unchecked.
    """
    try:
        peers[key]
    except KeyError as exc:
        return exc
    raise AssertionError(f"expected {key!r} to fail lookup, but it resolved")


def test_a_moving_alias_peer_key_is_rejected(tmp_path: Path) -> None:
    """The live config keyed the production NAS on `nas` for months."""
    # Arrange
    cfg_path = _write(tmp_path / "config.yaml", "peers:\n  nas:\n    ssh: nas\n")
    # Act
    errors = load(cfg_path).validate()
    # Assert
    assert len(errors) == 1


def test_the_rejection_names_the_stable_replacement(tmp_path: Path) -> None:
    """`sac host validate` must print what to change it to, not just that."""
    # Arrange
    cfg_path = _write(tmp_path / "config.yaml", "peers:\n  nas:\n    ssh: nas\n")
    # Act
    errors = load(cfg_path).validate()
    # Assert
    assert "nas-03" in errors[0]


def test_the_pinned_generation_validates_clean(tmp_path: Path) -> None:
    """The migrated config must pass, or the migration cannot be completed."""
    # Arrange
    cfg_path = _write(tmp_path / "config.yaml", "peers:\n  nas-03:\n    ssh: nas-03\n")
    # Act
    errors = load(cfg_path).validate()
    # Assert
    assert errors == []


def test_a_moving_alias_as_this_machines_canonical_name_is_rejected(
    tmp_path: Path,
) -> None:
    """host.aliases VALUES stamp every state.db row with this host's identity."""
    # Arrange
    cfg_path = _write(
        tmp_path / "config.yaml",
        "host:\n  aliases:\n    DXP480TPLUS-994: nas\npeers: {}\n",
    )
    # Act
    errors = load(cfg_path).validate()
    # Assert
    assert "DXP480TPLUS-994" in errors[0]


def test_an_alias_to_the_pinned_generation_validates_clean(tmp_path: Path) -> None:
    """The same mapping is correct once it names the generation."""
    # Arrange
    cfg_path = _write(
        tmp_path / "config.yaml",
        "host:\n  aliases:\n    DXP480TPLUS-994: nas-03\npeers: {}\n",
    )
    # Act
    errors = load(cfg_path).validate()
    # Assert
    assert errors == []


def test_config_that_still_names_the_alias_still_loads(tmp_path: Path) -> None:
    """Refusing at LOAD time would take every sac verb down over a name."""
    # Arrange
    cfg_path = _write(tmp_path / "config.yaml", "peers:\n  nas:\n    ssh: nas\n")
    # Act
    cfg = load(cfg_path)
    # Assert
    assert cfg.peers["nas"].ssh == "nas"


def test_dispatching_to_a_retired_alias_says_which_host_to_name() -> None:
    """After the re-key, `nas` is simply absent — and that reads as a typo."""
    # Arrange
    peers = PeersMap()
    # Act
    failure = _lookup_failure(peers, "nas")
    # Assert
    assert "nas-03" in str(failure)


def test_an_ordinary_unknown_peer_still_raises_a_plain_keyerror() -> None:
    """A guard that fires on everything cannot discriminate anything."""
    # Arrange
    peers = PeersMap()
    # Act
    failure = _lookup_failure(peers, "gpu-box")
    # Assert
    assert not isinstance(failure, MovingAliasError)


def test_callers_that_opt_into_absence_are_unchanged() -> None:
    """`.get` means the caller has already decided a miss is acceptable."""
    # Arrange
    peers = PeersMap()
    # Act
    resolved = peers.get("nas")
    # Assert
    assert resolved is None
