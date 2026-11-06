#!/usr/bin/env bash
# Rebuild and restart the ListMonk Pro instance that lives in THIS checkout,
# then wait until it answers /health.
#
# Nothing here is specific to a server or a domain: the published port and the
# container name come from this checkout's .env (HOST_PORT / CONTAINER_NAME,
# defaults 8000 / listmonk-pro), and the data volume is named after the
# checkout directory. Several checkouts can therefore run side by side on one
# VPS, each updated by running its own copy of this script.
#
#   ./deploy.sh          rebuild + restart + health check
#   ./deploy.sh --pull   fast-forward to the latest commit first (manual update)
#
# The CI workflow (.github/workflows/deploy.yml) resets each checkout to
# origin/main itself and then calls this script without --pull.
set -euo pipefail

HEALTH_ATTEMPTS=30   # x 2 s

# Read one KEY=value from .env without sourcing it (values may contain spaces
# or shell metacharacters). Surrounding quotes, spaces and CR are dropped.
env_value() {
    sed -n "s/^$1=//p" .env | tail -n 1 | tr -d "\"' \r"
}

main() {
    cd "$(dirname "$(readlink -f "$0")")"

    if [ ! -f .env ]; then
        echo "Error: no .env in $PWD (copy .env.example and fill it in)." >&2
        exit 1
    fi

    if [ "${1:-}" = "--pull" ]; then
        git pull --ff-only
    fi

    local port name
    port=$(env_value HOST_PORT)
    port=${port:-8000}
    name=$(env_value CONTAINER_NAME)
    name=${name:-listmonk-pro}

    echo "Deploying $name from $PWD (127.0.0.1:$port)"
    docker compose up -d --build

    for i in $(seq 1 "$HEALTH_ATTEMPTS"); do
        if curl -fs "http://127.0.0.1:$port/health" > /dev/null; then
            echo "$name is healthy."
            return 0
        fi
        echo "Waiting for $name to respond ($i/$HEALTH_ATTEMPTS)..."
        sleep 2
    done

    echo "Health check failed for $name on port $port." >&2
    docker compose logs --tail 40 >&2 || true
    exit 1
}

# Everything runs from main(), which bash has fully parsed before it starts:
# `--pull` may rewrite this very file mid-run without corrupting the execution.
main "$@"; exit
