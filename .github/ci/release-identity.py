#!/usr/bin/env python3
"""Bind a SAC release to one tag, source commit and two verified artifacts."""

import argparse
import base64
import hashlib
import importlib.util
import json
import os
import re
import stat
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import tomllib

REPOSITORY = "scitex-ai/scitex-agent-container"
TAG = re.compile(r"v(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)")
LOGIN = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?")
PROOF = "release-proof.json"


def _load_sibling(name):
    """Load a reviewed sibling by path; this runs under `python -I`."""
    spec = importlib.util.spec_from_file_location(
        name, os.path.join(os.path.dirname(__file__), name + ".py")
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


# Dependency order: each sibling imports only those registered before it.
normalized_name = _load_sibling("release_metadata").normalized_name
_archive = _load_sibling("release_archive")
_payload = _load_sibling("release_payload")
SHA = _archive.SHA
wheel_identity = _archive.wheel_identity
sdist_identity = _archive.sdist_identity
git_read = _payload.git_read
source_payload_identity = _payload.source_payload_identity


def api(path, status=200, *, open_url=urllib.request.urlopen):
    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    token = os.environ.get("GITHUB_TOKEN")
    if token:
        headers["Authorization"] = "Bearer " + token
    request = urllib.request.Request("https://api.github.com/" + path, headers=headers)
    with open_url(request, timeout=10) as response:
        if response.status != status:
            raise ValueError("GitHub identity response did not match")
        if status == 204:
            return {"status": 204}
        body = response.read(1048577)
        if len(body) > 1048576:
            raise ValueError("GitHub identity response too large")
        return json.loads(body)


def release_context():
    # The existing shared runner-admission gate owns membership. The caller
    # passes its exact output; this helper does not invent another member model.
    if (
        os.environ.get("GITHUB_REPOSITORY") != REPOSITORY
        or os.environ.get("RELEASE_AUTHORIZED") != "true"
    ):
        raise ValueError("shared release admission is not confirmed")
    if os.environ.get("GITHUB_EVENT_NAME") not in {"push", "workflow_dispatch"}:
        raise ValueError("release event is unsupported")


def resolve(tag, query=api):
    if not TAG.fullmatch(tag):
        raise ValueError("release requires an existing vX.Y.Z tag")
    prefix = "repos/" + REPOSITORY
    ref = query(prefix + "/git/ref/tags/" + tag)
    if ref.get("ref") != "refs/tags/" + tag:
        raise ValueError("returned tag ref differs")
    item = ref["object"]
    seen = set()
    for _ in range(5):
        digest = item.get("sha", "")
        if not SHA.fullmatch(digest) or digest in seen:
            raise ValueError("invalid or cyclic tag object")
        seen.add(digest)
        if item.get("type") == "commit":
            break
        if item.get("type") != "tag":
            raise ValueError("tag does not resolve to a commit")
        item = query(prefix + "/git/tags/" + digest)["object"]
    else:
        raise ValueError("tag annotation depth exceeded")
    commit = item["sha"]
    comparison = query(prefix + "/compare/" + commit + "...develop")
    if comparison.get("base_commit", {}).get("sha") != commit or comparison.get(
        "status"
    ) not in {"ahead", "identical"}:
        raise ValueError("release source is not on the accepted develop history")
    content = query(prefix + "/contents/pyproject.toml?ref=" + commit)
    if content.get("encoding") != "base64":
        raise ValueError("unsupported metadata encoding")
    raw = base64.b64decode(content["content"].replace("\n", ""), validate=True)
    if len(raw) > 131072 or hashlib.sha1(
        b"blob " + str(len(raw)).encode() + b"\0" + raw
    ).hexdigest() != content.get("sha"):
        raise ValueError("release metadata Git identity differs")
    project = tomllib.loads(raw.decode())["project"]
    if (
        normalized_name(project["name"]) != "scitex-agent-container"
        or project["version"] != tag[1:]
    ):
        raise ValueError("tag and project metadata differ")
    return {"tag": tag, "commit": commit, "version": tag[1:]}


def regular_bytes(path):
    if path.is_symlink() or not path.is_file():
        raise ValueError("artifact must be a regular file")
    with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW), "rb") as stream:
        before = os.fstat(stream.fileno())
        if not stat.S_ISREG(before.st_mode) or before.st_size > 268435456:
            raise ValueError("artifact type or size is invalid")
        raw = stream.read()
        after = os.fstat(stream.fileno())
    if (before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (
        after.st_ino,
        after.st_size,
        after.st_mtime_ns,
        after.st_ctime_ns,
    ) or path.lstat().st_ino != before.st_ino:
        raise ValueError("artifact changed while reading")
    return raw


def artifact_proof(directory, tag, commit, run, attempt, source_root=Path(".")):
    if (
        not TAG.fullmatch(tag)
        or not SHA.fullmatch(commit)
        or not run.isdigit()
        or not attempt.isdigit()
    ):
        raise ValueError("release identity is malformed")
    if directory.is_symlink() or not directory.is_dir():
        raise ValueError("artifact directory is unsafe")
    files = sorted(path for path in directory.iterdir() if path.name != PROOF)
    if (
        len(files) != 2
        or sum(path.name.endswith(".whl") for path in files) != 1
        or sum(path.name.endswith(".tar.gz") for path in files) != 1
    ):
        raise ValueError("release requires exactly one wheel and one sdist")
    rows = []
    payloads = {}
    for path in files:
        if path.name not in {
            "scitex_agent_container-" + tag[1:] + "-py3-none-any.whl",
            "scitex_agent_container-" + tag[1:] + ".tar.gz",
        }:
            raise ValueError("artifact filename version differs")
        raw = regular_bytes(path)
        payloads["wheel" if path.name.endswith(".whl") else "sdist"] = raw
        identity = (
            wheel_identity(raw, tag[1:])
            if path.name.endswith(".whl")
            else sdist_identity(raw, tag[1:])
        )
        rows.append(
            {
                "name": path.name,
                "bytes": len(raw),
                "sha256": hashlib.sha256(raw).hexdigest(),
                **identity,
            }
        )
    source_identity = source_payload_identity(
        payloads["wheel"], payloads["sdist"], commit, source_root
    )
    return {
        "schema": "scitex-agent-container-release/v1",
        "repository": REPOSITORY,
        "tag": tag,
        "commit": commit,
        "run": run,
        "attempt": attempt,
        "files": rows,
        "source_membership": source_identity,
    }


def run_witness(commit, run, attempt, query=api, *, tests=True):
    if (
        not SHA.fullmatch(commit)
        or not re.fullmatch(r"[1-9][0-9]*", run)
        or not re.fullmatch(r"[1-9][0-9]*", attempt)
    ):
        raise ValueError("release run identity is malformed")
    prefix = "repos/" + REPOSITORY + "/actions/runs/" + run + "/attempts/" + attempt
    current = query(prefix)
    if (
        current.get("head_sha") != commit
        or current.get("id") != int(run)
        or current.get("run_attempt") != int(attempt)
        or current.get("repository", {}).get("full_name") != REPOSITORY
        or current.get("event") not in {"push", "workflow_dispatch"}
        or current.get("path", "").split("@", 1)[0]
        != ".github/workflows/pypi-publish-and-github-release-on-tag.yml"
    ):
        raise ValueError("current release run/source differs")
    actors = {
        current.get(key, {}).get("login", "") for key in ("actor", "triggering_actor")
    }
    expected = {
        os.environ.get("GITHUB_ACTOR", ""),
        os.environ.get("GITHUB_TRIGGERING_ACTOR", ""),
    }
    if actors != expected or any(not LOGIN.fullmatch(x) for x in actors):
        raise ValueError("current release actors differ")
    witness = {
        "run": run,
        "attempt": attempt,
        "head_sha": commit,
        "actors": sorted(actors),
    }
    if not tests:
        return witness
    release_tests = _load_sibling("release_tests")
    witness.update(
        release_tests.witness(REPOSITORY, commit, run, attempt, current, prefix, query)
    )
    return witness


def index_identity(document, proof):
    info = document.get("info", {})
    if (
        normalized_name(info.get("name", "")) != "scitex-agent-container"
        or info.get("version") != proof["tag"][1:]
    ):
        raise ValueError("PyPI served project identity differs")
    rows = document.get("urls", [])
    if len(rows) != len(proof["files"]):
        raise ValueError("PyPI served artifact membership differs")
    for artifact in proof["files"]:
        matches = [row for row in rows if row.get("filename") == artifact["name"]]
        if (
            len(matches) != 1
            or matches[0].get("digests", {}).get("sha256") != artifact["sha256"]
            or matches[0].get("size") != artifact["bytes"]
            or matches[0].get("packagetype")
            != ("bdist_wheel" if artifact["name"].endswith(".whl") else "sdist")
        ):
            raise ValueError("PyPI served artifact bytes differ")
    return {"version": info["version"], "files": len(rows)}


def verify_index(proof, *, open_url=urllib.request.urlopen, pause=time.sleep):
    url = "https://pypi.org/pypi/scitex-agent-container/" + proof["tag"][1:] + "/json"
    for attempt in range(18):
        try:
            with open_url(url, timeout=20) as response:
                raw = response.read(1048577)
                if response.status != 200 or len(raw) > 1048576:
                    raise ValueError("PyPI served metadata response differs")
                document = json.loads(raw)
        except urllib.error.HTTPError as error:
            if error.code != 404:
                raise
        else:
            return index_identity(document, proof)
        if attempt != 17:
            pause(5)
    raise ValueError("PyPI has not served the qualified version/artifact bytes")


def transport_witness(commit, run, digest, query=api):
    if not re.fullmatch(r"[0-9a-f]{64}", digest):
        raise ValueError("uploaded artifact digest is missing")
    document = query(
        "repos/" + REPOSITORY + "/actions/runs/" + run + "/artifacts?per_page=100"
    )
    rows = document.get("artifacts", [])
    if document.get("total_count") != len(rows) or len(rows) > 100:
        raise ValueError("uploaded artifact inventory is incomplete")
    selected = [row for row in rows if row.get("name") == "dist-" + commit]
    if (
        len(selected) != 1
        or selected[0].get("expired") is not False
        or selected[0].get("digest") != "sha256:" + digest
        or selected[0].get("workflow_run", {}).get("id") != int(run)
        or selected[0].get("workflow_run", {}).get("head_sha") != commit
    ):
        raise ValueError("uploaded artifact run/source/digest differs")
    return {
        "name": selected[0]["name"],
        "digest": selected[0]["digest"],
        "id": selected[0]["id"],
    }


def main(query=api):
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "mode",
        choices=[
            "resolve",
            "check-tests",
            "write-proof",
            "verify-proof",
            "verify-index",
        ],
    )
    parser.add_argument("--tag", required=True)
    parser.add_argument("--commit")
    parser.add_argument("--dist", type=Path, default=Path("dist"))
    parser.add_argument("--revalidate", action="store_true")
    args = parser.parse_args()
    release_context()
    if args.mode == "resolve":
        identity = resolve(args.tag, query)
        # The fixed shared test callee checks out github.sha. Dispatching an
        # older tag from another ref must refuse before any test/build effect.
        if os.environ.get("GITHUB_SHA") != identity["commit"]:
            raise ValueError("workflow event and requested tag commit differ")
        run_witness(
            identity["commit"],
            os.environ.get("GITHUB_RUN_ID", ""),
            os.environ.get("GITHUB_RUN_ATTEMPT", ""),
            query,
            tests=False,
        )
        with open(os.environ["GITHUB_OUTPUT"], "a") as stream:
            for key, value in identity.items():
                stream.write(key + "=" + value + "\n")
        print(json.dumps(identity))
        return
    if git_read(["rev-parse", "HEAD"], Path(".")).decode().strip() != args.commit:
        raise ValueError("checkout and release commit differ")
    witness = run_witness(
        args.commit or "",
        os.environ.get("GITHUB_RUN_ID", ""),
        os.environ.get("GITHUB_RUN_ATTEMPT", ""),
        query,
    )
    if args.mode == "check-tests":
        print(json.dumps(witness))
        return
    proof = artifact_proof(
        args.dist,
        args.tag,
        args.commit or "",
        os.environ.get("GITHUB_RUN_ID", ""),
        os.environ.get("GITHUB_RUN_ATTEMPT", ""),
    )
    proof["run_witness"] = witness
    path = args.dist / PROOF
    if args.mode == "write-proof":
        with os.fdopen(
            os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w"
        ) as stream:
            json.dump(proof, stream, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
    elif json.loads(regular_bytes(path)) != proof:
        raise ValueError("artifact proof or byte identity differs")
    if args.mode in {"verify-proof", "verify-index"}:
        digest = os.environ.get("RELEASE_ARTIFACT_DIGEST", "")
        if not digest:
            # The upload action exposes no digest output, so resolve the
            # uploaded artifact's own digest from this run instead of
            # trusting a workflow-passed value: same run, same commit,
            # name dist-<commit>, exactly one match.
            document = query(
                "repos/"
                + REPOSITORY
                + "/actions/runs/"
                + os.environ["GITHUB_RUN_ID"]
                + "/artifacts?per_page=100"
            )
            rows = [
                row
                for row in document.get("artifacts", [])
                if row.get("name") == "dist-" + (args.commit or "")
            ]
            if len(rows) != 1 or not str(rows[0].get("digest", "")).startswith(
                "sha256:"
            ):
                raise ValueError("uploaded release artifact is missing or ambiguous")
            digest = str(rows[0]["digest"])[len("sha256:") :]
        transport_witness(
            args.commit,
            os.environ["GITHUB_RUN_ID"],
            digest,
            query,
        )
    if args.revalidate:
        if os.environ.get("GITHUB_SHA") != args.commit or resolve(args.tag, query) != {
            "tag": args.tag,
            "commit": args.commit,
            "version": args.tag[1:],
        }:
            raise ValueError("release authority changed after build")
    if args.mode == "verify-index":
        print(json.dumps(verify_index(proof)))
    else:
        print(json.dumps(proof))


if __name__ == "__main__":
    main()
