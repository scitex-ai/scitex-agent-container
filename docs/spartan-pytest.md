# Spartan pytest result integrity

The existing `sac pytest spartan run REPO@BRANCH` command submits, observes
and collects a remote test job. It is a writing operation, not a local health
check. This correction changes result qualification; it does not rename the
command, add retries, cancel jobs or change reservation, timeout, polling,
installation or test-selection defaults.

`--timeout` and `--poll-interval` accept positive integer seconds only.
Zero or negative values are usage errors, rejected before any upload or job
submission. Existing positive defaults and polling behavior are unchanged;
this does not add a whole-command timeout around SSH transport calls.

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
branch-based cloning, dependency-install fallback, terminal-log count
extraction and transport-call bounds retain their prior semantics. Exact
commit/job identity binding, complete dependency/coverage acceptance and
remote login/compute filesystem agreement require separate evidence.
No old artifact is rewritten and no failed job is retried automatically.


## Fresh submission directories

Fresh CLI submissions use `<scratch-root>/<uuid4-hex>/` for both `job.sbatch`
and `summary.json`. The namespace is allocated before upload and no longer
depends on wall-clock milliseconds. The CLI prints the planned directory
before upload so partial failures can be investigated without guessing it.
Client upload/fetch quoting is preserved, including supplied roots with
spaces. This does not qualify Slurm directive parsing or remote expansion.

Existing timestamp directories are not renamed, reused or deleted by this
change. A UUID is a request namespace, not a secret, lock, scheduler receipt
or artifact attestation. Real job/commit identity binding, branch resolution,
remote login/compute filesystem agreement and concurrent cluster execution remain separate
qualification work. No live submission or cleanup is needed to change this
client-side naming behavior.

## Submission receipts

The client accepts exactly one `Submitted batch job <positive ASCII integer>`
line from successful `sbatch` output. Other diagnostic lines may surround it;
their numbers are never interpreted as job IDs. Missing or multiple matching
receipt lines are refused, including repeated identical receipts. Noncanonical
output formats are not qualified by this parser.

This check happens after upload and submission. A refusal does not prove that
no job was created: inspect the retained script and scheduler records before
deciding what to do. The client neither retries submission nor polls a guessed
job ID. This receipt check is not cryptographic artifact/commit identity
binding and does not change the separate result-qualification requirements.

## Header input boundaries

The renderer rejects CR, LF and NUL in reservation names, scratch paths and
job labels, rather than inserting them into Slurm header lines. The CLI
reports a usage error before printing the planned path or contacting SSH.
Valid inputs retain the existing script and upload behavior. This check does
not qualify Slurm whitespace/path parsing, arbitrary header semantics or a
live scheduler; those remain separate from client-side field validation.

## Script upload boundary

The client sends script text on SSH stdin to a quoted `cat` destination;
it does not embed the script in a remote shell heredoc. Delimiter-like text
and shell expressions inside the job remain file contents during upload.
Only a successful write proceeds to `chmod` and submission. A failed write
may leave a partial file: this is not an atomic replacement or cleanup.

Owned local-shell tests exercise text preservation, delimiter-like content,
quoted paths and write failures with an inert `sbatch`. They do not execute
the uploaded job or qualify an actual SSH server, scheduler or target install.

## Remote scratch environment

The default `$SCRATCH/sac-pytest` resolves the leading `$SCRATCH` token from
the SSH host's environment through one fixed `printf` query before upload.
`${SCRATCH}` is also supported at the start of a root. Explicit paths are
preserved and need no environment lookup. This is not general shell expansion
or `eval`; other variables, substitutions and tildes are not expanded.

The remote value must be a nonempty absolute path without CR, LF or NUL.
Missing, malformed or failed reads stop before upload or submission, with no
local-path fallback. Header inputs are checked before the query as well.
Client-side quoting preserves spaces and literal metacharacters in the value;
Slurm header parsing and shared visibility between login and compute nodes
still require separate evidence. No real cluster environment is qualified by
the local-shell fixtures, and transport timeout/retry behavior is unchanged.
