"""Tests for config._parsers._apptainer.parse_apptainer."""

from __future__ import annotations

from scitex_agent_container.config._parsers._apptainer import parse_apptainer


def test_missing_returns_empty_spec():
    a = parse_apptainer({})
    assert a.image == ""
    assert a.binds == []
    assert a.env == {}
    assert a.raw_args == []
    assert a.container_workdir == "/work"
    assert a.post == ""
    assert a.environment == {}
    assert a.def_file == ""
    assert a.nv is False
    assert a.rocm is False
    assert a.overlay == ""


def test_non_dict_raw_returns_default():
    a = parse_apptainer({"apptainer": ["not", "a", "dict"]})
    assert a.image == ""
    assert a.binds == []


def test_image_and_workdir_round_trip():
    a = parse_apptainer(
        {
            "apptainer": {
                "image": "img.sif",
                "container_workdir": "/srv/app",
            }
        }
    )
    assert a.image == "img.sif"
    assert a.container_workdir == "/srv/app"


def test_empty_container_workdir_defaults_to_slash_work():
    # Empty string falls back via the `or "/work"` guard
    a = parse_apptainer({"apptainer": {"container_workdir": ""}})
    assert a.container_workdir == "/work"


def test_binds_string_shorthand_preserved():
    a = parse_apptainer({"apptainer": {"binds": ["/h:/c", "/x:/y:ro"]}})
    assert a.binds == ["/h:/c", "/x:/y:ro"]


def test_binds_dict_normalised_with_and_without_mode():
    a = parse_apptainer(
        {
            "apptainer": {
                "binds": [
                    {"src": "/h", "dst": "/c"},
                    {"src": "/h2", "dst": "/c2", "mode": "ro"},
                    {"src": "", "dst": "/skip"},  # missing src dropped
                    {"src": "/skipdst", "dst": ""},  # missing dst dropped
                ]
            }
        }
    )
    assert a.binds == ["/h:/c", "/h2:/c2:ro"]


def test_binds_non_list_yields_empty():
    a = parse_apptainer({"apptainer": {"binds": "not-a-list"}})
    assert a.binds == []


def test_binds_skips_empty_string():
    a = parse_apptainer({"apptainer": {"binds": ["", "/a:/b"]}})
    assert a.binds == ["/a:/b"]


def test_env_dict_keys_and_values_stringified():
    a = parse_apptainer({"apptainer": {"env": {"K": 1, 2: "v"}}})
    assert a.env == {"K": "1", "2": "v"}


def test_env_non_dict_yields_empty():
    a = parse_apptainer({"apptainer": {"env": ["bad"]}})
    assert a.env == {}


def test_environment_block_stringified():
    a = parse_apptainer({"apptainer": {"environment": {"A": 1}}})
    assert a.environment == {"A": "1"}


def test_environment_non_dict_yields_empty():
    a = parse_apptainer({"apptainer": {"environment": ["x"]}})
    assert a.environment == {}


def test_raw_args_list_stringified():
    a = parse_apptainer({"apptainer": {"raw_args": ["--cleanenv", 42]}})
    assert a.raw_args == ["--cleanenv", "42"]


def test_raw_args_non_list_yields_empty():
    a = parse_apptainer({"apptainer": {"raw_args": "no"}})
    assert a.raw_args == []


def test_gpu_and_overlay_flags():
    a = parse_apptainer(
        {
            "apptainer": {
                "nv": True,
                "rocm": True,
                "overlay": "/over.img",
                "post": "apt update",
                "def_file": "/d.def",
            }
        }
    )
    assert a.nv is True
    assert a.rocm is True
    assert a.overlay == "/over.img"
    assert a.post == "apt update"
    assert a.def_file == "/d.def"
