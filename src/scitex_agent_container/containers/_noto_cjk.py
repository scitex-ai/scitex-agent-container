"""Pinned Noto CJK JP assets for the shared SAC foundation build context.

Follows Dev ci-cpu's upstream, license, font directory and native-library
contract. Only two static Regular faces are shipped; applications inherit
them from the base image. Run this stdlib-only helper directly during a
remote build before SAC or its Python dependencies have been installed.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

__all__: list[str] = []

_COMMIT = "523d033d6cb47f4a80c58a35753646f5c3608a78"
_UPSTREAM = f"https://raw.githubusercontent.com/notofonts/noto-cjk/{_COMMIT}"
_CONTEXT_NAME = "noto-cjk-jp"
_FONT_DIR = Path("usr/local/share/fonts/opentype/noto-cjk-jp")
_LICENSE_DIR = Path("usr/share/licenses/noto-cjk-jp")


@dataclass(frozen=True)
class _Asset:
    name: str
    upstream_path: str
    sha256: str
    family: str = ""


_ASSETS = (
    _Asset(
        "NotoSansCJKjp-Regular.otf",
        "Sans/OTF/Japanese/NotoSansCJKjp-Regular.otf",
        "68a3fc98800b2a27b371f2fb79991daf3633bd89309d4ffaa6946fd587f375b5",
        "Noto Sans CJK JP",
    ),
    _Asset(
        "NotoSerifCJKjp-Regular.otf",
        "Serif/OTF/Japanese/NotoSerifCJKjp-Regular.otf",
        "f7e7faca49383d7a44168fe3e3112f23deafd681a304e8695a2a17af31e98e5a",
        "Noto Serif CJK JP",
    ),
    _Asset(
        "OFL-1.1.txt",
        "LICENSE",
        "6a73f9541c2de74158c0e7cf6b0a58ef774f5a780bf191f2d7ec9cc53efe2bf2",
    ),
)


def _digest(path: Path) -> str:
    with path.open("rb") as source:
        return hashlib.file_digest(source, "sha256").hexdigest()


def _verify_asset(path: Path, asset: _Asset) -> None:
    if path.is_symlink() or not path.is_file():
        raise RuntimeError(f"missing regular Noto asset: {path}")
    if _digest(path) != asset.sha256:
        raise RuntimeError(f"Noto asset SHA-256 mismatch: {path}")


def _download(asset: _Asset, destination: Path) -> None:
    with urllib.request.urlopen(
        f"{_UPSTREAM}/{asset.upstream_path}", timeout=30
    ) as source:
        with destination.open("wb") as target:
            shutil.copyfileobj(source, target)


def _cache_root() -> Path:
    root = Path(os.environ.get("XDG_CACHE_HOME", str(Path.home() / ".cache")))
    return root / "scitex-agent-container" / "upstream" / "noto-cjk" / _COMMIT


def _cached_asset(
    asset: _Asset,
    cache: Path,
    *,
    offline: bool,
    download: Callable[[_Asset, Path], None],
) -> Path:
    cached = cache / asset.name
    if cached.exists() or cached.is_symlink():
        _verify_asset(cached, asset)
        return cached
    if offline:
        raise RuntimeError(f"Noto asset is not cached for offline staging: {cached}")
    cache.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="noto-fetch-", dir=cache) as temp:
        candidate = Path(temp) / asset.name
        download(asset, candidate)
        _verify_asset(candidate, asset)
        candidate.chmod(0o644)
        candidate.replace(cached)
    return cached


def _verify_context(
    source: Path, *, assets: tuple[_Asset, ...] = _ASSETS, commit: str = _COMMIT
) -> None:
    expected = {asset.name for asset in assets} | {"SAC_UPSTREAM_COMMIT"}
    if (
        source.is_symlink()
        or not source.is_dir()
        or set(p.name for p in source.iterdir()) != expected
    ):
        raise RuntimeError(f"unexpected Noto build context: {source}")
    marker = source / "SAC_UPSTREAM_COMMIT"
    if marker.is_symlink() or marker.read_text() != f"{commit}\n":
        raise RuntimeError(f"Noto upstream commit mismatch: {source}")
    for asset in assets:
        _verify_asset(source / asset.name, asset)


def _stage_assets(
    context: Path,
    *,
    cache: Path | None = None,
    offline: bool = False,
    assets: tuple[_Asset, ...] = _ASSETS,
    commit: str = _COMMIT,
    download: Callable[[_Asset, Path], None] = _download,
) -> Path:
    """Verify cached/downloaded bytes before publishing one complete context."""
    cache = _cache_root() if cache is None else cache
    sources = [
        _cached_asset(asset, cache, offline=offline, download=download)
        for asset in assets
    ]
    destination = context / _CONTEXT_NAME
    if destination.exists() or destination.is_symlink():
        _verify_context(destination, assets=assets, commit=commit)
        return destination
    context.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="noto-stage-", dir=context) as temp:
        staged = Path(temp) / _CONTEXT_NAME
        staged.mkdir()
        for asset, source in zip(assets, sources, strict=True):
            shutil.copyfile(source, staged / asset.name)
            (staged / asset.name).chmod(0o644)
        (staged / "SAC_UPSTREAM_COMMIT").write_text(f"{commit}\n")
        _verify_context(staged, assets=assets, commit=commit)
        staged.replace(destination)
    return destination


def _install_assets(source: Path, *, root: Path = Path("/")) -> None:
    """Install the verified pair and license once in the shared foundation."""
    _verify_context(source)
    font_dir = root / _FONT_DIR
    license_dir = root / _LICENSE_DIR
    font_dir.mkdir(parents=True, exist_ok=True, mode=0o755)
    license_dir.mkdir(parents=True, exist_ok=True, mode=0o755)
    for asset in _ASSETS:
        destination = (font_dir if asset.family else license_dir) / asset.name
        shutil.copyfile(source / asset.name, destination)
        destination.chmod(0o644)
    subprocess.run(["fc-cache", "-f", str(font_dir)], check=True)
    for asset in _ASSETS:
        if not asset.family:
            continue
        matched = subprocess.run(
            ["fc-match", "-f", "%{file}\n", f"{asset.family}:lang=ja"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        if Path(matched).resolve() != (font_dir / asset.name).resolve():
            raise RuntimeError(
                f"fontconfig did not select the installed {asset.family}: {matched}"
            )


def _main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    actions = parser.add_subparsers(dest="action", required=True)
    stage = actions.add_parser("stage")
    stage.add_argument("context", type=Path)
    stage.add_argument("--cache", type=Path)
    stage.add_argument("--offline", action="store_true")
    install = actions.add_parser("install")
    install.add_argument("source", type=Path)
    install.add_argument("--root", type=Path, default=Path("/"))
    args = parser.parse_args()
    if args.action == "stage":
        staged = _stage_assets(args.context, cache=args.cache, offline=args.offline)
        print(json.dumps({"context": str(staged), "upstream_commit": _COMMIT}))
    else:
        _install_assets(args.source, root=args.root)


if __name__ == "__main__":
    _main()
