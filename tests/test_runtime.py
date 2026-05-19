import http.cookiejar
import json
import os
import re
import secrets
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request

import pytest

pytestmark = pytest.mark.runtime

IMAGE = os.environ["IMAGE"]
READY_DEADLINE_S = 240   # first-boot install can take ~30s on top of warmup
HEALTHY_DEADLINE_S = 120


def _sh(*args, check=True, capture=True):
    return subprocess.run(
        list(args),
        capture_output=capture, text=True, check=check,
    )


def _exec(container, *args, check=False):
    return subprocess.run(
        ["docker", "exec", container, *args],
        capture_output=True, text=True, check=check,
    )


def _wait_db_ready(container, kind, deadline_s=60):
    end = time.time() + deadline_s
    while time.time() < end:
        if kind == "mariadb":
            r = _exec(container, "healthcheck.sh", "--connect", "--innodb_initialized")
        elif kind == "postgres":
            r = _exec(container, "pg_isready", "-U", "postgres")
        else:
            raise ValueError(kind)
        if r.returncode == 0:
            return
        time.sleep(1)
    raise RuntimeError(f"{kind} container {container} not ready within {deadline_s}s")


def _http_get(url, timeout=10, opener=None):
    req = urllib.request.Request(url)
    if opener is None:
        return urllib.request.urlopen(req, timeout=timeout)
    return opener.open(req, timeout=timeout)


def _wait_http_200(url, deadline_s):
    end = time.time() + deadline_s
    last_err = None
    while time.time() < end:
        try:
            with _http_get(url, timeout=5) as r:
                if r.status == 200:
                    return
                last_err = f"status={r.status}"
        except (urllib.error.URLError, ConnectionError, TimeoutError) as e:
            last_err = repr(e)
        time.sleep(2)
    raise RuntimeError(f"{url} did not return 200 within {deadline_s}s (last={last_err})")


def _host_port(container, container_port):
    r = _sh("docker", "port", container, container_port)
    line = r.stdout.splitlines()[0]
    return int(line.rsplit(":", 1)[1])


def _start_db(kind, name, network):
    if kind == "mariadb":
        _sh(
            "docker", "run", "-d", "--name", name, "--network", network,
            "-e", "MARIADB_ROOT_PASSWORD=root",
            "-e", "MARIADB_DATABASE=phpbb",
            "-e", "MARIADB_USER=phpbb",
            "-e", "MARIADB_PASSWORD=test",
            "mariadb:11",
        )
    elif kind == "postgres":
        _sh(
            "docker", "run", "-d", "--name", name, "--network", network,
            "-e", "POSTGRES_PASSWORD=test",
            "-e", "POSTGRES_USER=phpbb",
            "-e", "POSTGRES_DB=phpbb",
            "postgres:16",
        )
    else:
        raise ValueError(kind)
    _wait_db_ready(name, kind)


def _start_phpbb(name, network, db_kind, db_host):
    if db_kind == "mariadb":
        env = [
            "PHPBB_DB_DRIVER=mysqli",
            f"PHPBB_DB_HOST={db_host}",
            "PHPBB_DB_NAME=phpbb",
            "PHPBB_DB_USER=phpbb",
            "PHPBB_DB_PASS=test",
        ]
    elif db_kind == "postgres":
        env = [
            "PHPBB_DB_DRIVER=postgres",
            f"PHPBB_DB_HOST={db_host}",
            "PHPBB_DB_NAME=phpbb",
            "PHPBB_DB_USER=phpbb",
            "PHPBB_DB_PASS=test",
        ]
    else:
        raise ValueError(db_kind)
    cmd = ["docker", "run", "-d", "--name", name, "--network", network]
    for e in env:
        cmd += ["-e", e]
    # podman rejects `-p 0:8080` (it wants a real port); docker treats 0 as
    # "pick any free port". Use a random high port so we work under both,
    # and still avoid collisions when the mariadb + postgres fixtures share
    # a session.
    host_port = secrets.randbelow(20000) + 30000
    cmd += [
        "-e", "PHPBB_ADMIN_USER=admin",
        "-e", "PHPBB_ADMIN_PASS=changeme",
        "-e", "PHPBB_ADMIN_EMAIL=admin@smoke.local",
        "-e", "PHPBB_BOARD_NAME=Smoke Test Board",
        "-e", "PHPBB_SERVER_NAME=localhost",
        "-e", "PHPBB_CRON_INTERVAL=10",
        "-p", f"{host_port}:8080",
        IMAGE,
    ]
    _sh(*cmd)


