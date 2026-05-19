import json
import os
import subprocess

import pytest

IMAGE = os.environ["IMAGE"]


def _inspect():
    out = subprocess.run(
        ["docker", "inspect", IMAGE],
        capture_output=True, text=True, check=True,
    )
    return json.loads(out.stdout)[0]


@pytest.fixture(scope="session")
def inspect():
    return _inspect()


def _run(*args, check=False):
    return subprocess.run(
        ["docker", "run", "--rm", "--entrypoint=", IMAGE, *args],
        capture_output=True, text=True, check=check,
    )


class TestImageMetadata:
    def test_required_oci_labels(self, inspect):
        labels = inspect["Config"].get("Labels") or {}
        for key in (
            "org.opencontainers.image.source",
            "org.opencontainers.image.version",
            "org.opencontainers.image.licenses",
            "org.opencontainers.image.title",
        ):
            assert labels.get(key), f"missing OCI label: {key}"

    def test_runs_as_non_root(self, inspect):
        user = inspect["Config"].get("User", "")
        assert user in ("www-data", "82"), f"expected www-data/82, got {user!r}"

    def test_healthcheck_defined(self, inspect):
        assert inspect["Config"].get("Healthcheck"), "no Healthcheck defined"

    def test_exposes_8080(self, inspect):
        ports = inspect["Config"].get("ExposedPorts") or {}
        assert "8080/tcp" in ports, f"8080/tcp not exposed; got {list(ports)}"

    def test_image_size_under_limit(self, inspect):
        # phpBB is leaner than FreeScout (no composer install, no laravel
        # vendor tree), so we can hold a tighter ceiling.
        size_mb = inspect["Size"] / (1024 * 1024)
        assert size_mb < 800, f"image size {size_mb:.0f} MB exceeds 800 MB guardrail"

    def test_default_env_present(self, inspect):
        env = dict(e.split("=", 1) for e in inspect["Config"].get("Env") or [])
        assert env.get("AUTORUN_ENABLED") == "false"
        assert env.get("SSL_MODE") == "off"
        assert env.get("ENABLE_PHPBB_CRON") == "TRUE"
        assert env.get("APP_BASE_DIR") == "/var/www/html"
        assert env.get("PHPBB_CRON_INTERVAL") == "300"
        assert env.get("PHPBB_VERSION"), "PHPBB_VERSION env not set"


