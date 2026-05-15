#!/bin/sh
# phpBB bootstrap — runs once before s6 starts nginx + php-fpm.
# POSIX sh. Pipelines are avoided so phpbbcli exit status is never masked.
set -eu

APP_DIR=/var/www/html
DATA_DIR=/data
CONFIG_FILE=${DATA_DIR}/config.php
INSTALLED_MARKER=${DATA_DIR}/.installed
INSTALL_YML=/tmp/phpbb-install.yml
EXT_DIST=${APP_DIR}/ext.dist

PHPBB_VERSION_BUILT=${PHPBB_VERSION:-unknown}

log() { printf '[phpbb-bootstrap] %s\n' "$*" >&2; }
die() { log "ERROR: $*"; exit 1; }

# Always scrub the rendered installer YAML on exit — it holds DB + admin
# credentials. rm -f is silent on missing files, so this is safe even on the
# already-installed fast path that never renders the YAML.
trap 'rm -f "$INSTALL_YML"' EXIT

# ---------------------------------------------------------------------------
# Pre-flight: refuse a /data/config.php-as-directory layout (bad bind mount).
# ---------------------------------------------------------------------------
if [ -d "$CONFIG_FILE" ]; then
    cat >&2 <<EOF
ERROR: $CONFIG_FILE is a directory. This image expects it to be a regular file
       (the phpBB config), written by the installer on first boot.
       Most likely you bind-mounted a host path to /data/config.php instead of /data.
EOF
    exit 1
fi

# Preflight writability check on /data. Without this an unchowned bind-mount
# surfaces as a bare `mkdir: Permission denied` deep in the boot sequence.
if ! ( : > "$DATA_DIR/.write-test" ) 2>/dev/null; then
    cat >&2 <<EOF
ERROR: $DATA_DIR is not writable by the container (UID:GID $(id -u):$(id -g)).
       Ownership of the bind-mount target must match how the container sees it.
       The right fix depends on your runtime — see README "User & permissions":
         - rootful docker/podman bind mount: chown the host dir to $(id -u):$(id -g)
         - rootless podman: add --userns=keep-id:uid=$(id -u),gid=$(id -g)
         - or use a named volume
         - or rebuild with --build-arg WWW_DATA_UID=...
EOF
    exit 1
fi
rm -f "$DATA_DIR/.write-test"

# ---------------------------------------------------------------------------
# Ensure /data subtree exists (idempotent).
# ---------------------------------------------------------------------------
mkdir -p \
    "$DATA_DIR/files" \
    "$DATA_DIR/store" \
    "$DATA_DIR/ext" \
    "$DATA_DIR/avatars"

# ---------------------------------------------------------------------------
# Sync bundled extensions from the build-time stash. Granularity is
# <vendor>/<ext_name>/ — phpBB extensions live at ext/<vendor>/<ext_name>/
# and a single vendor (e.g. phpbb) can hold both bundled and user-installed
# extensions, so we replace per-extension, not per-vendor.
#
# Bundled extension dirs are overwritten every boot so a phpBB image bump
# that patches bundled-extension code (e.g. a security fix in
# ext/phpbb/viglink) actually lands on disk. Anything else under /data/ext —
# vendors absent from ext.dist, or extensions in a shared vendor namespace
# that aren't bundled — is left untouched, preserving user-installed
# extensions across image upgrades.
#
# Note: if upstream removes a previously-bundled extension, its dir survives
# in /data/ext (we only touch what's currently in ext.dist). Disable/remove
# via the ACP if that matters.
# ---------------------------------------------------------------------------
if [ -d "$EXT_DIST" ]; then
    for vendor_dir in "$EXT_DIST"/*/; do
        [ -d "$vendor_dir" ] || continue
        vendor=$(basename "$vendor_dir")
        mkdir -p "$DATA_DIR/ext/$vendor"
        for ext_dir in "$vendor_dir"*/; do
            [ -d "$ext_dir" ] || continue
            ext_name=$(basename "$ext_dir")
            target="$DATA_DIR/ext/$vendor/$ext_name"
            # rm + cp is non-atomic; a crash here leaves the bundled ext
            # missing and the next boot restores it. Acceptable trade-off
            # against pulling in rsync just for --delete semantics.
            rm -rf "$target"
            cp -r "$ext_dir" "$target"
        done
    done
fi

