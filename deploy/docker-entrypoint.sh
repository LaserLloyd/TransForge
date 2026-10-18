#!/bin/sh
# TransForge container entrypoint.
#
#  * `docker compose run --rm app status --all` works: a first argument that is
#    not an executable (or starts with "-") is a transforge subcommand.
#  * First start: if the config file is missing, seed it from the shipped
#    template with the LLM endpoint filled in. TransForge itself has no
#    environment variable for the endpoint (it is a key in config.toml), so
#    LLM_BASE_URL is applied here, once; an existing config is never touched.
#    TransForge's `endpoint` is the server ROOT (no /v1), so a trailing /v1 on
#    LLM_BASE_URL is dropped.
#  * `schedule`: loop forever running `transforge $TRANSFORGE_SCHEDULE_CMD`
#    every $TRANSFORGE_INTERVAL seconds (for the optional compose service).
set -eu

CFG="${HOME}/.config/transforge/config.toml"
LLM_BASE_URL="${LLM_BASE_URL:-http://host.docker.internal:1234/v1}"
ENDPOINT="${LLM_BASE_URL%/}"
ENDPOINT="${ENDPOINT%/v1}"

if [ ! -f "$CFG" ]; then
    if sed "s|@ENDPOINT@|${ENDPOINT}|" /usr/local/share/transforge/config.example.toml > "$CFG" 2>/dev/null; then
        echo "transforge: seeded $CFG (endpoint ${ENDPOINT}); edit [sites.*] to point at your trees under /sites" >&2
    else
        echo "transforge: no config at $CFG and the directory is not writable by UID $(id -u)." >&2
        echo "            create it on the host (mkdir -p config && chown 1000:1000 config) or see docs/deploy/docker.md" >&2
    fi
fi

if [ "${1:-}" = "schedule" ]; then
    : "${TRANSFORGE_SCHEDULE_CMD:?set TRANSFORGE_SCHEDULE_CMD, e.g. 'run --site example'}"
    INTERVAL="${TRANSFORGE_INTERVAL:-3600}"
    HB=/data/tmp/heartbeat
    echo "transforge: schedule every ${INTERVAL}s: transforge ${TRANSFORGE_SCHEDULE_CMD}" >&2
    CHILD=""
    trap '[ -n "$CHILD" ] && kill "$CHILD" 2>/dev/null; exit 0' TERM INT
    while :; do
        # shellcheck disable=SC2086  # the command is deliberately word-split
        transforge ${TRANSFORGE_SCHEDULE_CMD} &
        CHILD=$!
        wait "$CHILD" || echo "transforge: scheduled run exited non-zero (will retry next interval)" >&2
        CHILD=""
        waited=0
        while [ "$waited" -lt "$INTERVAL" ]; do
            touch "$HB"
            sleep 30 &
            CHILD=$!
            wait "$CHILD" || true
            CHILD=""
            waited=$((waited + 30))
        done
    done
fi

if [ "$#" -eq 0 ] || [ "${1#-}" != "$1" ] || ! command -v "$1" >/dev/null 2>&1; then
    set -- transforge "$@"
fi
exec "$@"
