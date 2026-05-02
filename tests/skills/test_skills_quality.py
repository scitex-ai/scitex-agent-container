"""Enforces SciTeX skills quality checklist §1–§4.
Canonical: src/scitex/_skills/general/21_scitex-package-quality-checklist.md
"""

from pathlib import Path

import pytest

try:
    from scitex_dev._skills_quality_pytest import make_skill_quality_tests
except ImportError:  # stx-allow: fallback (reason: scitex-dev optional in install)
    make_skill_quality_tests = None

if make_skill_quality_tests is not None:
    test_skills_quality = make_skill_quality_tests(  # type: ignore[assignment]
        package_root=Path(__file__).resolve().parents[1]
    )
else:

    def test_skills_quality() -> None:  # type: ignore[no-redef]
        pytest.skip("scitex_dev._skills_quality_pytest not available")