# ---------------------------------------------------------------------------
# If config.php already exists and is non-empty, treat as already installed.
# Subsequent web requests handle 3.3.x -> 3.3.x patch schema upgrades.
#
# Defensive install/ cleanup runs in BOTH branches (already-installed and
# post-fresh-install) — phpbbcli never removes it itself, and leaving it in
# the web root after install is a known security risk. Migrating an existing
# /data/config.php into a fresh image lands here; without unconditional
# cleanup, install/ would be web-reachable until an image rebuild.
# ---------------------------------------------------------------------------
if [ -s "$CONFIG_FILE" ]; then
    if [ -d "$APP_DIR/install" ]; then
        log "removing $APP_DIR/install (already installed; security hardening)"
        rm -rf "$APP_DIR/install"
    fi
    if [ -f "$INSTALLED_MARKER" ]; then
        installed_ver=$(cat "$INSTALLED_MARKER" 2>/dev/null || echo unknown)
        if [ "$installed_ver" != "$PHPBB_VERSION_BUILT" ]; then
            log "image phpBB version $PHPBB_VERSION_BUILT differs from installed $installed_ver;"
            log "  patch-level (3.3.x) schema upgrades run automatically on first web hit."
            log "  major upgrades (e.g. 3.3 -> 4.0) are not handled by this image yet."
        fi
    else
        log "config.php present but no $INSTALLED_MARKER; assuming pre-existing install"
        printf '%s\n' "$PHPBB_VERSION_BUILT" > "$INSTALLED_MARKER"
    fi
    log "phpBB already installed; skipping installer"
    log "bootstrap complete"
    exit 0
fi

# ---------------------------------------------------------------------------
# First-boot install path. Validate required env, render YAML, run phpbbcli.
# ---------------------------------------------------------------------------
: "${PHPBB_DB_DRIVER:?PHPBB_DB_DRIVER is required (mysqli|postgres|sqlite3)}"
: "${PHPBB_DB_NAME:?PHPBB_DB_NAME is required}"
: "${PHPBB_ADMIN_USER:?PHPBB_ADMIN_USER is required}"
: "${PHPBB_ADMIN_PASS:?PHPBB_ADMIN_PASS is required}"
: "${PHPBB_ADMIN_EMAIL:?PHPBB_ADMIN_EMAIL is required}"
: "${PHPBB_BOARD_NAME:?PHPBB_BOARD_NAME is required}"
: "${PHPBB_SERVER_NAME:?PHPBB_SERVER_NAME is required}"

case "$PHPBB_DB_DRIVER" in
    mysqli)
        : "${PHPBB_DB_HOST:?PHPBB_DB_HOST is required for mysqli}"
        : "${PHPBB_DB_USER:?PHPBB_DB_USER is required for mysqli}"
        PHPBB_DB_PASS=${PHPBB_DB_PASS:-}
        PHPBB_DB_PORT=${PHPBB_DB_PORT:-3306}
        ;;
    postgres)
        : "${PHPBB_DB_HOST:?PHPBB_DB_HOST is required for postgres}"
        : "${PHPBB_DB_USER:?PHPBB_DB_USER is required for postgres}"
        PHPBB_DB_PASS=${PHPBB_DB_PASS:-}
        PHPBB_DB_PORT=${PHPBB_DB_PORT:-5432}
        ;;
    sqlite3)
        # sqlite uses dbhost as the file path; default to /data.
        PHPBB_DB_HOST=${PHPBB_DB_HOST:-${DATA_DIR}/phpbb.sqlite3}
        PHPBB_DB_USER=${PHPBB_DB_USER:-}
        PHPBB_DB_PASS=${PHPBB_DB_PASS:-}
        PHPBB_DB_PORT=${PHPBB_DB_PORT:-}
        ;;
    *)
        die "unsupported PHPBB_DB_DRIVER='$PHPBB_DB_DRIVER' (expected mysqli|postgres|sqlite3)"
        ;;
esac

PHPBB_TABLE_PREFIX=${PHPBB_TABLE_PREFIX:-phpbb_}
PHPBB_BOARD_LANG=${PHPBB_BOARD_LANG:-en}
PHPBB_BOARD_DESC=${PHPBB_BOARD_DESC:-}
PHPBB_SERVER_PROTOCOL=${PHPBB_SERVER_PROTOCOL:-http://}
PHPBB_SERVER_PORT=${PHPBB_SERVER_PORT:-8080}
PHPBB_SCRIPT_PATH=${PHPBB_SCRIPT_PATH:-/}

# Default cookie_secure to true under HTTPS (so session cookies get the
# Secure flag), false otherwise. Operator can force either way via
# PHPBB_COOKIE_SECURE=true|false.
case "$PHPBB_SERVER_PROTOCOL" in
    https://*|https://) cookie_secure_default=true ;;
    *)                  cookie_secure_default=false ;;
esac
PHPBB_COOKIE_SECURE=${PHPBB_COOKIE_SECURE:-$cookie_secure_default}
case "$PHPBB_COOKIE_SECURE" in
    true|false) ;;
    *) die "PHPBB_COOKIE_SECURE must be 'true' or 'false', got '$PHPBB_COOKIE_SECURE'" ;;
esac

