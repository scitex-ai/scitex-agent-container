# -*- coding: utf-8 -*-
# File: tests/integration/image_build_hooks/test_deny_raw_apptainer_build.py
"""The guard must be able to FAIL, and must refuse only what is ours.

Two halves, and both matter equally. A guard nobody has seen refuse
anything is indistinguishable from one that cannot refuse; a guard that
blocks unrelated builds gets disabled, and then the real rule is gone with
it. So every refusal case has an allowance case beside it.
"""

from __future__ import annotations

import os
import subprocess

from .conftest import ALLOW, DENY, HOOK, RECIPES_DIR


# ---------------------------------------------------------------------------
# Refusals
# ---------------------------------------------------------------------------


def test_refuses_raw_build_of_sac_recipe(run_hook, sac_like_recipe):
    # Arrange
    command = f"apptainer build out.sif {sac_like_recipe}"
    # Act
    result = run_hook(command)
    # Assert
    assert result.returncode == DENY


def test_refuses_the_real_shipped_base_recipe(run_hook):
    # Arrange
    command = f"apptainer build sac-base.sif {RECIPES_DIR / 'apptainer-base.def'}"
    # Act
    result = run_hook(command)
    # Assert
    assert result.returncode == DENY


def test_refuses_every_real_shipped_recipe(run_hook):
    # Arrange
    recipes = sorted(RECIPES_DIR.glob("apptainer-*.def"))
    # Act
    codes = {
        r.name: run_hook(f"apptainer build out.sif {r}").returncode
        for r in recipes
    }
    # Assert
    assert codes == {r.name: DENY for r in recipes}


def test_refuses_singularity_spelling_too(run_hook, sac_like_recipe):
    # Arrange
    command = f"singularity build out.sif {sac_like_recipe}"
    # Act
    result = run_hook(command)
    # Assert
    assert result.returncode == DENY


def test_refuses_build_behind_a_global_flag(run_hook, sac_like_recipe):
    # Arrange
    command = f"apptainer --debug build out.sif {sac_like_recipe}"
    # Act
    result = run_hook(command)
    # Assert
    assert result.returncode == DENY


def test_refuses_the_sandbox_form_the_def_advertises(run_hook, sac_like_recipe):
    # Arrange — apptainer-base.def's own header comment teaches this spelling
    command = f"apptainer build --sandbox base/ {sac_like_recipe}"
    # Act
    result = run_hook(command)
    # Assert
    assert result.returncode == DENY


def test_refuses_a_fully_demoted_raw_build(run_hook, sac_like_recipe):
    # Arrange — this exact command passes enforce_heavy_job_demotion.sh,
    # which judges only nice'ing; this hook exists to close that gap.
    command = (
        "nice -n 19 ionice -c 2 -n 7 apptainer build "
        f"out.sif {sac_like_recipe}"
    )
    # Act
    result = run_hook(command)
    # Assert
    assert result.returncode == DENY


def test_sees_through_quoted_bash_c_wrapper(run_hook, sac_like_recipe):
    # Arrange — spartan-sif-bake.sh:275's shape: absolute argv[0], and the
    # build visible only INSIDE a quoted argument. An argv[0]-only matcher
    # misses this, and so would miss a hand-typed workaround spelled the
    # same way.
    command = (
        f"bash -c 'exec /usr/bin/apptainer build --force p.sif {sac_like_recipe}'"
    )
    # Act
    result = run_hook(command)
    # Assert
    assert result.returncode == DENY


def test_refuses_when_only_the_recipe_directory_is_named(run_hook):
    # Arrange — file unreadable from here; the path is the only signal
    command = (
        "apptainer build out.sif "
        "/opt/x/scitex_agent_container/containers/apptainer-scitex.def"
    )
    # Act
    result = run_hook(command)
    # Assert
    assert result.returncode == DENY


def test_refuses_a_remote_build_naming_the_sif_directory(run_hook):
    # Arrange
    command = (
        "ssh spartan 'apptainer build "
        "~/.scitex/agent-container/containers/sac-base.sif recipe.def'"
    )
    # Act
    result = run_hook(command)
    # Assert
    assert result.returncode == DENY


# ---------------------------------------------------------------------------
# The discriminator keys on the label KEY, never the known value set
# ---------------------------------------------------------------------------


def test_matches_label_key_not_known_layer_values(run_hook, tmp_path):
    # Arrange — a stage name that exists in no current mapping. A matcher
    # spelled `org\.scitex\.layer (base|scitex|proxy)` would pass its own
    # tests and silently stop guarding once the .def restructuring lands.
    recipe = tmp_path / "07-some-future-stage.def"
    recipe.write_text(
        "Bootstrap: localimage\n%labels\n    org.scitex.layer python-pkgs\n",
        encoding="utf-8",
    )
    # Act
    result = run_hook(f"apptainer build out.sif {recipe}")
    # Assert
    assert result.returncode == DENY


