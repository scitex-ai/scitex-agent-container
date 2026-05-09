#!/usr/bin/env bash
# Lesson 01 — Building and inspecting the runtime image.
#
# sac runs every agent inside a single shared image
# `scitex-agent-container:sdk-persistent`. Each agent is a separate
# container started from that image; the image itself is built once.
#
# Pure docker:
#   docker build -t scitex-agent-container:sdk-persistent -f containers/Dockerfile containers/
#   docker images scitex-agent-container
#   docker inspect scitex-agent-container:sdk-persistent
#
# sac wrapper:
#   sac image build               # invokes docker build with the right tag/dockerfile
#   sac image build --dry-run     # just print what would run
set -euo pipefail
APPLY="${1:-}"

echo "── docker images (filtered) ──"
docker images scitex-agent-container || true

echo
echo "── sac image build --dry-run ──"
sac image build --dry-run

if [[ "$APPLY" == "--apply" ]]; then
    echo
    echo "── sac image build (real) ──"
    sac image build -y
fi