def _make_stack(db_kind):
    suffix = secrets.token_hex(4)
    net = f"phpbb-net-{db_kind}-{suffix}"
    db = f"db-{db_kind}-{suffix}"
    bb = f"bb-{db_kind}-{suffix}"

    _sh("docker", "network", "create", net)
    try:
        _start_db(db_kind, db, net)
        _start_phpbb(bb, net, db_kind, db)
        port = _host_port(bb, "8080")
        try:
            _wait_http_200(f"http://127.0.0.1:{port}/index.php", READY_DEADLINE_S)
        except RuntimeError:
            print(_sh("docker", "logs", bb, check=False).stdout)
            print(_sh("docker", "logs", bb, check=False).stderr)
            raise
        yield {"bb": bb, "db": db, "net": net, "port": port, "kind": db_kind}
    finally:
        for n in (bb, db):
            subprocess.run(["docker", "rm", "-f", n], capture_output=True)
        subprocess.run(["docker", "network", "rm", net], capture_output=True)


@pytest.fixture(scope="session")
def stack():
    yield from _make_stack("mariadb")


@pytest.fixture(scope="session")
def stack_postgres():
    yield from _make_stack("postgres")


# ---------------------------------------------------------------------------
# MariaDB stack tests
# ---------------------------------------------------------------------------

def test_index_php_responds_200(stack):
    with _http_get(f"http://127.0.0.1:{stack['port']}/index.php") as r:
        assert r.status == 200
        body = r.read().decode("utf-8", errors="replace")
    # The seeded board name should appear in the rendered page header.
    assert "Smoke Test Board" in body or "phpBB" in body


def test_install_dir_removed(stack):
    r = _exec(stack["bb"], "test", "-d", "/var/www/html/install")
    assert r.returncode != 0, "/var/www/html/install still present after first-boot install"


def test_config_php_persisted(stack):
    r = _exec(stack["bb"], "test", "-s", "/data/config.php")
    assert r.returncode == 0, "/data/config.php missing or empty"


def test_installed_marker_present(stack):
    r = _exec(stack["bb"], "test", "-f", "/data/.installed")
    assert r.returncode == 0, "/data/.installed marker missing"


def test_bundled_ext_seeded_into_data(stack):
    # Bootstrap copies ext.dist into /data/ext at <vendor>/<ext_name>/
    # granularity. If the build's ext.dist is non-empty, every bundled
    # extension dir should appear in /data/ext.
    listing = _exec(
        stack["bb"], "sh", "-c",
        # Print "vendor/ext_name" for every bundled extension dir.
        "cd /var/www/html/ext.dist && for v in */; do "
        "  for e in \"$v\"*/; do "
        "    [ -d \"$e\" ] && printf '%s\\n' \"${e%/}\"; "
        "  done; "
        "done",
    )
    assert listing.returncode == 0, listing.stderr
    bundled = {e for e in listing.stdout.splitlines() if e}
    assert bundled, "ext.dist contains no <vendor>/<ext_name> dirs"

    for path in bundled:
        r = _exec(stack["bb"], "test", "-d", f"/data/ext/{path}")
        assert r.returncode == 0, f"bundled extension {path} not seeded into /data/ext"


