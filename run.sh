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

# --- Pre-flight: clean up stale port-3000 binders ---------------------------------
# `docker compose run --rm` is supposed to delete the run container on exit, but if a
# previous run was killed via `docker kill` (rather than Ctrl-C), or the daemon was
# overloaded, the orphan can linger holding port 3000 — and our --service-ports below
# will then fail with "port is already allocated". Detect + offer to remove before we
# attempt to start. Only kills langgraph-run containers we recognize; never touches
# anything else binding 3000 (e.g., a host process the user genuinely owns).
stale=$(docker ps \
    --filter "label=com.docker.compose.service=langgraph" \
    --filter "publish=3000" \
    --format "{{.Names}}" 2>/dev/null || true)
if [ -n "${stale}" ]; then
    echo "WARN: stale langgraph-run container(s) still bound to host port 3000:" >&2
    echo "${stale}" | sed 's/^/  - /' >&2
    echo "" >&2
    echo "These will block --service-ports binding. Remove them and re-run:" >&2
    echo "  docker rm -f $(echo "${stale}" | tr '\n' ' ')" >&2
    echo "" >&2
    echo "(Or run with HARNESS_NO_KILL_STALE=1 to skip this check.)" >&2
    if [ "${HARNESS_NO_KILL_STALE:-0}" != "1" ]; then
        # shellcheck disable=SC2086
        docker rm -f ${stale} >/dev/null 2>&1 || true
        echo "Removed stale containers; continuing." >&2
        echo "" >&2
    else
        exit 1
    fi
fi

# --- Forward host shell env vars to the container --------------------------------
# docker-compose.yml uses `env_file: .env`, which loads variables from .env only;
# variables exported in the host shell (e.g., `HARNESS_EVALUATOR_TIER=cheap ./run.sh ...`)
# are NOT forwarded by default. `docker compose run -e VAR` (no `=value`) forwards
# the var's value from the host shell. Sweep relevant prefixes/names so per-invocation
# overrides Just Work without having to edit .env.
forward_args=()
while IFS='=' read -r name _; do
    case "${name}" in
        HARNESS_*|OPENROUTER_*|ANTHROPIC_*|OPENAI_*|HF_TOKEN|HUGGING_FACE_HUB_TOKEN)
            forward_args+=(-e "${name}")
            ;;
    esac
done < <(env)

exec docker compose run --rm --use-aliases --service-ports "${forward_args[@]}" langgraph python graph.py "$@"
