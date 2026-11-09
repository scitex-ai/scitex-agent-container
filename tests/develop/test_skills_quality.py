"""Enforces SciTeX skills quality checklist §1–§4.
Canonical: scitex-dev general/03_interface/04_skills/12_quality-checklist.md.
Use the audit gate's verified checkout; a tests-directory anchor checks nothing.
"""

import pytest

pytest.importorskip("scitex_dev._skills_quality_pytest")

from scitex_dev._skills_quality_pytest import (  # noqa: E402
    make_skill_quality_tests,
)
from tests.develop.test_audit import _verified_repo_root  # noqa: E402

test_skills_quality = make_skill_quality_tests(
    package_root=_verified_repo_root()
)
