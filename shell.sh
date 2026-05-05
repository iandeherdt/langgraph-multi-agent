#!/usr/bin/env bash
# Convenience wrapper: drop a shell into the langgraph container with the right flags.
# Equivalent to:
#   docker compose run --rm --use-aliases --service-ports langgraph bash "$@"
#
# Why a separate wrapper from run.sh: run.sh hardcodes `python graph.py "$@"` so its
# args forward to the harness. To get a shell instead, you'd have to remember the full
# `docker compose run --rm --use-aliases --service-ports langgraph bash` invocation —
# easy to skip a flag and end up with a container whose dev-server port isn't published
# to the host. Hence this script.
#
# `docker compose exec langgraph bash` does NOT work for this — exec only attaches to
# `compose up` containers, not the transient `compose run` containers the harness uses.

set -euo pipefail
exec docker compose run --rm --use-aliases --service-ports langgraph bash "$@"
