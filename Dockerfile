# phpBB image — self-maintained, derived from serversideup/php.
# See README.md for design notes and usage.
#
# Build:
#   podman build \
#     --build-arg PHPBB_VERSION=3.3.16 \
#     --build-arg PHP_VERSION=8.3 \
#     -t ghcr.io/pikapods/docker-phpbb:3.3.16-php8.3 .

ARG PHP_VERSION=8.3
FROM serversideup/php:${PHP_VERSION}-fpm-nginx-alpine

ARG PHPBB_VERSION=3.3.16

LABEL org.opencontainers.image.title="phpBB" \
      org.opencontainers.image.description="Self-maintained phpBB container" \
      org.opencontainers.image.source="https://github.com/pikapods/docker-phpbb" \
      org.opencontainers.image.licenses="GPL-2.0" \
      org.opencontainers.image.version="${PHPBB_VERSION}"

USER root

# Runtime + build dependencies.
# Runtime: mariadb-client (mysqladmin ping), postgresql-client (pg_isready),
# tzdata, curl (cron worker + healthcheck), unzip (release zip extraction).
RUN apk add --no-cache \
        unzip \
        mariadb-client \
        postgresql-client \
        tzdata \
        curl \
    && install-php-extensions \
        mysqli \
        pdo_mysql \
        pdo_pgsql \
        pgsql \
        pdo_sqlite \
        sqlite3 \
        intl \
        gd \
        exif \
        opcache \
        zip

# Fetch the official release zip (vendor/ pre-bundled — no composer step needed).
# The archive expands to a top-level phpBB3/ directory; flatten into /var/www/html
# and strip docs/ to trim the image.
RUN series="${PHPBB_VERSION%.*}" \
    && curl -fsSL -o /tmp/phpbb.zip \
         "https://download.phpbb.com/pub/release/${series}/${PHPBB_VERSION}/phpBB-${PHPBB_VERSION}.zip" \
    && unzip -q /tmp/phpbb.zip -d /tmp/phpbb-extract \
    && rm -rf /var/www/html \
    && mv /tmp/phpbb-extract/phpBB3 /var/www/html \
    && rm -rf /tmp/phpbb.zip /tmp/phpbb-extract /var/www/html/docs

# Replace persistent paths with symlinks into /data.
# Targets do not resolve until /data is populated at runtime — fine; the
# bootstrap script mkdir -p's them on first boot.
#
# ext/ subtlety: phpBB ships a bundled tree (e.g. ext/phpbb/viglink). A
# straight `rm -rf ext && symlink to empty /data/ext` would erase it. Stash
# the bundled tree to /var/www/html/ext.dist; the bootstrap seeds it into
# /data/ext on first boot (`cp -rn` so user-installed extensions win).
#
# /data itself must exist and be owned by www-data: the container runs as a
# non-root user (UID 82 on Alpine) which cannot create /data under /.
#
# cache/ stays in the image (transient — phpBB regenerates it).
RUN rm -f /var/www/html/config.php \
    && rm -rf /var/www/html/files /var/www/html/store \
              /var/www/html/images/avatars/upload \
    && mv /var/www/html/ext /var/www/html/ext.dist \
    && ln -s /data/config.php /var/www/html/config.php \
    && ln -s /data/files /var/www/html/files \
    && ln -s /data/store /var/www/html/store \
    && ln -s /data/ext /var/www/html/ext \
    && ln -s /data/avatars /var/www/html/images/avatars/upload \
    && mkdir -p /data \
    && chown www-data:www-data /data \
    && chown -R www-data:www-data /var/www/html

# Build-arg UID/GID override. The base image fixes www-data at 82:82; rebuild
# with --build-arg WWW_DATA_UID=$(id -u) --build-arg WWW_DATA_GID=$(id -g) for
# bind-mount UX without host-side chown. Guarded so the default-build path
# adds no extra layer work. See README "User & permissions".
ARG WWW_DATA_UID=82
ARG WWW_DATA_GID=82
RUN if [ "$WWW_DATA_UID" != "82" ] || [ "$WWW_DATA_GID" != "82" ]; then \
        docker-php-serversideup-set-id www-data "${WWW_DATA_UID}:${WWW_DATA_GID}" \
     && docker-php-serversideup-set-file-permissions --owner "${WWW_DATA_UID}:${WWW_DATA_GID}" \
     && chown "${WWW_DATA_UID}:${WWW_DATA_GID}" /data; \
    fi

VOLUME /data

# Overlay our entrypoint hook + s6 cron service + nginx site config.
COPY rootfs/ /

# - chmod *before* docker-php-serversideup-s6-init: the init tool moves
#   /etc/entrypoint.d/*.sh into /etc/s6-overlay/scripts/ and renames them, so
#   chmod afterwards at the original path would fail.
# - chown /etc/nginx to www-data: ServerSideUp's 10-init-webserver-config
#   runs as www-data and renders /etc/nginx/nginx.conf at boot. After our
#   COPY rootfs/ the directory ends up root-owned and nginx fails to start
#   with "Permission denied" opening nginx.conf.
RUN chmod +x /etc/entrypoint.d/20-phpbb-bootstrap.sh \
             /etc/s6-overlay/s6-rc.d/phpbb-cron/run \
    && chown -R www-data:www-data /etc/nginx \
    && docker-php-serversideup-s6-init

# Image defaults.
# AUTORUN_ENABLED=false: we own the boot sequence.
# SSL_MODE=off: TLS terminates at the reverse proxy.
# ENABLE_PHPBB_CRON=TRUE: phpBB cron tasks (prune, stats) need a periodic hit.
# PHPBB_CRON_INTERVAL=300: 5-minute cadence for the in-container worker.
ENV AUTORUN_ENABLED=false \
    SSL_MODE=off \
    ENABLE_PHPBB_CRON=TRUE \
    APP_BASE_DIR=/var/www/html \
    NGINX_WEBROOT=/var/www/html \
    PHPBB_CRON_INTERVAL=300 \
    PHPBB_VERSION=${PHPBB_VERSION}

# Health endpoint hits index.php (the phpBB main board page, returns 200
# once installed). start-period covers first-boot install + nginx warmup.
# Using /index.php (not /app.php/) sidesteps the need for a custom
# `\.php(/|$)` nginx location override on top of the serversideup default.
HEALTHCHECK --interval=30s --timeout=5s --start-period=180s --retries=3 \
    CMD curl -fsS http://localhost:8080/index.php -o /dev/null || exit 1

EXPOSE 8080

USER www-data
