#!/usr/bin/env python3
# File: src/hatch_build.py

"""Bake the build stamp into the distribution (hatchling build hook).

A pip-installed copy has no ``.git``, so build time is the ONLY moment the
commit can be captured. This hook writes
``src/scitex_agent_container/_provenance/_build_info.py`` before the
wheel/sdist is assembled, and force-includes it so a VCS-ignored generated
file still ships.

WHY IT LIVES UNDER ``src/`` AND NOT AT THE REPO ROOT (hatchling's default
location, wired up via ``tool.hatch.build.hooks.custom.path``): it sits
beside the package rather than inside it, so it is NOT packaged into the
wheel — a module that imports ``hatchling`` at top level has no business
shipping to runtime, where the build frontend does not exist.

The logic itself lives in the package (``_provenance._stamp``) so it is
unit-testable without a build frontend; this file is only the adapter. It
loads that module by FILE PATH rather than importing
``scitex_agent_container._provenance``, because at build time the package
is not installed and going through the package ``__init__`` would trigger
``importlib.metadata`` lookups for a distribution that does not exist yet.

Run it directly to see what a build would bake, without building::

    $ python src/hatch_build.py
    version:    0.21.13
    commit:     082d2fe949118fe0b13e7bac2b5ecd966846167b (git)
    code_hash:  c6c986b1f49b77c7175eaea5cf74198d
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

try:
    from hatchling.builders.hooks.plugin.interface import BuildHookInterface
except ImportError:  # stx-allow: fallback (reason: hatchling is a build-time-only dep; guarding it keeps the `python src/hatch_build.py` dry-run usable on a machine with no build frontend)
    BuildHookInterface = object  # type: ignore[assignment,misc]

_ROOT = Path(__file__).resolve().parent.parent
_PKG_DIR = _ROOT / "src" / "scitex_agent_container"
_PROV_DIR = _PKG_DIR / "_provenance"
_PACKAGE_ALIAS = "_sac_build_provenance"


def load_stamp_module():
    """Load ``_provenance._stamp`` off disk, relative imports intact.

    A bare namespace package is registered first so ``_stamp``'s
    ``from ._git import ...`` resolves, WITHOUT executing the real package
    ``__init__``.
    """
    if _PACKAGE_ALIAS not in sys.modules:
        pkg_spec = importlib.util.spec_from_file_location(
            _PACKAGE_ALIAS,
            _PROV_DIR / "__init__.py",
            submodule_search_locations=[str(_PROV_DIR)],
        )
        sys.modules[_PACKAGE_ALIAS] = importlib.util.module_from_spec(pkg_spec)

    name = f"{_PACKAGE_ALIAS}._stamp"
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, _PROV_DIR / "_stamp.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _declared_version() -> str:
    """Read ``version = "..."`` out of pyproject without a TOML dep."""
    for line in (_ROOT / "pyproject.toml").read_text(encoding="utf-8").splitlines():
        if line.startswith("version"):
            return line.split("=", 1)[1].strip().strip('"')
    return "0.0.0+unknown"


class BuildStampHook(BuildHookInterface):
    """Write ``_build_info.py`` and force-include it into the artifact."""

    PLUGIN_NAME = "custom"

    def initialize(self, version, build_data):
        stamp_mod = load_stamp_module()
        stamp = stamp_mod.compute_stamp(
            root=_ROOT,
            package_dir=_PKG_DIR,
            version=self.metadata.version,
        )
        target = _PROV_DIR / stamp_mod.BUILD_INFO_NAME
        target.write_text(stamp_mod.render_module(stamp), encoding="utf-8")

        self.app.display_info(
            f"build stamp: commit={stamp.get('commit') or 'unknown'} "
            f"({stamp.get('commit_source')}) code_hash={stamp.get('code_hash')}"
        )

        # _build_info.py is git-ignored (it is generated) and hatchling skips
        # VCS-ignored files by default — force-include it, or the artifact
        # ships with no stamp, which would defeat the entire hook.
        inside = (
            f"scitex_agent_container/_provenance/{stamp_mod.BUILD_INFO_NAME}"
            if self.target_name == "wheel"
            else target.relative_to(_ROOT).as_posix()
        )
        build_data.setdefault("force_include", {})[str(target)] = inside


def main() -> None:
    """Dry-run: print the stamp a build would bake, writing nothing."""
    stamp_mod = load_stamp_module()
    stamp = stamp_mod.compute_stamp(
        root=_ROOT, package_dir=_PKG_DIR, version=_declared_version()
    )
    print(f"version:    {stamp['version']}")
    print(f"commit:     {stamp['commit'] or 'unknown'} ({stamp['commit_source']})")
    print(f"code_hash:  {stamp['code_hash']}")
    print(f"built_at:   {stamp['built_at']}")


if __name__ == "__main__":
    main()

# EOF
