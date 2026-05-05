#!/usr/bin/env bash
# Convenience wrapper for the harness. Equivalent to:
#   docker compose run --rm --use-aliases --service-ports langgraph python graph.py "$@"
#
# Three compose-run quirks this wrapper handles:
#
# --use-aliases: the playwright-mcp sibling container reaches the dev server the
# builder spawns via the service-name DNS alias `langgraph` (resolved by Compose's
# embedded DNS). Without this flag, compose-run containers only register the
# container-name as a network alias; `langgraph` is unresolvable from playwright-mcp,
# so browser_navigate fails with NS_ERROR_UNKNOWN_HOST.
#
# --service-ports: compose-run IGNORES the service's `ports:` spec by default — even
# when docker-compose.yml lists `ports: "3000:3000"`. Without this flag, the dev server
# the builder spawns is reachable inside the container and from playwright-mcp (sibling
# DNS), but NOT from the host (your Mac browser). With this flag, the run container
# publishes its declared ports, so http://localhost:3000 on the host works.
#
# explicit `python graph.py "$@"`: docker compose run treats any args after the service
# name as the COMMAND to run inside the container, REPLACING the Dockerfile's CMD. So
# `./run.sh --resume X` without this would try to exec `--resume X` directly inside the
# container and fail with "executable not found". Spelling out `python graph.py` makes
# our flags forward to the Python entrypoint.

set -euo pipefail
exec docker compose run --rm --use-aliases --service-ports langgraph python graph.py "$@"