def test_ext_sync_overwrites_bundled_preserves_user(stack):
    # Two regressions in one boot cycle (each container restart costs ~30s):
    #   1. Bundled extension files must be overwritten from ext.dist on
    #      every boot, so phpBB image bumps that patch bundled-extension
    #      code (e.g. a security fix in ext/phpbb/viglink) actually land
    #      on disk. The previous `cp -rn` (no-clobber) implementation
    #      pinned bundled code to whatever shipped on first boot.
    #   2. Vendor namespaces NOT in ext.dist (user-installed extensions)
    #      must survive across boots.
    bb = stack["bb"]

    # --- Setup: taint a bundled file, plant a fake user extension ------
    pick = _exec(
        bb, "sh", "-c",
        "find /var/www/html/ext.dist -mindepth 3 -maxdepth 3 -type f -name '*.php' "
        "| head -1",
    )
    assert pick.returncode == 0 and pick.stdout.strip(), (
        f"could not locate a bundled .php file in ext.dist (stdout={pick.stdout!r})"
    )
    src_path = pick.stdout.strip()
    rel = src_path[len("/var/www/html/ext.dist/"):]    # vendor/ext_name/path/file.php
    data_path = f"/data/ext/{rel}"

    pristine = _exec(bb, "sha256sum", src_path)
    assert pristine.returncode == 0, pristine.stderr
    expected_hash = pristine.stdout.split()[0]

    seeded = _exec(bb, "sha256sum", data_path)
    assert seeded.returncode == 0, f"{data_path} missing before taint: {seeded.stderr}"
    assert seeded.stdout.split()[0] == expected_hash, "data file did not match ext.dist before taint"

    taint = _exec(bb, "sh", "-c", f"echo 'tainted' >> {data_path}")
    assert taint.returncode == 0, taint.stderr

    user_sentinel = "/data/ext/acme-test/widget/composer.json"
    setup = _exec(
        bb, "sh", "-c",
        f"mkdir -p $(dirname {user_sentinel}) && echo '{{\"name\":\"acme-test/widget\"}}' > {user_sentinel}",
    )
    assert setup.returncode == 0, setup.stderr

    # --- Restart and re-wait ------------------------------------------------
    _sh("docker", "restart", bb)
    port = _host_port(bb, "8080")
    try:
        _wait_http_200(f"http://127.0.0.1:{port}/index.php", READY_DEADLINE_S)
    except RuntimeError:
        print(_sh("docker", "logs", bb, check=False).stdout)
        raise

    # --- Assert: bundled restored, user preserved --------------------------
    restored = _exec(bb, "sha256sum", data_path)
    assert restored.returncode == 0, restored.stderr
    assert restored.stdout.split()[0] == expected_hash, (
        f"bundled extension file {data_path} was not restored from ext.dist on restart "
        f"(expected {expected_hash}, got {restored.stdout.split()[0]})"
    )

    r = _exec(bb, "test", "-f", user_sentinel)
    assert r.returncode == 0, (
        f"user extension {user_sentinel} disappeared after restart — sync logic is wiping non-bundled vendors"
    )


def test_config_stable_across_restart(stack):
    bb = stack["bb"]
    before = _exec(bb, "sha256sum", "/data/config.php")
    assert before.returncode == 0, before.stderr
    _sh("docker", "restart", bb)
    # `-p 0:8080` makes the host port ephemeral; re-query post-restart.
    port = _host_port(bb, "8080")
    try:
        _wait_http_200(f"http://127.0.0.1:{port}/index.php", READY_DEADLINE_S)
    except RuntimeError:
        print(_sh("docker", "logs", bb, check=False).stdout)
        raise
    after = _exec(bb, "sha256sum", "/data/config.php")
    assert after.returncode == 0
    assert before.stdout == after.stdout, (
        f"config.php changed across restart:\nbefore={before.stdout!r}\nafter={after.stdout!r}"
    )
    # And the install dir must still be gone (defensive cleanup runs every boot).
    r = _exec(bb, "test", "-d", "/var/www/html/install")
    assert r.returncode != 0, "/var/www/html/install reappeared after restart"


