# Spartan pytest result integrity

The existing `sac pytest spartan run REPO@BRANCH` command submits, observes
and collects a remote test job. It is a writing operation, not a local health
check. This correction changes result qualification; it does not rename the
command, add retries, cancel jobs or change reservation, timeout, polling,
installation or test-selection defaults.

The CLI prints PASS only when the collected summary has explicit valid test
counts, pytest and log-capture exit codes of zero, at least one passing test,
and observed `COMPLETED` accounting. A failed `squeue` query, failed `sacct`
query or absent accounting result is an error, never inferred completion.
Accounting failures may leave the submitted job running; this command does
not add automatic cancellation or resubmission.

The generated Bash job records both pipeline exit codes immediately from
`PIPESTATUS`. This distinguishes pytest failure from failure to capture its
output and preserves the pipeline's nonzero job status. In particular,
[pytest documents exit 5 as no tests collected](https://docs.pytest.org/en/stable/reference/exit-codes.html),
which cannot become PASS just because the log contains zero failures.
[Slurm describes COMPLETED as successful execution](https://slurm.schedmd.com/job_state_codes.html);
a missing record is not that observation.

The Python summary helper still reads complete legacy count-only records;
the new optional exit fields default to `None`. Those counts do not establish
process success, and the CLI refuses to qualify such a record. Incomplete,
negative, mistyped or nonfinite summary values produce the existing error
result instead of silently becoming clean counts. A malformed-summary error
is a parser failure, not a newly invented pytest test failure.

The local tests use owned SSH executables and inert install/test tools around
the actual rendered Bash pipeline. They verify both result handling and
refusal cases without contacting a cluster. The existing real-Spartan test
still requires its explicit live opt-in. No live cluster execution, package
installation, scheduler availability or operator repository is qualified by
these fixtures.

This is not a replacement for required CI or release qualification. Existing
branch-based cloning, timestamp scratch paths, dependency-install fallback,
terminal-log count extraction and transport-call bounds retain their prior
semantics. Exact commit/job identity binding, complete dependency/coverage
acceptance and default remote scratch expansion require separate evidence.
No old artifact is rewritten and no failed job is retried automatically.
