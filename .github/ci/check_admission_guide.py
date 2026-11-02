"""Verify portable admission-guide references and source freshness in a wheel."""
from __future__ import annotations

import argparse
import hashlib
from html.parser import HTMLParser
import io
import json
from pathlib import Path
import posixpath
import re
import sys
from urllib.parse import urlsplit
from zipfile import ZipFile

_PACKAGE = "scitex_agent_container"
_SKILL = f"{_PACKAGE}/_skills/scitex-agent-container/57_gui-admission-receipts.md"
_HTML = f"{_PACKAGE}/_sphinx_html/talking-to-agents.html"
_SOURCE = f"{_PACKAGE}/_sphinx_html/_sources/talking-to-agents.md.txt"


class _GuideHTML(HTMLParser):
    def __init__(self):
        super().__init__()
        self.anchors = set()
        self.fingerprints = []

    def handle_starttag(self, tag, attrs):
        values = dict(attrs)
        if values.get("id"):
            self.anchors.add(values["id"])
        if tag == "meta" and values.get("name") == "sac-guide-source-sha256":
            self.fingerprints.append(values.get("content"))


def verify_admission_guide(wheel_bytes: bytes, source_root: Path) -> dict:
    """Refuse missing links, stale sources, stale HTML and unbound fragments."""
    canonical = (source_root / "docs/talking-to-agents.md").read_bytes()
    fingerprint = hashlib.sha256(canonical).hexdigest()
    with ZipFile(io.BytesIO(wheel_bytes)) as archive:
        names = archive.namelist()
        if any(names.count(n) != 1 for n in (_SKILL, _HTML, _SOURCE)):
            raise ValueError("admission guide resource missing or duplicated")
        if archive.read(_SOURCE) != canonical:
            raise ValueError("packaged guide source differs from canonical documentation")
        guide = _GuideHTML()
        guide.feed(archive.read(_HTML).decode("utf-8"))
        if guide.fingerprints != [fingerprint]:
            raise ValueError("packaged guide HTML source fingerprint is stale or absent")
        links = re.findall(r"\]\(([^)]+)\)", archive.read(_SKILL).decode("utf-8"))
        if len(links) != 1:
            raise ValueError("admission skill must identify its portable guide reference")
        link = urlsplit(links[0])
        target = posixpath.normpath(posixpath.join(posixpath.dirname(_SKILL), link.path))
        if link.scheme or link.netloc or link.query or target != _HTML:
            raise ValueError("admission guide reference escapes its packaged HTML owner")
        if not link.fragment or link.fragment not in guide.anchors:
            raise ValueError("admission guide reference has no rendered section anchor")
    return {"guide": _HTML, "anchor": link.fragment, "source_sha256": fingerprint}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("wheel", type=Path)
    parser.add_argument("--source-root", type=Path, default=Path(__file__).resolve().parents[2])
    args = parser.parse_args()
    proof = verify_admission_guide(args.wheel.read_bytes(), args.source_root)
    sys.stdout.write(json.dumps(proof) + "\n")


if __name__ == "__main__":
    main()