def test_logs_clean(stack):
    logs = _sh("docker", "logs", stack["bb"], check=False)
    combined = logs.stdout + logs.stderr
    bad = re.findall(r"RuntimeException|PHP Fatal", combined)
    assert not bad, f"bad patterns in container logs: {bad[:5]}"


def test_cron_longrun_alive(stack):
    # Read /proc/<pid>/cmdline directly — busybox `ps` truncates args for
    # shebang-launched scripts, so the run-script path doesn't appear in
    # `ps` output. /proc cmdline contains the kernel's view of argv.
    r = _exec(
        stack["bb"], "sh", "-c",
        "cat /proc/[0-9]*/cmdline 2>/dev/null | tr '\\0' '\\n' "
        "| grep -qF phpbb-cron/run",
    )
    assert r.returncode == 0, (
        "cron longrun process not present in /proc cmdlines "
        f"(stdout={r.stdout!r}, stderr={r.stderr!r})"
    )


def test_cron_cli_runs_successfully(stack):
    # Sanity-check the exact command the s6 worker invokes. If phpBB's CLI
    # cron command moves or changes name across versions this will catch it
    # before the silent-loop regression we just shipped a fix for.
    r = _exec(
        stack["bb"], "sh", "-c",
        "cd /var/www/html && php bin/phpbbcli.php cron:run --no-interaction --no-ansi",
    )
    assert r.returncode == 0, (
        f"phpbbcli cron:run failed (rc={r.returncode}) "
        f"stdout={r.stdout!r} stderr={r.stderr!r}"
    )


def test_cron_php_endpoint_not_polled(stack):
    # Regression guard for the curl-/cron.php worker. phpBB 3.3.x returns
    # HTTP 400 for bare hits, so the old worker was effectively a no-op —
    # any future refactor that re-introduces it must fail loudly here.
    # Fixture sets PHPBB_CRON_INTERVAL=10; wait >2 intervals on a fresh log
    # tail so we only inspect lines emitted during this test.
    baseline = _sh("docker", "logs", stack["bb"], check=False)
    start_len = len(baseline.stdout) + len(baseline.stderr)
    time.sleep(25)
    after = _sh("docker", "logs", stack["bb"], check=False)
    new = (after.stdout + after.stderr)[start_len:]
    assert "/cron.php" not in new, (
        "cron worker is still hitting /cron.php; expected CLI invocation only"
    )


def test_healthcheck_reports_healthy(stack):
    end = time.time() + HEALTHY_DEADLINE_S
    last = None
    while time.time() < end:
        r = _sh("docker", "inspect", "--format", "{{json .State.Health}}", stack["bb"])
        health = json.loads(r.stdout)
        if not health:
            pytest.skip("image has no HEALTHCHECK or daemon does not surface health")
        last = health.get("Status")
        if last == "healthy":
            return
        if last == "unhealthy":
            pytest.fail(f"container went unhealthy: {health.get('Log', [])[-1:]!r}")
        time.sleep(3)
    pytest.fail(f"healthcheck still {last!r} after {HEALTHY_DEADLINE_S}s")


# ---------------------------------------------------------------------------
# Admin login flow — exercises the full request stack against the seeded user.
# phpBB protects the login form with a creation_time/form_token pair and a
# session id (sid) cookie; we GET the form first to harvest both, then POST
# credentials with the same cookie jar.
# ---------------------------------------------------------------------------

_HIDDEN_RE = re.compile(
    r'<input[^>]*type="hidden"[^>]*name="(creation_time|form_token|sid)"[^>]*value="([^"]*)"',
    re.IGNORECASE,
)


