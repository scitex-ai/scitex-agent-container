# Hermes uses the launching SAC installation

An existing image can contain an older SAC loader. Image SAC 0.29.8 rejects
the authored Hermes `max_turns` and `goals` fields. Updating the host wheel
therefore must update the SAC code used by the owner and agent tools too.

Hermes launches now derive `sac` from the current Python installation's
standard scripts directory. The owner uses that interpreter, Apptainer's
`PREPEND_PATH` exposes its CLI to Hermes terminal children, and the builtin
SAC MCP command uses the same absolute entrypoint. `SAC_BIN` and
`SAC_BIN_IN_SIF` may identify that installation; selecting an unrelated
or older image CLI is refused. Hermes itself remains the image executable
at `/opt/hermes-agent/.venv/bin/hermes` so host Python tools cannot replace
the reviewed harness accidentally.

Before launch, the finalized explicit binds must expose the installed
Python prefix, standard library prefix, console script, and every
interpreter symlink hop at their original absolute paths. Existing host
home and scratch binds can satisfy this. A missing or shadowed installation
produces an actionable error; SAC does not create a wrapper, install a
second runtime environment, shadow individual packages, or rebuild the image.

Run `tests/integration/test_hermes_installed_sac_runtime.py` with
`SAC_TEST_HERMES_SIF` set to the reviewed image. The default reviewed Hermes
pin is `17c5fde5a3f3642262003cd6aa09d54cf4d11de3`; an explicitly reviewed
replacement can be supplied through `SAC_TEST_HERMES_PIN`. The test uses
disposable state and verifies the real Hermes subprocess environment.
