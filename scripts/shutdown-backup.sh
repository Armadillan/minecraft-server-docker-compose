#!/bin/sh
# Shutdown backup + joint retention for the Minecraft world.
#
# WHAT THIS IS FOR. The mc-backup sidecar flushes the world over RCON and
# archives it on a schedule - which needs the server alive. When the router
# scales the server down (no players for AUTO_SCALE_DOWN_AFTER), RCON is gone
# with it and the scheduled backup can no longer run. This watcher fills that
# gap: it watches the server container's state through the socket proxy and,
# on running -> stopped, tars the world directory - which the graceful stop
# has just flushed to disk, so no RCON dance is needed.
#
# It is also the single keeper of the JOINT retention policy that covers both
# backup triggers (the scheduled archives and these shutdown archives): at
# most MAX_PER_DAY archives per calendar day (newest kept) across at most
# MAX_DAYS distinct calendar days (newest kept). mc-backup's own age prune is
# set far out (PRUNE_BACKUPS_DAYS=3650) so it never fights this policy; it
# stays as a safety net and keeps the log line the e2e test asserts on.
#
# Archive names follow the sidecar's convention - world-YYYYMMDD-HHMMSS.tar.gz,
# stamped in the same TZ - with a -shutdown suffix, so both triggers'
# archives are interchangeable and fall under one retention pass.
set -u

PROXY_URL="${WATCHER_PROXY_URL:-http://socket-proxy:2375}"
FILTER="label=com.docker.compose.service%3Dminecraft-server"
DATA_DIR="${DATA_PATH:-/data}"
BACKUPS_DIR="${MINECRAFT_SERVER_BACKUPS_PATH:-/backups}"
POLL_INTERVAL="${WATCHER_POLL_INTERVAL:-60}"
MAX_PER_DAY="${WATCHER_MAX_PER_DAY:-2}"
MAX_DAYS="${WATCHER_MAX_DAYS:-3}"

[ "$MAX_PER_DAY" -ge 1 ] 2>/dev/null || MAX_PER_DAY=1
[ "$MAX_DAYS" -ge 1 ] 2>/dev/null || MAX_DAYS=1

# Prints "running" or "stopped"; fails when the proxy did not answer at all,
# which is not a state change and must not trigger a backup.
server_state() {
    resp="$(wget -qO- "${PROXY_URL}/containers/json?filters=${FILTER}" 2>/dev/null)" || return 1
    [ -n "$resp" ] || return 1
    if printf '%s' "$resp" | grep -q '"State":"running"'; then
        printf 'running'
    else
        printf 'stopped'
    fi
}

# Enforce the joint policy over every world-*.tar.gz in the archive dir.
enforce_retention() {
    cd "$BACKUPS_DIR" 2>/dev/null || return
    days="$(ls -1 world-*.tar.gz 2>/dev/null | sed -n 's/^world-\([0-9]\{8\}\)-.*/\1/p' | sort -u -r)"
    [ -n "$days" ] || return
    i=0
    for day in $days; do
        i=$((i + 1))
        if [ "$i" -gt "$MAX_DAYS" ]; then
            # Older than the newest MAX_DAYS distinct days: drop the whole day.
            for f in world-"$day"-*.tar.gz; do
                [ -e "$f" ] && rm -f "$f"
            done
            continue
        fi
        # Keep only the newest MAX_PER_DAY of this day (names sort by time).
        n=0
        for f in $(ls -1 world-"$day"-*.tar.gz 2>/dev/null | sort -r); do
            n=$((n + 1))
            if [ "$n" -gt "$MAX_PER_DAY" ]; then
                rm -f "$f"
            fi
        done
    done
}

shutdown_backup() {
    ts="$(date +"%Y%m%d-%H%M%S")"
    out="${BACKUPS_DIR}/world-${ts}-shutdown.tar.gz"
    # The server may have been woken again between the transition and now.
    # Taring a live world would archive a torn state, so skip this one; the
    # scheduled backup or the next shutdown covers it.
    state="$(server_state)" || state=""
    if [ "$state" = "running" ]; then
        echo "server came back up before the shutdown backup could start; skipping this one"
        return
    fi
    echo "server is stopped; archiving ${DATA_DIR} to ${out}"
    # Same excludes as the mc-backup sidecar (EXCLUDES=*.jar,cache,logs,*.tmp)
    # so the archives are interchangeable.
    if tar -czf "$out" --exclude='*.jar' --exclude=cache --exclude=logs --exclude='*.tmp' -C "$DATA_DIR" .; then
        echo "shutdown archive written: ${out}"
    else
        echo "shutdown archive FAILED: ${out}"
        rm -f "$out"
    fi
    enforce_retention
}

prev=""
echo "backup watcher started: poll=${POLL_INTERVAL}s max_per_day=${MAX_PER_DAY} max_days=${MAX_DAYS}"
while :; do
    state="$(server_state)" || state=""
    if [ -n "$state" ]; then
        if [ -n "$prev" ] && [ "$prev" = "running" ] && [ "$state" = "stopped" ]; then
            shutdown_backup
        fi
        prev="$state"
        enforce_retention
    fi
    sleep "$POLL_INTERVAL"
done