class TestImageFilesystem:
    @pytest.mark.parametrize("link,target", [
        ("/var/www/html/config.php", "/data/config.php"),
        ("/var/www/html/files", "/data/files"),
        ("/var/www/html/store", "/data/store"),
        ("/var/www/html/ext", "/data/ext"),
        ("/var/www/html/images/avatars/upload", "/data/avatars"),
    ])
    def test_data_symlinks(self, link, target):
        r = _run("readlink", link)
        assert r.returncode == 0, f"readlink {link} failed: {r.stderr}"
        assert r.stdout.strip() == target, f"{link} -> {r.stdout.strip()!r}, expected {target!r}"

    def test_data_dir_owned_by_www_data(self):
        r = _run("stat", "-c", "%U:%G", "/data")
        assert r.returncode == 0, r.stderr
        assert r.stdout.strip() == "www-data:www-data"

    @pytest.mark.parametrize("binary", [
        "php", "nginx", "unzip", "curl", "mysqladmin", "pg_isready",
    ])
    def test_runtime_binaries_present(self, binary):
        r = _run("which", binary)
        assert r.returncode == 0, f"{binary} not found on PATH"
        assert r.stdout.strip(), f"which {binary} returned empty"

    @pytest.mark.parametrize("ext", [
        "mysqli", "pdo_mysql", "pdo_pgsql", "pgsql", "pdo_sqlite",
        "sqlite3", "intl", "gd", "exif", "Zend OPcache", "zip",
    ])
    def test_php_extensions_loaded(self, ext):
        r = _run("php", "-m")
        assert r.returncode == 0, r.stderr
        modules = {line.strip() for line in r.stdout.splitlines() if line.strip()}
        assert ext in modules, f"PHP module {ext!r} not loaded; got {sorted(modules)}"

    def test_s6_cron_run_executable(self):
        r = _run("test", "-x", "/etc/s6-overlay/s6-rc.d/phpbb-cron/run")
        assert r.returncode == 0, "phpbb-cron run script missing or not executable"

    def test_s6_cron_depends_on_bootstrap(self):
        r = _run(
            "test", "-f",
            "/etc/s6-overlay/s6-rc.d/phpbb-cron/dependencies.d/20-phpbb-bootstrap",
        )
        assert r.returncode == 0, "cron dependency marker on bootstrap missing"

    def test_s6_cron_registered_in_user_bundle(self):
        r = _run(
            "test", "-f",
            "/etc/s6-overlay/s6-rc.d/user/contents.d/phpbb-cron",
        )
        assert r.returncode == 0, "phpbb-cron not registered in s6 user bundle"

    def test_s6_cron_uses_phpbbcli_not_curl(self):
        # phpBB 3.3.x /cron.php rejects bare hits with HTTP 400 (the route
        # needs per-task query params), so the worker must drive cron via
        # the CLI. Lock the wiring at the image-layer test lane.
        r = _run("cat", "/etc/s6-overlay/s6-rc.d/phpbb-cron/run")
        assert r.returncode == 0, r.stderr
        body = r.stdout
        # Strip shell comments before scanning for active commands — the
        # header comment intentionally explains *why* we no longer hit
        # /cron.php and would otherwise trigger the negative check.
        active = "\n".join(
            line for line in body.splitlines()
            if not line.lstrip().startswith("#")
        )
        assert "phpbbcli.php cron:run" in active, (
            "cron worker no longer invokes phpbbcli cron:run — the CLI path "
            "is the only way to drive cron without per-task query params"
        )
        assert "/cron.php" not in active, (
            "cron worker still references /cron.php in an executable line; "
            "phpBB 3.3.x returns HTTP 400 for bare hits to that route"
        )
        assert "curl" not in active, (
            "cron worker still shells out to curl; expected phpbbcli only"
        )

    def test_s6_bootstrap_oneshot_installed(self):
        # The base image's docker-php-serversideup-s6-init moves our
        # /etc/entrypoint.d/20-phpbb-bootstrap.sh into /etc/s6-overlay/scripts/
        # with a rename suffix we don't want to couple to — glob is deliberate.
        r = _run("sh", "-c", "ls /etc/s6-overlay/scripts/ | grep -E 'phpbb-bootstrap'")
        assert r.returncode == 0, (
            "no phpbb-bootstrap script in /etc/s6-overlay/scripts/ "
            f"(stdout={r.stdout!r}, stderr={r.stderr!r})"
        )

    def test_phpbb_app_present(self):
        for path in ("/var/www/html/common.php", "/var/www/html/index.php"):
            r = _run("test", "-f", path)
            assert r.returncode == 0, f"{path} missing"

    def test_bundled_ext_stashed(self):
        # Regression guard: the Dockerfile renames ext/ to ext.dist/ so the
        # bootstrap can seed /data/ext on first boot. If a future refactor
        # accidentally drops it, fresh installs lose phpBB's bundled
        # extensions (e.g. ext/phpbb/viglink in 3.3.x).
        r = _run("test", "-d", "/var/www/html/ext.dist")
        assert r.returncode == 0, "/var/www/html/ext.dist missing — bundled extensions would be lost on first boot"
        # And it must contain at least one bundled namespace dir.
        r = _run("sh", "-c", "ls -1 /var/www/html/ext.dist | head -1")
        assert r.stdout.strip(), "/var/www/html/ext.dist is empty"


# Marked runtime: a full image rebuild (~30s with buildkit cache, minutes
# cold). Lives outside TestImageFilesystem so `-m 'not runtime'` keeps the
# fast image lane fast.
@pytest.mark.runtime
class TestCustomUidRebuild:
    """Rebuild with --build-arg WWW_DATA_UID/GID and verify the new UID
    actually owns /data. Regression guard: set-file-permissions only touches
    a hardcoded path list, so /data needs an explicit chown in the Dockerfile
    or the rebuilt image's www-data can't write to its own volume.
    """

    UID = "1000"
    GID = "1000"

    @pytest.fixture(scope="class")
    def image(self):
        ctx = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        tag = f"phpbb-uid{self.UID}-test"
        r = subprocess.run(
            ["docker", "build",
             "--build-arg", f"WWW_DATA_UID={self.UID}",
             "--build-arg", f"WWW_DATA_GID={self.GID}",
             "-t", tag, ctx],
            capture_output=True, text=True,
        )
        if r.returncode != 0:
            pytest.fail(
                f"docker build failed (rc={r.returncode})\n"
                f"--- stdout ---\n{r.stdout}\n--- stderr ---\n{r.stderr}"
            )
        try:
            yield tag
        finally:
            subprocess.run(["docker", "rmi", "-f", tag], capture_output=True)

    def test_www_data_user_remapped(self, image):
        r = subprocess.run(
            ["docker", "run", "--rm", "--entrypoint=", image,
             "id", "-u", "www-data"],
            capture_output=True, text=True, check=True,
        )
        assert r.stdout.strip() == self.UID

    def test_data_dir_remapped(self, image):
        r = subprocess.run(
            ["docker", "run", "--rm", "--entrypoint=", image,
             "stat", "-c", "%u:%g", "/data"],
            capture_output=True, text=True, check=True,
        )
        assert r.stdout.strip() == f"{self.UID}:{self.GID}"
