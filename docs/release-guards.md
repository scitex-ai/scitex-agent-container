# Package release source and runtime guards

The tag publisher retains its existing wheel/sdist proof, Git source membership,
metadata/RECORD checks, three inline test witnesses, artifact transport witness,
and pre-OIDC/post-mint artifact verification. Its test, build and publish jobs
enter through `.github/ci/release-exec-in-sif.sh` before delegating to the
ordinary `exec-in-sif.sh` and unchanged inner drivers. The outer wrapper has an
explicit release-only PG image override; its registered whole-source hash
therefore changes and requires normal review. Ordinary discovery behavior is
retained when the override is absent.

Before any image Python or installer, the checked entry point sources
`release-store-environment.sh`. It removes inherited libpq (`PG*`), canonical
store and test DSNs, including Apptainer/Singularity aliases, then supplies an
explicit unavailable store/libpq floor. Its empty 0600 password file belongs
to a unique directory beneath the existing CI PostgreSQL scratch root; the
existing PostgreSQL age reaper owns that directory's cleanup. Scratch aliases
or a root owned by another UID are refused. Service selectors are removed,
rather than exported blank: libpq treats an empty service name as a lookup.
The qualified image has no service selector defaults.

The early external-directory pytest plugin preflight does not load this
repository's pytest configuration or conftest, so their later isolation alone
cannot protect that import window. The release floor remains unavailable;
this does **not** claim whole-suite default persistence coverage. Tests that
need SQL still explicitly acquire the proven owned PostgreSQL endpoint through
`pg_schema`, retaining the fleet-system-identifier refusal and per-test schema
lifecycle. Ordinary PR executors and inner drivers are unchanged by this floor.

The checked entry point verifies the whole qualified AA583 Python image,
PG18.6 server image and Apptainer bytes through `verify-release-runtime.sh`.
The fixed Python image digest is
`aa5836a6c317640d7e20f01eb79aa7385b3dca2f369b595065c50ba3dd34a7d5`;
the runtime digest is
`7bdb501fdddbb7264b4e4d6038e0ec27201f63e217519e5f7f0db950c9285ddd`.
The fixed PG18.6 server image digest is
`842a1fcf5abdc512af312c1527a0700c52e4b1a6b6e3e3dddc6edde69113ee23`
(157,646,848 bytes). Its existing qualification receipt is
`75e24f6fa3a80369b45e8c3d1029d940076109c92404d0d82a168be74859771b`.
A filename or executable bit alone is insufficient. Configured paths are
resolved, regular files are hashed, and their metadata must stay unchanged
during reading. No fallback runtime or image is admitted by this entry point.

For a read-only byte check from the source checkout, with the existing Actions
image/runtime variables set:

```bash
source .github/ci/verify-release-runtime.sh
sac_release_runtime
```

`release-identity.py check-source` then checks the tag and source before the
ordinary wrapper's PostgreSQL/reap side effects. The tag must match current
protected `main`, the workflow event commit, and the checkout. The existing
accepted-`develop` ancestry requirement remains. After a normal release PR,
ensure the resulting main commit is also accepted into develop history before
tagging; neither force-resetting history nor bypassing the check is required.
The existing before-OIDC revalidation reads this identity again.

Manual version input is passed as literal environment data and must match the
existing canonical `vX.Y.Z` grammar before checkout. All three supported release
minors remain unconditional. Their existing `SAC_TEST_PG_REQUIRED=1` contract
turns missing PostgreSQL coverage into failure. The checked entry point clears
inherited test DSNs so the ordinary wrapper must supply its newly owned cluster;
the fixture's fleet system-identifier refusal remains active. Release mode
uses the one verified PG image path rather than rediscovering it by glob,
rechecks PG/runtime bytes at actual PG use, and requires SQL to identify the
new owned data directory, actual server version 18.6 and writable state.
Provisioning failure is fatal in release mode for test/build/publish alike.

Focused regression checks:

```bash
pytest tests/integration/test_release_source_guards.py \
  tests/integration/test_release_runtime_guards.py \
  tests/integration/test_release_private_pg.py
```

The source controls use real local Git objects behind the explicit query seam.
The portable fixture integration starts fresh owned PostgreSQL 16 clusters
and executes
schema/table/data operations, and verifies unavailable/read-only refusal. The
CI image declares these server binaries. No fleet database is a test target.
The distinct native release-seam probe
uses the same outer wrapper and fixed PG18.6 image, executes actual SQL and
unavailable/read-only controls, and stops its observed owned server. PG16
fixture evidence is not presented as PG18.6 image qualification.
Runtime-image build qualification remains an independently recorded operation;
these package publisher checks do not substitute for its evidence.