def test_admin_can_log_in(stack):
    # Earlier session-scoped tests `docker restart` the container; with
    # `-p 0:8080` the host port is reassigned on each restart, so the
    # fixture's cached value goes stale. Re-query before connecting.
    port = _host_port(stack["bb"], "8080")
    base = f"http://127.0.0.1:{port}"
    jar = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))

    # Hit /index.php first so phpBB issues a stable guest session before the
    # login form is rendered. Going straight to ucp.php?mode=login as the very
    # first request can land in a state where the form_token generated on GET
    # doesn't validate on POST (observed after the in-session container
    # restarts performed by earlier tests).
    with opener.open(f"{base}/index.php", timeout=10) as r:
        r.read()

    # 1. GET login form, harvest hidden tokens + session cookie.
    with opener.open(f"{base}/ucp.php?mode=login", timeout=10) as r:
        assert r.status == 200
        body = r.read().decode("utf-8", errors="replace")

    hidden = dict(_HIDDEN_RE.findall(body))
    # creation_time + form_token are mandatory for phpBB's CSRF check; sid
    # may be absent on the form (it travels in the cookie) — don't require it.
    assert "creation_time" in hidden, "creation_time hidden field not found in login form"
    assert "form_token" in hidden, "form_token hidden field not found in login form"

    # phpBB's check_form_key() requires `time() - creation_time` to be > 0
    # (the `$diff &&` guard treats 0 as a bot-like instant submission). In CI
    # the GET+POST can complete within the same wall-clock second and the
    # token validates as invalid — phpBB then renders "Form submission
    # invalid", which is a 200 with no marker the credentials check would
    # produce. Sleeping past the second boundary deflakes it.
    time.sleep(1.1)

    # 2. POST credentials with the harvested tokens. The login button name is
    # `login`; phpBB checks for its presence to dispatch the auth path.
    payload = {
        "username": "admin",
        "password": "changeme",
        "redirect": "index.php",
        "creation_time": hidden["creation_time"],
        "form_token": hidden["form_token"],
        "login": "Login",
    }
    if "sid" in hidden:
        payload["sid"] = hidden["sid"]

    data = urllib.parse.urlencode(payload).encode()
    req = urllib.request.Request(f"{base}/ucp.php?mode=login", data=data, method="POST")
    req.add_header("Content-Type", "application/x-www-form-urlencoded")

    with opener.open(req, timeout=10) as r:
        # phpBB returns 200 with a "you have been successfully logged in"
        # meta-refresh page on success, or 200 with a visible error string
        # on failure. Status alone is not enough — check the body.
        assert r.status == 200
        body = r.read().decode("utf-8", errors="replace")

    lower = body.lower()
    bad_markers = (
        "incorrect password",
        "incorrect username",
        "you have entered an invalid",
        "form submission invalid",
    )
    for marker in bad_markers:
        assert marker not in lower, f"login appears to have failed (marker={marker!r})"
    # On success phpBB issues a phpbb3_*_u cookie set to the user id (>1 = real user, 1 = anon).
    user_cookie = next(
        (c for c in jar if c.name.endswith("_u") and c.name.startswith("phpbb")),
        None,
    )
    assert user_cookie is not None, f"no phpbb*_u cookie issued; cookies={[c.name for c in jar]}"
    assert user_cookie.value not in ("", "1"), (
        f"phpbb*_u cookie still anonymous after login: {user_cookie.value!r}"
    )


# ---------------------------------------------------------------------------
# Postgres variant — same install + 200 check, catches DB-driver regressions.
# ---------------------------------------------------------------------------

def test_postgres_variant(stack_postgres):
    with _http_get(f"http://127.0.0.1:{stack_postgres['port']}/index.php") as r:
        assert r.status == 200
    r = _exec(stack_postgres["bb"], "test", "-s", "/data/config.php")
    assert r.returncode == 0, "config.php missing under postgres install"
    r = _exec(stack_postgres["bb"], "test", "-f", "/data/.installed")
    assert r.returncode == 0, ".installed marker missing under postgres install"