def test_ignores_the_recipe_filename_entirely(run_hook, tmp_path):
    # Arrange — sac's filename, but somebody else's content
    impostor = tmp_path / "apptainer-base.def"
    impostor.write_text(
        "Bootstrap: docker\nFrom: alpine:3.20\n", encoding="utf-8"
    )
    # Act
    result = run_hook(f"apptainer build out.sif {impostor}")
    # Assert
    assert result.returncode == ALLOW


# ---------------------------------------------------------------------------
# Allowances — unrelated work must keep running
# ---------------------------------------------------------------------------


def test_allows_build_of_an_unrelated_recipe(run_hook, unrelated_recipe):
    # Arrange
    command = f"apptainer build out.sif {unrelated_recipe}"
    # Act
    result = run_hook(command)
    # Assert
    assert result.returncode == ALLOW


def test_allows_build_from_a_docker_uri(run_hook):
    # Arrange
    command = "apptainer build myimage.sif docker://ubuntu:24.04"
    # Act
    result = run_hook(command)
    # Assert
    assert result.returncode == ALLOW


def test_allows_the_sanctioned_cli_invocation(run_hook):
    # Arrange
    command = "sac image build base -y"
    # Act
    result = run_hook(command)
    # Assert
    assert result.returncode == ALLOW


def test_allows_the_sanctioned_bake_wrapper(run_hook):
    # Arrange — spartan-sif-bake.sh does its own $CTX staging, so it is
    # exempt on merit rather than by accident.
    command = (
        "bash src/scitex_agent_container/containers/spartan-sif-bake.sh "
        "--layer base"
    )
    # Act
    result = run_hook(command)
    # Assert
    assert result.returncode == ALLOW


def test_allows_read_only_label_inspection(run_hook):
    # Arrange
    command = (
        "apptainer inspect --labels "
        "~/.scitex/agent-container/containers/sac-base.sif"
    )
    # Act
    result = run_hook(command)
    # Assert
    assert result.returncode == ALLOW


def test_allows_read_only_deffile_inspection(run_hook):
    # Arrange
    command = (
        "apptainer inspect --deffile "
        "~/.scitex/agent-container/containers/sac-base.sif"
    )
    # Act
    result = run_hook(command)
    # Assert
    assert result.returncode == ALLOW


def test_allows_exec_whose_argv_contains_the_word_build(run_hook):
    # Arrange — `build` here is make's target, not apptainer's verb
    command = "apptainer exec img.sif make build"
    # Act
    result = run_hook(command)
    # Assert
    assert result.returncode == ALLOW


def test_allows_a_non_bash_tool_call(run_hook):
    # Arrange
    command = ""
    # Act
    result = run_hook(command)
    # Assert
    assert result.returncode == ALLOW


# ---------------------------------------------------------------------------
# The refusal must be actionable
# ---------------------------------------------------------------------------


def test_refusal_names_the_replacement_command(run_hook, sac_like_recipe):
    # Arrange
    command = f"apptainer build out.sif {sac_like_recipe}"
    # Act
    result = run_hook(command)
    # Assert
    assert "sac image build base" in result.stderr


def test_refusal_names_the_triggering_file(run_hook, sac_like_recipe):
    # Arrange
    command = f"apptainer build out.sif {sac_like_recipe}"
    # Act
    result = run_hook(command)
    # Assert
    assert str(sac_like_recipe) in result.stderr


def test_refusal_names_the_override_env_var(run_hook, sac_like_recipe):
    # Arrange
    command = f"apptainer build out.sif {sac_like_recipe}"
    # Act
    result = run_hook(command)
    # Assert
    assert "SAC_ALLOW_RAW_IMAGE_BUILD=1" in result.stderr


def test_refusal_explains_the_staging_reason(run_hook, sac_like_recipe):
    # Arrange
    command = f"apptainer build out.sif {sac_like_recipe}"
    # Act
    result = run_hook(command)
    # Assert
    assert "scitex-agent-container-src" in result.stderr


# ---------------------------------------------------------------------------
# Overrides
# ---------------------------------------------------------------------------


def test_honours_the_inline_bypass_marker(run_hook, sac_like_recipe):
    # Arrange
    command = (
        f"apptainer build out.sif {sac_like_recipe} "
        "# hook-bypass: raw-apptainer-build"
    )
    # Act
    result = run_hook(command)
    # Assert
    assert result.returncode == ALLOW


def test_honours_the_bypass_environment_variable(run_hook, sac_like_recipe):
    # Arrange
    env = dict(os.environ, SAC_ALLOW_RAW_IMAGE_BUILD="1")
    # Act
    result = run_hook(f"apptainer build out.sif {sac_like_recipe}", env=env)
    # Assert
    assert result.returncode == ALLOW


# ---------------------------------------------------------------------------
# The asset's own self-test stays green
# ---------------------------------------------------------------------------


def test_bundled_self_test_suite_passes():
    # Arrange
    argv = ["bash", str(HOOK), "--self-test"]
    # Act
    result = subprocess.run(argv, capture_output=True, text=True, timeout=180)
    # Assert
    assert result.returncode == 0, result.stdout

# EOF
