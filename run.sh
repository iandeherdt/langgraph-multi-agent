#!/usr/bin/env bash
# Convenience wrapper for the harness. Equivalent to:
#   docker compose run --rm --use-aliases langgraph "$@"
#
# Why --use-aliases is mandatory: the playwright-mcp sibling container reaches the dev
# server the builder spawns via the service-name DNS alias `langgraph` (resolved by
# Compose's embedded DNS). Without --use-aliases, `compose run` only registers the
# container-name as a network alias; `langgraph` is unresolvable from playwright-mcp,
# so browser_navigate fails with NS_ERROR_UNKNOWN_HOST.

set -euo pipefail
exec docker compose run --rm --use-aliases langgraph "$@"
