# docker-phpbb

[phpBB](https://www.phpbb.com/) container image, built on
[`serversideup/php`](https://serversideup.net/open-source/docker-php/).

This image powers phpBB on [PikaPods](https://www.pikapods.com) and is
maintained by the PikaPods team. It's published here for our users' reference
and the benefit of the wider community. To run your own phpBB pod from
$2.3/month, see
[pikapods.com/pods?run=phpbb](https://www.pikapods.com/pods?run=phpbb).

Published to both `ghcr.io/pikapods/docker-phpbb` and
`pikapods/docker-phpbb` (Docker Hub) — pick whichever registry you prefer.
Three tag patterns are pushed per build:

| Tag         | Mutability | Use for                                                  |
|-------------|------------|----------------------------------------------------------|
| `latest`    | mutable    | Most recent build of the most recent in-series stable    |
| `3.3.16`    | mutable    | Pin to a phpBB version; auto-receive base-image patches  |
| `3.3.16-r1` | immutable  | Byte-for-byte reproducibility; never reused              |

Source: https://github.com/pikapods/docker-phpbb

## Why this image

A small, maintainable phpBB image focused on simplicity: a short entrypoint
that runs the official `phpbbcli.php` installer headlessly, idempotent boot,
an in-container cron worker, and a daily auto-rebuild against upstream phpBB
releases.

## Quick start

The bundled `compose.yaml` brings up phpBB plus a MariaDB sidecar with zero
external dependencies — the fastest way to try the image:

```bash
git clone https://github.com/pikapods/docker-phpbb.git
cd docker-phpbb
docker compose up -d
# wait ~60s for first-boot install
curl -I http://localhost:8080/index.php   # → HTTP/1.1 200 OK
```

Default admin credentials are `admin` / `changeme` — change them before any
real deployment.

Against an existing database:

```bash
docker run -d --name phpbb \
  -v phpbb-data:/data \
  -e PHPBB_DB_DRIVER=mysqli \
  -e PHPBB_DB_HOST=db.internal \
  -e PHPBB_DB_NAME=phpbb \
  -e PHPBB_DB_USER=phpbb \
  -e PHPBB_DB_PASS=... \
  -e PHPBB_ADMIN_USER=admin \
  -e PHPBB_ADMIN_PASS=changeme \
  -e PHPBB_ADMIN_EMAIL=admin@example.com \
  -e PHPBB_BOARD_NAME="My Board" \
  -e PHPBB_SERVER_NAME=forum.example.com \
  -p 8080:8080 \
  ghcr.io/pikapods/docker-phpbb:latest
```

### Running on podman

The compose file and `docker run` examples work as-is under
`podman compose` / `podman run`. Three podman-specific notes:

- **Build:** `podman build --format docker …`. Podman defaults to OCI
  manifests, which silently drop the `HEALTHCHECK` instruction; docker
  format embeds it.
- **Rootless permissions:** rootless podman remaps UIDs, so the
  container's `www-data` (UID 82) isn't host UID 82. For bind mounts, add
  `--userns=keep-id:uid=82,gid=82`. Rootful podman behaves like docker.
  Full decision matrix in [User & permissions](#user--permissions).
- **Healthcheck inspection:** `podman healthcheck run <container>` runs
  the check on demand. Docker runs it automatically; inspect with
  `docker inspect --format '{{.State.Health.Status}}' <container>`.

## Environment variables

### Database

| Var                | Required | Purpose                                                      |
|--------------------|----------|--------------------------------------------------------------|
| `PHPBB_DB_DRIVER`  | yes      | `mysqli`, `postgres`, or `sqlite3`.                          |
| `PHPBB_DB_HOST`    | mysqli/postgres | DB hostname. For `sqlite3`, defaults to `/data/phpbb.sqlite3` (the absolute path on disk). |
| `PHPBB_DB_PORT`    | no       | DB port. Defaults to 3306 (mysqli) / 5432 (postgres) / empty (sqlite3). |
| `PHPBB_DB_NAME`    | yes      | DB name (or sqlite filename).                                |
| `PHPBB_DB_USER`    | mysqli/postgres | DB user.                                              |
| `PHPBB_DB_PASS`    | mysqli/postgres | DB password (may be empty).                           |
| `PHPBB_TABLE_PREFIX` | no     | Table name prefix. Defaults to `phpbb_`.                     |

### Admin (first boot only)

| Var                  | Required | Purpose                                  |
|----------------------|----------|------------------------------------------|
| `PHPBB_ADMIN_USER`   | yes      | Admin username.                          |
| `PHPBB_ADMIN_PASS`   | yes      | Admin password.                          |
| `PHPBB_ADMIN_EMAIL`  | yes      | Admin email.                             |

The admin is only seeded if `/data/config.php` is missing (i.e. the very
first boot). On subsequent boots these vars are ignored — change the admin
password through the ACP, not the env.

### Board / server

| Var                     | Required | Purpose                                                                  |
|-------------------------|----------|--------------------------------------------------------------------------|
| `PHPBB_BOARD_NAME`      | yes      | Forum title (shown in headers).                                          |
| `PHPBB_BOARD_DESC`      | no       | Forum description. Defaults to empty.                                    |
| `PHPBB_BOARD_LANG`      | no       | Default board language. Defaults to `en`.                                |
| `PHPBB_SERVER_NAME`     | yes      | Public hostname (no scheme), e.g. `forum.example.com`.                   |
| `PHPBB_SERVER_PROTOCOL` | no       | `http://` or `https://`. Defaults to `http://`.                          |
| `PHPBB_SERVER_PORT`     | no       | Public port. Defaults to `8080`.                                         |
| `PHPBB_SCRIPT_PATH`     | no       | Sub-path phpBB is mounted under. Defaults to `/`.                        |
| `PHPBB_COOKIE_SECURE`   | no       | `true` or `false`. Auto-derived from `PHPBB_SERVER_PROTOCOL` (`true` under `https://`); set explicitly to override. |

These are written into `/data/config.php` on first boot. To change them
later, edit the values via the ACP "Server settings" page.

### Cron worker

| Var                    | Default | Purpose                                                       |
|------------------------|---------|---------------------------------------------------------------|
| `ENABLE_PHPBB_CRON`    | `TRUE`  | Set `FALSE` to disable the in-container cron driver.          |
| `PHPBB_CRON_INTERVAL`  | `300`   | Seconds between `phpbbcli cron:run` invocations.              |

phpBB's cron tasks (prune logs, recalculate stats) only fire on user requests
by default. On a low-traffic board they may never run, so this image runs an
s6 longrun worker that invokes `php bin/phpbbcli.php cron:run` periodically.
Set `ENABLE_PHPBB_CRON=FALSE` if you want to drive cron from an external
scheduler (e.g. Kubernetes CronJob, host cron) instead.

## Mounts

| Path                   | Purpose                                                                 |
|------------------------|-------------------------------------------------------------------------|
| `/data`                | Persistent volume. Holds `config.php`, `files/`, `store/`, `ext/`, `avatars/`, `.installed`. |
| `/var/www/html`        | phpBB source. Baked at build time — do **not** bind-mount.              |

The image creates `/var/www/html/{config.php,files,store,ext,images/avatars/upload}`
as symlinks into `/data` at build time. Anything you write under `/data/`
(uploads, attachments, installed extensions, custom avatars) survives
container restarts and image upgrades.

`cache/` deliberately stays inside the image (transient — phpBB regenerates
it on demand).

### User & permissions

Both nginx and php-fpm run as `www-data` (**UID 82 / GID 82** — Alpine's
default, inherited from `serversideup/php:*-alpine`). How those writes
surface on the host depends on your runtime; pick the row that matches:

| Setup                             | What to do                                                                                               | Host-side ownership of `/data` writes |
|-----------------------------------|----------------------------------------------------------------------------------------------------------|---------------------------------------|
| Named volume (docker or podman)   | Nothing — daemon manages ownership. Default in `compose.yaml`.                                           | Inside daemon-managed volume; not user-visible. |
| Bind mount, rootful docker/podman | `chown -R 82:82 <host-dir>` before first boot.                                                           | `82:82`.                              |
| Bind mount, rootless podman       | Add `--userns=keep-id:uid=82,gid=82` to `podman run`.                                                    | Invoking host user's UID/GID.         |
| Custom-UID rebuild                | `docker build --build-arg WWW_DATA_UID=$(id -u) --build-arg WWW_DATA_GID=$(id -g) -t phpbb:local .`      | The UID baked at build time.          |

The bootstrap runs a preflight writability check on `/data` and refuses to
start with a readable error if ownership is wrong, rather than failing
cryptically deep in the install path.

## Ports

| Port | Purpose                                                                 |
|------|-------------------------------------------------------------------------|
| 8080 | HTTP (serversideup's unprivileged default).                             |

Behind a reverse proxy this is invisible to end users; document any direct
exposure if you're not using a proxy.

## Install lifecycle

phpBB's `install/phpbbcli.php` runs once on first boot from a YAML config
the bootstrap renders out of `PHPBB_*` env vars. After it succeeds:

1. `/data/config.php` is written by the installer (the canonical phpBB
   config — symlinked from `/var/www/html/config.php`).
2. The bootstrap removes `/var/www/html/install/` — phpBB does **not** do
   this itself, and leaving it accessible is a known security risk.
3. A `/data/.installed` marker is written, recording the phpBB version
   used for the install.

On subsequent boots:

- If `/data/config.php` is non-empty, the bootstrap skips the installer.
- If the image's phpBB version differs from `/data/.installed`, the
  bootstrap logs a notice. Patch-level upgrades within the same series
  (e.g. 3.3.16 → 3.3.17) run automatic schema migrations on the first web
  request — no action needed. Major upgrades (3.3 → 4.0) are not handled
  by this image yet.
- The `install/` directory is removed defensively every boot, in case a
  volume rollback restored it.

## Building locally

```bash
docker build \
  --build-arg PHPBB_VERSION=3.3.16 \
  --build-arg PHP_VERSION=8.3 \
  -t phpbb:test .
```

On podman, add `--format docker` — see the podman notes in Quick start for
why.

## License

The phpBB source is GPL-2.0; this image inherits that license.
