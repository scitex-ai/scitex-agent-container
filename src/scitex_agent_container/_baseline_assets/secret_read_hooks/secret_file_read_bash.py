#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# File: src/scitex_agent_container/_baseline_assets/secret_read_hooks/secret_file_read_bash.py
"""Which secret paths a Bash command would READ.

Token-based (``shlex`` with shell punctuation), so a path that is only
DATA -- ``echo ~/.ssh/id_rsa``, a commit message -- is not a read. A
command counts as reading an operand when:

* its program (after wrappers like ``sudo``/``env``/``timeout``/``xargs``,
  or after ``find -exec``) is a content reader (:data:`READERS`), or
* the operand is the target of a ``<`` redirection (any program), or
* it is ``gpg`` decrypting / exporting secret keys, or
* it is ``bash -c`` / ``sh -c`` / ``eval`` of a string that does one of
  the above (bounded recursion).

``cd DIR`` inside the command moves the anchor for later relative paths,
so ``cd ~/.ssh && cat id_rsa`` is judged as ``cat ~/.ssh/id_rsa``.
"""

from __future__ import annotations

import os
import re
import shlex
import sys
from collections.abc import Iterator, Sequence

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from secret_file_read_paths import Hit, expand, find_hits  # noqa: E402

#: Programs that put a named file's CONTENTS somewhere the agent can see
#: (stdout, an archive, a copy, a link, the network). ``ls``/``stat``/
#: ``file``/``test``/``wc`` are absent on purpose: metadata, not contents.
READERS = frozenset(
    """
    cat tac nl head tail less more most bat batcat view grep egrep fgrep
    zgrep zcat rg ag ack sed awk gawk mawk cut sort uniq paste diff cmp comm
    jq yq cp ln scp rsync install base64 base32 basenc xxd od hexdump strings
    tar zip 7z 7za gzip bzip2 xz zstd openssl dd curl wget tee split iconv
    fold pr expand column
    """.split()
)
#: Readers that walk directories, so a secret-holding DIRECTORY counts.
RECURSIVE_READERS = frozenset(
    "grep egrep fgrep zgrep rg ag ack cp scp rsync tar zip 7z 7za diff".split()
)
#: Readers whose first positional operand is a PATTERN, not a file.
SEARCHERS = frozenset("grep egrep fgrep zgrep rg ag ack".split())
_PATTERN_OPTS = frozenset({"-e", "--regexp", "-f", "--file"})
#: Prefixes that run the NEXT word as the program.
WRAPPERS = frozenset(
    """
    sudo doas env nice ionice nohup exec command builtin time stdbuf timeout
    xargs chronic setsid flock
    """.split()
)
SHELLS = frozenset({"bash", "sh", "zsh", "dash", "ksh"})
BOUNDARIES = frozenset({";", "&", "&&", "|", "||", "|&", "(", ")", "\n"})
REDIRECT_TOKENS = frozenset({"<", "<<", "<<<", ">", ">>", ">&", "<&", "&>"})
_ASSIGNMENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
_DURATION = re.compile(r"^\d+(\.\d+)?[smhd]?$")

_GPG_DECRYPT = frozenset({"-d", "--decrypt", "--decrypt-files"})
_GPG_EXPORT_SECRET = frozenset(
    {"--export-secret-keys", "--export-secret-subkeys", "--export-secret-key"}
)
_GPG_VALUE_OPTS = frozenset(
    {
        "--homedir",
        "-r",
        "--recipient",
        "-u",
        "--local-user",
        "-o",
        "--output",
        "--keyring",
        "--secret-keyring",
        "--trustdb-name",
    }
)
_MAX_DEPTH = 3


def _tokens(command: str) -> list[str]:
    lexer = shlex.shlex(command, posix=True, punctuation_chars=";&|()<>")
    lexer.commenters = "#"
    lexer.whitespace_split = True
    return list(lexer)


def _simple_commands(tokens: Sequence[str]) -> Iterator[tuple[list[str], str]]:
    """Yield each simple command with the operator token that precedes it."""
    current: list[str] = []
    before = ""
    for tok in tokens:
        if tok in BOUNDARIES:
            if current:
                yield current, before
            current = []
            before = tok
        else:
            current.append(tok)
    if current:
        yield current, before


def _program_words(words: Sequence[str]) -> tuple[list[str], int]:
    """Program basenames in one simple command, and the first one's index."""
    progs: list[str] = []
    start = -1
    expect = True
    for j, word in enumerate(words):
        if expect:
            if word.startswith("-") or _ASSIGNMENT.match(word) or _DURATION.match(word):
                continue  # env assignment / wrapper option / timeout value
            base = os.path.basename(word)
            progs.append(base)
            if start < 0:
                start = j
            expect = base in WRAPPERS
        elif word in ("-exec", "-execdir", "-ok", "-okdir"):
            expect = True
    return progs, start


