#!/bin/bash
# -*- coding: utf-8 -*-
# File: src/scitex_agent_container/_baseline_assets/secret_read_hooks/deny_secret_file_read.sh
#
# Description: PreToolUse guard for Read, NotebookRead, Grep, Glob and Bash.
# Refuses tool calls that would read SECRET MATERIAL from disk -- SSH/GPG
# private keys, ~/.pw GPG payloads, the dotfiles plaintext token store,
# *.pem/*.p12/*.pfx, git/gh/netrc/docker/aws credential files, and the
# scitex secrets signing store -- because whatever a tool reads lands in the
# session transcript, which is durable and leaves the machine. Before this
# hook only `deny_broad_env_dump.sh` existed, and it covers Bash env dumps
# only: nothing stopped a Read/Grep/Glob (or `cat`) of a key file.
#
# DECLARED BY scitex-agent-container in `_claude_hooks_plugin.py`, APPLIED
# BY scitex-dev through the `scitex_dev.hooks` entry-point group. Deploy all
# four files of this bundle (this wrapper + secret_file_read_core.py +
# secret_file_read_paths.py + secret_file_read_bash.py) together into
# $HOME/.claude/hooks/pre-tool-use/. The decision logic, path catalogue and
# covered / not-covered table live in the Python siblings and README.md.
#
# If the engine or python3 is missing the hook FAILS OPEN, like every other
# sac PreToolUse guard: a broken hook must not wedge every file tool.
#
# Override (operator only, logged): export SAC_ALLOW_SECRET_FILE_READ=1 in
# the harness launch environment. There is no inline bypass marker.
#
# Verify at any time:  bash deny_secret_file_read.sh --self-test

set -u

THIS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CORE="$THIS_DIR/secret_file_read_core.py"

[[ -f "$CORE" ]] || exit 0
command -v python3 >/dev/null 2>&1 || exit 0

exec python3 "$CORE" "$@"

# EOF
