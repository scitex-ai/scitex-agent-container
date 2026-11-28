#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# File: src/scitex_agent_container/_baseline_assets/secret_read_hooks/secret_file_read_paths.py
"""Path normalisation and the secret-path catalogue.

The ONE place that knows which paths hold secret material. Every consumer
(native file tools, Bash) funnels operands through :func:`find_hits`, so a
path is judged identically however it was spelled:

* ``~``, ``~user``, ``$HOME`` and ``${HOME}`` are expanded;
* relative paths are anchored at the caller's ``cwd``;
* ``.`` / ``..`` are collapsed (``os.path.normpath``);
* paths that EXIST are additionally resolved with ``os.path.realpath``,
  so a symlink to a key is judged as the key;
* ``{a,b}`` braces are expanded and wildcard runs (``*``, ``**``, ``?``,
  ``[...]``) are judged by what they could expand to.

Deployed flat into ``$HOME/.claude/hooks/pre-tool-use/`` next to
``secret_file_read_core.py``; not a package.
"""

from __future__ import annotations

import fnmatch
import os
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

#: Stands in for any run of shell/glob wildcards. A NUL never occurs in a
#: real path, so a wildcard can never accidentally spell a literal name.
WILD = "\x00"

SIGNING_STORE = "/var/lib/scitex-secrets-signing"
TOKEN_SUFFIXES = (
    ".token",
    ".github-token",
    ".gitea-token",
    ".a2a-token",
    ".password",
)
CERT_SUFFIXES = (".pem", ".p12", ".pfx")
#: Public CA bundles: shaped like ``*.pem`` but carry no private material.
CA_BUNDLE_DIRS = (
    "/etc/ssl/certs",
    "/etc/pki/ca-trust",
    "/etc/pki/tls/certs",
    "/usr/share/ca-certificates",
    "/usr/local/share/ca-certificates",
)
CA_BUNDLE_NAMES = frozenset(
    {"cacert.pem", "ca-bundle.pem", "ca-certificates.pem", "tls-ca-bundle.pem"}
)

#: Probe basenames: can a WILDCARD basename inside a secret-holding
#: directory expand to a secret file? (`~/.ssh/*` yes, `~/.ssh/*.pub` no.)
SSH_PROBES = ("id_rsa", "id_ed25519", "id_ecdsa", "id_dsa", "ssh_host_rsa_key")
TOKEN_STORE_PROBES = ("access_tokens",) + tuple("x" + s for s in TOKEN_SUFFIXES)

#: (directory suffix, secret basename, label) for single-file credential
#: stores inside an otherwise ordinary config directory.
CONFIG_STORES = (
    ((".config", "gh"), "hosts.yml", "GitHub CLI token (~/.config/gh/hosts.yml)"),
    (
        (".docker",),
        "config.json",
        "Docker registry credentials (~/.docker/config.json)",
    ),
    ((".aws",), "credentials", "AWS credentials (~/.aws/credentials)"),
)


@dataclass(frozen=True)
class Hit:
    """One secret target a tool call would read."""

    path: str
    label: str


# ----------------------------------------------------------------------
# Normalisation
# ----------------------------------------------------------------------
def _home() -> str:
    return os.environ.get("HOME") or os.path.expanduser("~")


def expand(raw: str, cwd: str) -> str:
    """Expand ``~``/``$HOME``, anchor at ``cwd``, collapse ``.``/``..``."""
    home = _home()
    text = raw.replace("${HOME}", home).replace("$HOME", home)
    if text == "~" or text.startswith("~/"):
        text = home + text[1:]
    elif text.startswith("~"):
        text = os.path.expanduser(text)
        if text.startswith("~"):  # ~unknownuser: cannot be resolved
            return ""
    if not text.startswith("/"):
        text = os.path.join(cwd or os.getcwd(), text)
    return os.path.normpath(text)


def _literalise(pattern: str) -> str:
    """Replace every wildcard run (``*``, ``**``, ``?``, ``[...]``) by WILD."""
    text = re.sub(r"\[[^\]/]*\]", WILD, pattern)
    text = re.sub(r"[*?]+", WILD, text)
    return re.sub(WILD + "+", WILD, text)


def _expand_braces(text: str, limit: int = 64) -> list[str]:
    """Bash-style ``{a,b}`` expansion (bounded)."""
    out: list[str] = []
    stack = [text]
    while stack and len(out) < limit:
        item = stack.pop()
        match = re.search(r"\{([^{}]*,[^{}]*)\}", item)
        if not match:
            out.append(item)
            continue
        for alt in match.group(1).split(","):
            stack.append(item[: match.start()] + alt + item[match.end() :])
    return out or [text]


