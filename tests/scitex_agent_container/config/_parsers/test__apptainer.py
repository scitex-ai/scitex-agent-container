"""Tests for ``config._parsers._apptainer.parse_apptainer``.

Each test pins exactly one observable behaviour of the parser. The
``spec.apptainer`` block is optional: missing / non-dict / per-field
defaults all collapse to a zero-value ``ApptainerSpec``. ``binds``
accepts both the new ``host:container[:mode]`` shorthand strings and
the legacy ``{src, dst, mode}`` dict form (normalised to strings);
``env`` / ``environment`` / ``raw_args`` stringify their leaf values.
``nv`` / ``rocm`` / ``overlay`` / ``post`` / ``def_file`` are pass-through.

TQ cleanup: module docstring summarises intent (TQ001); every test
carries AAA markers (TQ002); descriptive names spell out the verified
behaviour (TQ003); each test asserts exactly one fact (TQ007).
Same-shape default-field invariants over one arrange/act collapse into
``pytest.parametrize`` over ``(attr, expected)`` pairs.
"""

from __future__ import annotations

import pytest

from scitex_agent_container.config._parsers._apptainer import parse_apptainer

# ---------------------------------------------------------------------------
# Missing / non-dict apptainer block → default ApptainerSpec
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("attr", "expected"),
    [
        ("image", ""),
        ("binds", []),
        ("env", {}),
        ("raw_args", []),
        ("container_workdir", "/work"),
        ("post", ""),
        ("environment", {}),
        ("def_file", ""),
        ("nv", False),
        ("rocm", False),
        ("overlay", ""),
    ],
)
def test_missing_block_yields_default_field(attr, expected):
    # Arrange
    spec: dict = {}
    # Act
    result = parse_apptainer(spec)
    # Assert
    assert getattr(result, attr) == expected


def test_non_dict_apptainer_value_yields_empty_image():
    # Arrange
    spec = {"apptainer": ["not", "a", "dict"]}
    # Act
    result = parse_apptainer(spec)
    # Assert
    assert result.image == ""


def test_non_dict_apptainer_value_yields_empty_binds():
    # Arrange
    spec = {"apptainer": ["not", "a", "dict"]}
    # Act
    result = parse_apptainer(spec)
    # Assert
    assert result.binds == []


# ---------------------------------------------------------------------------
# image + container_workdir
# ---------------------------------------------------------------------------


def test_image_field_is_round_tripped_from_spec():
    # Arrange
    spec = {"apptainer": {"image": "img.sif", "container_workdir": "/srv/app"}}
    # Act
    result = parse_apptainer(spec)
    # Assert
    assert result.image == "img.sif"


def test_container_workdir_field_is_round_tripped_from_spec():
    # Arrange
    spec = {"apptainer": {"image": "img.sif", "container_workdir": "/srv/app"}}
    # Act
    result = parse_apptainer(spec)
    # Assert
    assert result.container_workdir == "/srv/app"


def test_empty_container_workdir_falls_back_to_slash_work():
    # Arrange
    spec = {"apptainer": {"container_workdir": ""}}
    # Act
    result = parse_apptainer(spec)
    # Assert
    assert result.container_workdir == "/work"


# ---------------------------------------------------------------------------
# binds — shorthand strings, dict normalisation, defensive coercion
# ---------------------------------------------------------------------------


def test_binds_string_shorthand_is_preserved_verbatim():
    # Arrange
    spec = {"apptainer": {"binds": ["/h:/c", "/x:/y:ro"]}}
    # Act
    result = parse_apptainer(spec)
    # Assert
    assert result.binds == ["/h:/c", "/x:/y:ro"]


def test_binds_dict_form_is_normalised_to_shorthand_strings():
    # Arrange
    spec = {
        "apptainer": {
            "binds": [
                {"src": "/h", "dst": "/c"},
                {"src": "/h2", "dst": "/c2", "mode": "ro"},
                {"src": "", "dst": "/skip"},  # missing src dropped
                {"src": "/skipdst", "dst": ""},  # missing dst dropped
            ]
        }
    }
    # Act
    result = parse_apptainer(spec)
    # Assert
    assert result.binds == ["/h:/c", "/h2:/c2:ro"]


def test_binds_non_list_value_yields_empty_list():
    # Arrange
    spec = {"apptainer": {"binds": "not-a-list"}}
    # Act
    result = parse_apptainer(spec)
    # Assert
    assert result.binds == []


def test_binds_empty_string_entries_are_skipped():
    # Arrange
    spec = {"apptainer": {"binds": ["", "/a:/b"]}}
    # Act
    result = parse_apptainer(spec)
    # Assert
    assert result.binds == ["/a:/b"]


# ---------------------------------------------------------------------------
# env (engine-scoped) — stringify keys + values, defensive coercion
# ---------------------------------------------------------------------------


def test_env_dict_keys_and_values_are_stringified():
    # Arrange
    spec = {"apptainer": {"env": {"K": 1, 2: "v"}}}
    # Act
    result = parse_apptainer(spec)
    # Assert
    assert result.env == {"K": "1", "2": "v"}


def test_env_non_dict_value_yields_empty_dict():
    # Arrange
    spec = {"apptainer": {"env": ["bad"]}}
    # Act
    result = parse_apptainer(spec)
    # Assert
    assert result.env == {}


# ---------------------------------------------------------------------------
# environment (def-file %environment block) — same coercion rules
# ---------------------------------------------------------------------------


def test_environment_dict_values_are_stringified():
    # Arrange
    spec = {"apptainer": {"environment": {"A": 1}}}
    # Act
    result = parse_apptainer(spec)
    # Assert
    assert result.environment == {"A": "1"}


def test_environment_non_dict_value_yields_empty_dict():
    # Arrange
    spec = {"apptainer": {"environment": ["x"]}}
    # Act
    result = parse_apptainer(spec)
    # Assert
    assert result.environment == {}


# ---------------------------------------------------------------------------
# raw_args — stringify list entries, defensive coercion
# ---------------------------------------------------------------------------


def test_raw_args_list_entries_are_stringified():
    # Arrange
    spec = {"apptainer": {"raw_args": ["--cleanenv", 42]}}
    # Act
    result = parse_apptainer(spec)
    # Assert
    assert result.raw_args == ["--cleanenv", "42"]


def test_raw_args_non_list_value_yields_empty_list():
    # Arrange
    spec = {"apptainer": {"raw_args": "no"}}
    # Act
    result = parse_apptainer(spec)
    # Assert
    assert result.raw_args == []


# ---------------------------------------------------------------------------
# Pass-through scalar flags: nv / rocm / overlay / post / def_file
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("attr", "expected"),
    [
        ("nv", True),
        ("rocm", True),
        ("overlay", "/over.img"),
        ("post", "apt update"),
        ("def_file", "/d.def"),
    ],
)
def test_apptainer_scalar_flag_is_passed_through(attr, expected):
    # Arrange
    spec = {
        "apptainer": {
            "nv": True,
            "rocm": True,
            "overlay": "/over.img",
            "post": "apt update",
            "def_file": "/d.def",
        }
    }
    # Act
    result = parse_apptainer(spec)
    # Assert
    assert getattr(result, attr) == expected
