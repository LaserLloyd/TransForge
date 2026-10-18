#!/bin/sh
# TransForge container entrypoint. Deliberately POSIX sh and short.
#
# In order:
#  1. If started as root: align the `app` account with PUID/PGID, chown the
#     config and state directories (never /sites: that is your site tree),
#     drop privileges (setpriv) and re-exec. Not root: nothing to adjust.
#  2. Fail LOUD and EARLY when the state directory or the config directory is
#     not writable (bind-mount UID mismatch).
#  3. Pre-upgrade backup: when the image version differs from the one that last
#     ran on this state volume, copy config.toml and the manifests to
#     <state>/image-backups/<old>-to-<new>-<timestamp>/ (keep the last 5).
#     (TransForge's own <state>/backups/ holds translated files it replaced;
#     that is a different thing and is left alone.)
#  4. First start: seed config.toml from the template with the LLM endpoint
#     taken from LLM_BASE_URL. An existing config is never touched.
#  5. `docker compose run --rm app status --all` works: a first argument that
#     is not an executable (or starts with "-") is a transforge subcommand.
#  6. `schedule`: run `transforge $TRANSFORGE_SCHEDULE_CMD` every
#     $TRANSFORGE_INTERVAL seconds (the optional compose service).
set -eu

PUID="${PUID:-1000}"
PGID="${PGID:-1000}"
CFG_DIR="${HOME}/.config/transforge"
CFG="${CFG_DIR}/config.toml"
STATE="${HOME}/.local/state/transforge"
LLM_BASE_URL="${LLM_BASE_URL:-http://host.docker.internal:1234/v1}"
ENDPOINT="${LLM_BASE_URL%/}"
ENDPOINT="${ENDPOINT%/v1}"

log() { printf '%s transforge-entrypoint: %s\n' "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" "$*" >&2; }
die() { log "FATAL: $*"; exit 1; }

# ---- 1. privileges ----------------------------------------------------------
if [ "$(id -u)" = "0" ]; then
    log "started as root; chown config+state to ${PUID}:${PGID} and drop privileges"
    if [ "$(id -u app)" != "$PUID" ] || [ "$(id -g app)" != "$PGID" ]; then
        groupmod -o -g "$PGID" app 2>/dev/null || true
        usermod -o -u "$PUID" -g "$PGID" app 2>/dev/null || true
    fi
    mkdir -p "$STATE" "$CFG_DIR"
    for d in "$STATE" "$CFG_DIR"; do
        chown "$PUID:$PGID" "$d"
        if [ "$(find "$d" -maxdepth 1 -mindepth 1 ! -user "$PUID" 2>/dev/null | head -n1)" != "" ]; then
            log "ownership mismatch in $d: one-time recursive chown"
            chown -R "$PUID:$PGID" "$d"
        fi
    done
    mkdir -p /tmp
    exec setpriv --reuid="$PUID" --regid="$PGID" --init-groups --inh-caps=-all -- "$0" "$@"
fi

# ---- 2. writability --------------------------------------------------------
for d in "$STATE" "$CFG_DIR"; do
    mkdir -p "$d" 2>/dev/null || true
    if [ ! -w "$d" ]; then
        log "$d is not writable by UID $(id -u) (owned by $(stat -c '%u:%g' "$d" 2>/dev/null || echo '?'))."
        log "  Bind mount owned by another user? chown it to $(id -u):$(id -g), or use the PUID/PGID"
        log "  override (docs/deploy/docker.md, 'Volume permissions')."
        # The config mount is allowed to be read-only if a config already exists.
        if [ "$d" = "$CFG_DIR" ] && [ -f "$CFG" ]; then
            log "  (config exists and is readable: continuing read-only)"
            continue
        fi
        die "$d not writable"
    fi
done

# ---- 3. pre-upgrade backup --------------------------------------------------
state_files() {
    [ -f "$CFG" ] && echo "$CFG"
    find "$STATE" -maxdepth 1 -type f -name '*-manifest.json' 2>/dev/null
    return 0
}

pre_upgrade_backup() {
    marker="$STATE/.image-version"
    new="$(python -c "import importlib.metadata as m; print(m.version('transforge'))" 2>/dev/null || echo unknown)-${IMAGE_VERSION:-dev}"
    old="$(cat "$marker" 2>/dev/null || true)"
    if [ "$old" != "$new" ]; then
        files="$(state_files)"
        if [ -n "$files" ]; then
            tag_old="$(printf '%s' "${old:-unversioned}" | tr -c 'A-Za-z0-9._+\n-' '_')"
            tag_new="$(printf '%s' "$new" | tr -c 'A-Za-z0-9._+\n-' '_')"
            dest="$STATE/image-backups/${tag_old}-to-${tag_new}-$(date -u '+%Y%m%dT%H%M%SZ')"
            mkdir -p "$dest"
            printf '%s\n' "$files" | while IFS= read -r f; do cp -p "$f" "$dest/"; done
            log "image version changed (${old:-none} -> ${new}): config + manifests copied to ${dest}"
            ls -1dt "$STATE"/image-backups/*/ 2>/dev/null | tail -n +6 | while IFS= read -r d; do rm -rf "$d"; done
        fi
        printf '%s\n' "$new" > "$marker"
    fi
}
pre_upgrade_backup

# ---- 4. first-start config seed ---------------------------------------------
if [ ! -f "$CFG" ]; then
    if sed "s|@ENDPOINT@|${ENDPOINT}|" /usr/local/share/transforge/config.example.toml > "$CFG" 2>/dev/null; then
        log "seeded $CFG (endpoint ${ENDPOINT}); edit [sites.*] to point at your trees under /sites"
    else
        log "no config at $CFG and it could not be created; see docs/deploy/docker.md"
    fi
fi

# ---- 6. scheduler -----------------------------------------------------------
if [ "${1:-}" = "schedule" ]; then
    : "${TRANSFORGE_SCHEDULE_CMD:?set TRANSFORGE_SCHEDULE_CMD, e.g. 'run --site example'}"
    INTERVAL="${TRANSFORGE_INTERVAL:-3600}"
    HB=/tmp/heartbeat
    log "schedule every ${INTERVAL}s: transforge ${TRANSFORGE_SCHEDULE_CMD}"
    CHILD=""
    trap '[ -n "$CHILD" ] && kill "$CHILD" 2>/dev/null; exit 0' TERM INT
    while :; do
        # shellcheck disable=SC2086  # the command is deliberately word-split
        transforge ${TRANSFORGE_SCHEDULE_CMD} &
        CHILD=$!
        wait "$CHILD" || log "scheduled run exited non-zero (will retry next interval)"
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

# ---- 5. command -------------------------------------------------------------
if [ "$#" -eq 0 ] || [ "${1#-}" != "$1" ] || ! command -v "$1" >/dev/null 2>&1; then
    set -- transforge "$@"
fi
# exec: the app becomes the process that receives SIGTERM.
exec "$@"
