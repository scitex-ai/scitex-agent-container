"""Enforces SciTeX skills quality checklist §1–§4.
Canonical: src/scitex/_skills/general/21_scitex-package-quality-checklist.md
"""
import pytest
from pathlib import Path

try:
    from scitex_dev._skills_quality_pytest import make_skill_quality_tests
    test_skills_quality = make_skill_quality_tests(
        package_root=Path(__file__).resolve().parents[1]
    )
except ImportError:
    pytest.skip(
        "scitex_dev._skills_quality_pytest not available — skipping skills quality checks",
        allow_module_level=True,
    )
