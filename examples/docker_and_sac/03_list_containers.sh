#!/usr/bin/env bash
# Lesson 03 — Listing what's running.
#
# Pure docker:
#   docker ps                          # running containers
#   docker ps -a                       # including stopped
#   docker ps --filter "label=sac"     # only sac-managed
#
# sac equivalent — operates on the *agent registry* (sqlite at
# ~/.scitex/agent-container/state.db), not raw docker:
#   sac agent status                   # fleet view, table
#   sac agent status --json            # machine-readable
#   sac agent status <name>            # rich per-agent payload
#
# Why sac has its own list:
#   docker ps only shows live containers. sac tracks agents whose
#   container has died but whose session jsonl is still recoverable
#   (you can `sac agent recall <name>` to resume).
set -euo pipefail

echo "── docker ps (raw) ──"
docker ps --format 'table {{.Names}}\t{{.Image}}\t{{.Status}}' || true

echo
echo "── sac agent status (registry) ──"
sac agent status
