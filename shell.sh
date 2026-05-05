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

# Forward host shell env vars (HARNESS_*, OPENROUTER_*, etc.) to the container.
# See run.sh for why this sweep is necessary (docker-compose.yml only env_file's .env).
forward_args=()
while IFS='=' read -r name _; do
    case "${name}" in
        HARNESS_*|OPENROUTER_*|ANTHROPIC_*|OPENAI_*|HF_TOKEN|HUGGING_FACE_HUB_TOKEN)
            forward_args+=(-e "${name}")
            ;;
    esac
done < <(env)

exec docker compose run --rm --use-aliases --service-ports "${forward_args[@]}" langgraph bash "$@"
