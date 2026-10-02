"""Real filesystem controls for immutable shared foundation font staging."""

import hashlib
import shutil

import pytest

from scitex_agent_container.containers import _noto_cjk as fonts


@pytest.fixture
def cached_assets(tmp_path):
    cache = tmp_path / "cache"
    cache.mkdir()
    payloads = {
        "sans.otf": b"fixture sans",
        "serif.otf": b"fixture serif",
        "OFL.txt": b"fixture license",
    }
    assets = tuple(
        fonts._Asset(name, name, hashlib.sha256(payload).hexdigest())
        for name, payload in payloads.items()
    )
    for name, payload in payloads.items():
        (cache / name).write_bytes(payload)
    return cache, assets, payloads


def _never_download(_asset, _destination):
    raise AssertionError("verified offline assets must not fetch")


def _stage(context, cache, assets, **kwargs):
    return fonts._stage_assets(
        context, cache=cache, assets=assets, commit="fixture-pin", **kwargs
    )


def _error_of(action):
    try:
        action()
    except (RuntimeError, OSError) as error:
        return error
    return None


def _missing(path):
    path.unlink()


def _corrupt(path):
    path.write_bytes(b"altered retained bytes")


def _symlink(path):
    alternate = path.parent / "alternate"
    path.rename(alternate)
    path.symlink_to(alternate)


def _wrong_hash(_asset, destination):
    destination.write_bytes(b"unverified download")


def _broken_transport(_asset, _destination):
    raise OSError("connection interrupted")


def _extra_file(staged):
    (staged / "unexpected.otf").write_bytes(b"unreviewed")


def _wrong_pin(staged):
    (staged / "SAC_UPSTREAM_COMMIT").write_text("other-pin\n")


def _wrong_license(staged):
    _corrupt(staged / "OFL.txt")


def _directory_link(staged):
    moved = staged.parent / "moved"
    staged.rename(moved)
    staged.symlink_to(moved, target_is_directory=True)


def _marker_link(staged):
    marker = staged / "SAC_UPSTREAM_COMMIT"
    external = staged.parent / "marker"
    marker.rename(external)
    marker.symlink_to(external)


def test_offline_context_preserves_exact_asset_bytes(tmp_path, cached_assets):
    # Arrange
    cache, assets, payloads = cached_assets

    # Act
    staged = _stage(
        tmp_path / "context", cache, assets, offline=True, download=_never_download
    )

    # Assert
    assert {name: (staged / name).read_bytes() for name in payloads} == payloads


def test_offline_context_carries_exact_source_pin(tmp_path, cached_assets):
    # Arrange
    cache, assets, _ = cached_assets

    # Act
    staged = _stage(tmp_path / "context", cache, assets, offline=True)

    # Assert
    assert (staged / "SAC_UPSTREAM_COMMIT").read_text() == "fixture-pin\n"


def test_repeated_verified_staging_is_idempotent(tmp_path, cached_assets):
    # Arrange
    cache, assets, _ = cached_assets
    context = tmp_path / "context"
    first = _stage(context, cache, assets, offline=True)
    before = {path.name: path.stat().st_mtime_ns for path in first.iterdir()}

    # Act
    second = _stage(context, cache, assets, offline=True, download=_never_download)

    # Assert
    assert {path.name: path.stat().st_mtime_ns for path in second.iterdir()} == before


@pytest.mark.parametrize("damage", [_missing, _corrupt, _symlink])
def test_untrusted_cache_never_publishes_context(tmp_path, cached_assets, damage):
    # Arrange
    cache, assets, _ = cached_assets
    target = cache / assets[-1].name
    damage(target)
    context = tmp_path / "context"

    # Act
    error = _error_of(lambda: _stage(context, cache, assets, offline=True))

    # Assert
    assert (type(error), context.exists()) == (RuntimeError, False)


def test_corrupt_cache_is_not_replaced_or_downloaded(tmp_path, cached_assets):
    # Arrange
    cache, assets, _ = cached_assets
    target = cache / assets[0].name
    target.write_bytes(b"corrupt retained cache")

    # Act
    error = _error_of(
        lambda: _stage(tmp_path / "context", cache, assets, download=_never_download)
    )

    # Assert
    assert (type(error), target.read_bytes()) == (
        RuntimeError,
        b"corrupt retained cache",
    )


def test_downloaded_bytes_are_verified_before_publication(tmp_path, cached_assets):
    # Arrange
    source, assets, payloads = cached_assets
    cache = tmp_path / "new-cache"

    def download(asset, destination):
        shutil.copyfile(source / asset.name, destination)

    # Act
    staged = _stage(tmp_path / "context", cache, assets, download=download)

    # Assert
    assert {name: (staged / name).read_bytes() for name in payloads} == payloads


@pytest.mark.parametrize(
    "download,error_type", [(_wrong_hash, RuntimeError), (_broken_transport, OSError)]
)
def test_bad_download_never_publishes_cache_or_context(
    tmp_path, cached_assets, download, error_type
):
    # Arrange
    _, assets, _ = cached_assets
    cache = tmp_path / "new-cache"
    context = tmp_path / "context"

    # Act
    error = _error_of(lambda: _stage(context, cache, assets, download=download))

    # Assert
    assert (type(error), context.exists(), (cache / assets[0].name).exists()) == (
        error_type,
        False,
        False,
    )


@pytest.mark.parametrize(
    "damage", [_extra_file, _wrong_pin, _wrong_license, _directory_link, _marker_link]
)
def test_staged_context_rejects_tampering(tmp_path, cached_assets, damage):
    # Arrange
    cache, assets, _ = cached_assets
    context = tmp_path / "context"
    staged = _stage(context, cache, assets, offline=True)
    damage(staged)

    # Act
    error = _error_of(lambda: _stage(context, cache, assets, offline=True))

    # Assert
    assert isinstance(error, RuntimeError)


def test_invalid_install_context_cannot_write_shared_font_paths(
    tmp_path, cached_assets
):
    # Arrange
    cache, assets, _ = cached_assets
    staged = _stage(tmp_path / "context", cache, assets, offline=True)
    root = tmp_path / "install-root"

    # Act
    error = _error_of(lambda: fonts._install_assets(staged, root=root))

    # Assert
    assert (type(error), root.exists()) == (RuntimeError, False)


@pytest.mark.parametrize(
    ("name", "family", "sha256"),
    [
        (
            "NotoSansCJKjp-Regular.otf",
            "Noto Sans CJK JP",
            "68a3fc98800b2a27b371f2fb79991daf3633bd89309d4ffaa6946fd587f375b5",
        ),
        (
            "NotoSerifCJKjp-Regular.otf",
            "Noto Serif CJK JP",
            "f7e7faca49383d7a44168fe3e3112f23deafd681a304e8695a2a17af31e98e5a",
        ),
        (
            "OFL-1.1.txt",
            "",
            "6a73f9541c2de74158c0e7cf6b0a58ef774f5a780bf191f2d7ec9cc53efe2bf2",
        ),
    ],
)
def test_canonical_assets_retain_reviewed_upstream_pins(name, family, sha256):
    # Arrange
    by_name = {asset.name: asset for asset in fonts._ASSETS}

    # Act
    asset = by_name[name]

    # Assert
    assert (asset.family, asset.sha256) == (family, sha256)