def _operands(words: Sequence[str], start: int) -> list[str]:
    """Every word after the program that could name a path."""
    out: list[str] = []
    for word in words[start + 1 :]:
        if word in REDIRECT_TOKENS or "://" in word:
            continue  # a URL is a remote resource, not a local path
        if word.startswith("-") and "=" in word:  # --file=PATH
            out.append(word.split("=", 1)[1])
            continue
        if word.startswith("@"):  # curl -d @FILE / --data-binary @FILE
            out.append(word[1:])
            continue
        if re.match(r"^(if|of)=", word):  # dd if=FILE
            out.append(word.split("=", 1)[1])
            continue
        if ":" in word and not word.startswith("/"):  # scp/rsync host:PATH
            out.append(word.split(":", 1)[1])
        out.append(word)
    return out


def _without_search_pattern(words: Sequence[str]) -> list[str]:
    """Drop a searcher's PATTERN operand: `grep .netrc docs/` names no file.

    Only the first non-option word after the searcher is dropped, and only
    when no `-e`/`-f` supplies the pattern -- so the worst a mis-parse (an
    option value such as `-A 3`) can do is keep a pattern, never drop a FILE
    that follows a real pattern.
    """
    out = list(words)
    for k, word in enumerate(out):
        if os.path.basename(word) not in SEARCHERS:
            continue
        rest = out[k + 1 :]
        if any(
            w in _PATTERN_OPTS or w.startswith(("--regexp=", "--file=")) for w in rest
        ):
            return out
        for j, w in enumerate(rest):
            if not w.startswith("-"):
                del out[k + 1 + j]
                return out
        return out
    return out


def _gpg_hits(words: Sequence[str], at: int, cwd: str) -> list[Hit]:
    args = list(words[at + 1 :])
    if any(a.split("=", 1)[0] in _GPG_EXPORT_SECRET for a in args):
        return [Hit("gpg --export-secret-keys", "GnuPG private key export")]
    positional: list[str] = []
    skip = False
    for a in args:
        if skip:
            skip = False
        elif a in _GPG_VALUE_OPTS:
            skip = True  # e.g. --homedir ~/.gnupg is configuration, not a read
        elif not a.startswith("-"):
            positional.append(a)
    # `gpg FILE` decrypts by default, so a positional secret is a read with
    # or without -d; `gpg --list-keys` has no positional and stays allowed.
    recursive = any(a in _GPG_DECRYPT for a in args)
    return find_hits(positional, cwd, recursive=recursive)


def hits_for_bash(command: str, cwd: str, depth: int = 0) -> list[Hit]:
    """Secret paths ``command`` would read, judged from ``cwd``."""
    if depth > _MAX_DEPTH or not command:
        return []
    try:
        tokens = _tokens(command)
    except ValueError:
        return []  # unparseable quoting: fail open, like the sibling guards

    hits: list[Hit] = []
    for i, tok in enumerate(tokens[:-1]):
        if tok == "<":  # `prog < FILE` feeds FILE's contents to any program
            hits += find_hits([tokens[i + 1]], cwd, recursive=False)

    upstream: list[str] = []  # operands of the previous simple command
    for words, before in _simple_commands(tokens):
        progs, start = _program_words(words)
        if not progs:
            continue
        feeding, upstream = upstream, _operands(words, start)
        if progs[0] == "cd":
            target = expand(words[start + 1], cwd) if len(words) > start + 1 else ""
            cwd = target or cwd
            continue
        if progs[0] in SHELLS or "eval" in progs:
            for k, word in enumerate(words[:-1]):
                if word == "-c":
                    hits += hits_for_bash(words[k + 1], cwd, depth + 1)
            if "eval" in progs:
                hits += hits_for_bash(" ".join(words[start + 1 :]), cwd, depth + 1)
            continue
        gpg_at = [
            k for k, w in enumerate(words) if os.path.basename(w) in ("gpg", "gpg2")
        ]
        if gpg_at and os.path.basename(words[gpg_at[0]]) in progs:
            hits += _gpg_hits(words, gpg_at[0], cwd)
            continue
        readers = [p for p in progs if p in READERS]
        if not readers:
            continue
        # `find DIR -exec cat {} +` walks DIR, so the directory counts.
        recursive = "find" in progs or any(p in RECURSIVE_READERS for p in readers)
        hits += find_hits(
            _operands(_without_search_pattern(words), start), cwd, recursive=recursive
        )
        if progs[0] == "xargs" and before == "|":
            # `find ~/.ssh | xargs cat`: the reader's operands arrive on stdin,
            # so the upstream command's operands are what gets read.
            hits += find_hits(feeding, cwd, recursive=True)
    return hits


# EOF
