#!/usr/bin/env bash
# Lesson 01 — Building and inspecting a runtime image.
#
# Each agent picks its image in spec.yaml:
#
#   spec:
#     image: scitex-agent-container:sdk-persistent   # default-ish
#   # or:
#     image: scitex-agent-container:cuda-12          # GPU agent
#     image: my-custom:latest                        # bring your own
#
# `sac image build` builds the *bundled default* image
# (`scitex-agent-container:sdk-persistent`); custom images you build
# with plain docker (or any of your team's pipelines).
#
# Pure docker:
#   docker build -t scitex-agent-container:sdk-persistent -f containers/Dockerfile containers/
#   docker images scitex-agent-container
#   docker inspect scitex-agent-container:sdk-persistent
#
# sac wrapper (default image only):
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
