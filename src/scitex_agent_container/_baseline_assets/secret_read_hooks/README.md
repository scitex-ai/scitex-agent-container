# Secret-read hooks

`deny_secret_file_read.sh` is a Claude Code `PreToolUse` guard on
`Read`, `NotebookRead`, `Grep`, `Glob` and `Bash`. It refuses any call that
would read **secret material from disk**, because whatever a tool reads
lands in the session transcript. The transcript is durable and leaves the
machine. Before this hook, the only secret guard was `deny_broad_env_dump.sh`,
which covers Bash environment dumps only. Nothing stopped a `Read` of
`~/.ssh/id_ed25519`.

Agents never need these files. Credentials reach them as environment
variables, which sac's payload system fills from the operator's secret pool
at agent start. The deny message sends the agent there
(`~/.claude/hooks/lib/env-check VARNAME` checks that a variable is set without
printing it) and tells it to ask the operator for anything missing.

The rule is declared as `sac.no-secret-file-read` in
`scitex_agent_container._claude_hooks_plugin` and scitex-dev materialises it.
Deploy all four files together into `$HOME/.claude/hooks/pre-tool-use/`:

| File | Role |
|---|---|
| `deny_secret_file_read.sh` | wrapper; fails open if python3 or the engine is missing |
| `secret_file_read_core.py` | payload dispatch, native tools, deny message, `--self-test` |
| `secret_file_read_paths.py` | path normalisation and the secret-path catalogue |
| `secret_file_read_bash.py` | which paths a Bash command reads |

## What counts as secret

| Target | Rule |
|---|---|
| `~/.pw/**` | every file in the GPG payload store |
| `~/.gnupg/**` | every file in the key ring |
| `.ssh/id_*` (but not `*.pub`), `.ssh/*_key`, `ssh_host_*_key` | SSH private keys |
| `/var/lib/scitex-secrets-signing/**` | signing store |
| `*/.bash.d/secrets/access_tokens/**` | dotfiles plaintext token store |
| `*/.bash.d/secrets/*.{token,github-token,gitea-token,a2a-token,password}` | dotfiles token files |
| `*.pem`, `*.p12`, `*.pfx` | key / certificate bundles, except CA bundles (`/etc/ssl/certs`, `/etc/pki/...`, `/usr/share/ca-certificates`, `cacert.pem`, `ca-bundle.pem`) |
| `.git-credentials`, `.netrc`, `.config/gh/hosts.yml`, `.docker/config.json`, `.aws/credentials` | credential files |

Matching is by path component, so it works under any home directory
(for example a host home mounted into a container), not only `$HOME`.

## Normalisation

Every operand is expanded and then judged. The steps are: expand `~`,
`~user`, `$HOME` and `${HOME}`; anchor relative paths at the payload `cwd`;
collapse `.` and `..`; and resolve symlinks with `realpath` for paths that
exist. `{a,b}` braces are expanded. A wildcard counts by what it could
expand to: `~/.ssh/*` and `~/.ssh/id_*` are refused, and `~/.ssh/*.pub` is
allowed. In Bash, `cd DIR` moves the anchor for the rest of the command, so
`cd ~/.ssh && cat id_rsa` is judged as `cat ~/.ssh/id_rsa`.

## Per tool

* **Read / NotebookRead**: `file_path` / `notebook_path` is judged as a file.
* **Grep**: refused when `path` is a secret file (anything inside `.pw`,
  `.gnupg`, the signing store or `access_tokens` counts), or when it is
  a secret-holding directory (`.ssh`, `.gnupg`, `.pw`, `.bash.d/secrets`,
  `.aws`, `.docker`, `.config/gh`, signing store). It is also refused when
  `glob` targets secret files. A glob without a `/` matches at any depth,
  like ripgrep's.
* **Glob**: `path` + `pattern` is judged as a pattern.
* **Bash**: tokenised with `shlex`. An operand counts as read when:
  * the program is a content reader (`cat head tail less more grep rg sed awk
    cut sort diff jq cp ln scp rsync base64 xxd od hexdump strings tar zip
    gzip openssl dd curl wget tee ...`), including after `sudo`, `env`,
    `timeout`, `nice`, `xargs` or `find -exec`; or
  * it follows a `<` redirection, whatever the program; or
  * the program is `gpg` decrypting or exporting secret keys; or
  * it appears inside `bash -c` / `sh -c` / `eval` strings (recursion is
    bounded).

  Option values (`--file=X`), `curl -d @X`, `dd if=X` and `host:path` are
  also checked. Directory operands count only for programs that walk
  directories (`grep -r`, `rg`, `tar`, `cp -r`, `rsync`, `zip`).

## Deliberately allowed

`ls ~/.ssh`, `stat`, `test -f`, `wc -c FILE` (metadata, not contents);
`cat ~/.ssh/id_*.pub`, `~/.ssh/config`, `~/.ssh/known_hosts`;
`gpg --list-keys` and `gpg --homedir ~/.gnupg --list-keys`; CA bundles;
`echo ~/.ssh/id_rsa` (the path is data); URLs passed to `curl`.

## Known gaps (not covered)

* Interpreters reading the file themselves: `python -c "open(...)"`,
  `node -e`, `perl -ne`. They are not content readers in the list above.
* Paths built from variables other than `HOME` (`cat "$KEYDIR/id_rsa"`) or
  from command substitution.
* Wildcards that spell the marker itself (`~/.ss*/id_rsa`).
* Searching from an ancestor without naming the secret: `rg --hidden token ~`,
  `tar czf x.tgz ~`, or a Grep tool call rooted at `$HOME`. These are judged
  by their root, which is not secret.
* `ssh host 'cat ~/.ssh/id_rsa'` (remote command strings are not parsed),
  and `sudo -u USER cat ...` (the `-u` value is taken as the program).
* A symlink created and read in the same Bash command (`realpath` runs before
  the link exists). `ln` of a secret is refused, which closes the common
  spelling of this.
* `*.key` and other private-key extensions not listed above.

## Override

The operator can set `SAC_ALLOW_SECRET_FILE_READ=1` in the harness launch
environment. This follows the `SAC_ALLOW_<RULE>=1` convention of
`deny_swallowed_exit_code.sh`. A Bash tool call cannot set it. Every
override is logged to `$SCITEX_AGENT_CONTAINER_HOOK_LOG_PATH` (default:
`.deny_secret_file_read.sh.log` next to the hook), as is every block. The
log records the tool, path and label, never the command text. There is
**no** inline `# hook-bypass` marker, because an agent could type one.

## Codex

Codex fires `PreToolUse` for its shell tool (`exec_command`) with
`tool_name: "Bash"` and `tool_input.command`. Exit code 2 plus a stderr
reason is a valid deny there too. Codex has no `Read`/`Grep`/`Glob`
tools, so those matcher entries never fire under Codex. The `Bash` entry is
what protects a Codex pane, and the hooks block that
`runtimes/_apptainer_codex_exec.write_hooks_from` copies verbatim works
unchanged.

Verify at any time: `bash deny_secret_file_read.sh --self-test`.