def variants(raw: str, cwd: str) -> list[str]:
    """Every normalised absolute spelling an operand can refer to."""
    found: list[str] = []
    for spelled in _expand_braces(raw):
        path = expand(_literalise(spelled), cwd)
        if not path:
            continue
        found.append(path)
        if WILD not in path:
            try:
                real = os.path.realpath(path)
            except (OSError, ValueError):
                real = path
            if real != path:
                found.append(real)
    return found


# ----------------------------------------------------------------------
# Classification of ONE normalised absolute path
# ----------------------------------------------------------------------
def _seq_index(parts: Sequence[str], seq: Sequence[str]) -> int:
    n = len(seq)
    for i in range(len(parts) - n + 1):
        if tuple(parts[i : i + n]) == tuple(seq):
            return i
    return -1


def _wild_could_be(name: str, probes: Iterable[str]) -> bool:
    """True when a WILD-bearing basename can expand to one of ``probes``."""
    if WILD not in name:
        return False
    pattern = name.replace(WILD, "*")
    return any(fnmatch.fnmatchcase(p, pattern) for p in probes)


def _ssh_private_key(name: str) -> bool:
    if name.endswith(".pub"):
        return False
    return name.startswith("id_") or name.endswith("_key")


def _token_store(parts: list[str], name: str, recursive: bool) -> str | None:
    i = _seq_index(parts, (".bash.d", "secrets"))
    if i < 0:
        return None
    rest = parts[i + 2 :]
    store = "dotfiles plaintext token store (.bash.d/secrets)"
    if not rest:
        return store if recursive else None
    if rest[0] == "access_tokens" and (len(rest) > 1 or recursive):
        return "dotfiles plaintext token store (.bash.d/secrets/access_tokens)"
    if len(rest) > 1 and WILD in rest[0]:
        return store  # secrets/*/x -- the wildcard may be access_tokens
    if name.endswith(TOKEN_SUFFIXES):
        return "dotfiles plaintext token file (.bash.d/secrets)"
    if len(rest) == 1 and _wild_could_be(name, TOKEN_STORE_PROBES):
        return store
    return None


def classify(path: str, *, recursive: bool) -> str | None:
    """Return a label when ``path`` is (or, recursively, holds) secrets.

    ``recursive`` is True for consumers that read a DIRECTORY's contents
    (``grep -r``, ``tar``, ``cp -r``, the Grep tool): then a secret-holding
    directory itself counts. Single-file readers pass False, which keeps a
    directory name on its own (the ``ls`` case) out of scope.
    """
    parts = [p for p in path.split("/") if p]
    if not parts:
        return None
    name = parts[-1]
    parent = parts[:-1]

    if path.startswith(SIGNING_STORE + "/") or (path == SIGNING_STORE and recursive):
        return "scitex secrets signing store (/var/lib/scitex-secrets-signing)"

    for store, label in (
        (".pw", "GPG-encrypted password payloads (~/.pw)"),
        (".gnupg", "GnuPG private key ring (~/.gnupg)"),
    ):
        if store in parent or (name == store and recursive):
            return label

    label = _token_store(parts, name, recursive)
    if label:
        return label

    if ".ssh" in parent and (
        _ssh_private_key(name) or _wild_could_be(name, SSH_PROBES)
    ):
        return "SSH private key"
    if name == ".ssh" and recursive:
        return "SSH key directory (~/.ssh)"
    if name.startswith("ssh_host_") and name.endswith("_key"):
        return "SSH host private key"

    lowered = name.lower()
    if lowered.endswith(CERT_SUFFIXES) and lowered not in CA_BUNDLE_NAMES:
        if not any(path.startswith(d + "/") for d in CA_BUNDLE_DIRS):
            return "private key / certificate bundle (*.pem, *.p12, *.pfx)"

    if name == ".git-credentials":
        return "git credential store (~/.git-credentials)"
    if name in (".netrc", "_netrc"):
        return "netrc credentials (~/.netrc)"

    for dirs, secret_name, store_label in CONFIG_STORES:
        n = len(dirs)
        if tuple(parent[-n:]) == dirs and (
            name == secret_name or _wild_could_be(name, (secret_name,))
        ):
            return store_label
        if recursive and tuple(parts[-n:]) == dirs:
            return store_label
    return None


def find_hits(operands: Iterable[str], cwd: str, *, recursive: bool) -> list[Hit]:
    """Classify every operand; one :class:`Hit` per secret operand."""
    hits: list[Hit] = []
    for raw in operands:
        if not raw or not isinstance(raw, str):
            continue
        for path in variants(raw, cwd):
            label = classify(path, recursive=recursive)
            if label:
                hits.append(Hit(raw, label))
                break
    return hits


# EOF