# ---------------------------------------------------------------------------
# Wait for DB. 30s deadline, fail fast on timeout.
# ---------------------------------------------------------------------------
case "$PHPBB_DB_DRIVER" in
    mysqli)
        log "waiting for mysqli at $PHPBB_DB_HOST:$PHPBB_DB_PORT (30s deadline)"
        deadline=$(( $(date +%s) + 30 ))
        while :; do
            if mysqladmin ping -h "$PHPBB_DB_HOST" -P "$PHPBB_DB_PORT" \
                    -u "$PHPBB_DB_USER" --password="$PHPBB_DB_PASS" \
                    --silent >/dev/null 2>&1; then
                break
            fi
            [ "$(date +%s)" -ge "$deadline" ] && \
                die "mysqli at $PHPBB_DB_HOST:$PHPBB_DB_PORT not reachable within 30s"
            sleep 1
        done
        ;;
    postgres)
        log "waiting for postgres at $PHPBB_DB_HOST:$PHPBB_DB_PORT (30s deadline)"
        deadline=$(( $(date +%s) + 30 ))
        while :; do
            if pg_isready -h "$PHPBB_DB_HOST" -p "$PHPBB_DB_PORT" \
                    -U "$PHPBB_DB_USER" >/dev/null 2>&1; then
                break
            fi
            [ "$(date +%s)" -ge "$deadline" ] && \
                die "postgres at $PHPBB_DB_HOST:$PHPBB_DB_PORT not reachable within 30s"
            sleep 1
        done
        ;;
    sqlite3)
        log "sqlite3 driver — no DB wait"
        ;;
esac
log "DB is reachable"

# ---------------------------------------------------------------------------
# Render install YAML. Single-quoted scalars are safest for arbitrary values;
# escape embedded single quotes by doubling per the YAML spec.
# ---------------------------------------------------------------------------
yq() {
    # yaml_quote: emit a single-quoted YAML scalar with proper '' escaping.
    val=$1
    escaped=$(printf '%s' "$val" | sed "s/'/''/g")
    printf "'%s'" "$escaped"
}

log "rendering installer config -> $INSTALL_YML"
{
    printf 'installer:\n'
    printf '    admin:\n'
    printf '        name: %s\n'     "$(yq "$PHPBB_ADMIN_USER")"
    printf '        password: %s\n' "$(yq "$PHPBB_ADMIN_PASS")"
    printf '        email: %s\n'    "$(yq "$PHPBB_ADMIN_EMAIL")"
    printf '    board:\n'
    printf '        lang: %s\n'         "$(yq "$PHPBB_BOARD_LANG")"
    printf '        name: %s\n'         "$(yq "$PHPBB_BOARD_NAME")"
    if [ -n "$PHPBB_BOARD_DESC" ]; then
        printf '        description: %s\n'  "$(yq "$PHPBB_BOARD_DESC")"
    fi
    printf '    database:\n'
    printf '        dbms: %s\n'         "$(yq "$PHPBB_DB_DRIVER")"
    printf '        dbhost: %s\n'       "$(yq "$PHPBB_DB_HOST")"
    printf '        dbport: %s\n'       "$(yq "$PHPBB_DB_PORT")"
    printf '        dbuser: %s\n'       "$(yq "$PHPBB_DB_USER")"
    printf '        dbpasswd: %s\n'     "$(yq "$PHPBB_DB_PASS")"
    printf '        dbname: %s\n'       "$(yq "$PHPBB_DB_NAME")"
    printf '        table_prefix: %s\n' "$(yq "$PHPBB_TABLE_PREFIX")"
    printf '    email:\n'
    printf '        enabled: false\n'
    printf '        smtp_delivery: false\n'
    printf "        smtp_host: ''\n"
    printf "        smtp_auth: ''\n"
    printf "        smtp_user: ''\n"
    printf "        smtp_pass: ''\n"
    printf '    server:\n'
    printf '        cookie_secure: %s\n' "$PHPBB_COOKIE_SECURE"
    printf '        server_protocol: %s\n' "$(yq "$PHPBB_SERVER_PROTOCOL")"
    printf '        force_server_vars: false\n'
    printf '        server_name: %s\n'     "$(yq "$PHPBB_SERVER_NAME")"
    printf '        server_port: %s\n'     "$PHPBB_SERVER_PORT"
    printf '        script_path: %s\n'     "$(yq "$PHPBB_SCRIPT_PATH")"
    printf '    extensions: []\n'
} > "$INSTALL_YML"

# Tighten perms — file holds DB + admin credentials.
chmod 600 "$INSTALL_YML" 2>/dev/null || true

# ---------------------------------------------------------------------------
# Run phpbbcli install. Fatal on non-zero. Tail goes to stderr.
# ---------------------------------------------------------------------------
log "running phpbbcli install"
if ! ( cd "$APP_DIR" && php install/phpbbcli.php install "$INSTALL_YML" ) >&2; then
    die "phpbbcli install failed"
fi

# Verify the symlink target was actually written.
if [ ! -s "$CONFIG_FILE" ]; then
    die "phpbbcli install completed but $CONFIG_FILE is missing or empty"
fi

# Wipe install/ — phpbbcli does NOT do this and it's a known security risk.
log "removing $APP_DIR/install (security hardening)"
rm -rf "$APP_DIR/install"

# Drop the marker.
printf '%s\n' "$PHPBB_VERSION_BUILT" > "$INSTALLED_MARKER"

# (rendered YAML scrubbed by the EXIT trap registered at top — no per-path
#  cleanup needed here)

log "phpBB $PHPBB_VERSION_BUILT installed successfully"
log "bootstrap complete"
