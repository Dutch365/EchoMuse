"""A local replacement does not share the installed add-on's data directory.

Supervisor's Addon.path_data is addons/data/<repository>_<slug>
(home-assistant/supervisor addon.py). A git store's repository id is the
first 8 hex characters of SHA-1 of its URL. The wilbowes store URL is
acc06409, so the installed add-on is acc06409_controller. A local folder
is repository "local", so slug "controller" becomes local_controller.
Starting that empty directory mints a new CA.
"""

import hashlib
import subprocess
import sys
from pathlib import Path

import yaml

CONTROLLER = Path(__file__).resolve().parents[1]
ROOT = CONTROLLER.parent
WILBOWES = "https://github.com/wilbowes/EchoMuse"
FORK = "https://github.com/Dutch365/EchoMuse"


def _repo_prefix(url: str) -> str:
    return hashlib.sha1(url.lower().encode()).hexdigest()[:8]


def test_committed_addon_slug_is_the_upstream_slug():
    text = (CONTROLLER / "config.yaml").read_text()
    config = yaml.safe_load(text)
    assert config["slug"] == "controller"
    assert 'DB_PATH: "/data/echomuse.db"' in text
    # The published image must still start on an empty first install.
    assert "EM_REQUIRE_EXISTING_DATA" not in text
    repo = yaml.safe_load((ROOT / "repository.yaml").read_text())
    assert repo["name"] == "EchoMuse"
    assert repo["url"] == WILBOWES


def test_store_url_hash_is_the_installed_prefix():
    assert _repo_prefix(WILBOWES) == "acc06409"
    assert _repo_prefix(FORK) == "84c6fece"
    assert _repo_prefix(WILBOWES + ".git") != "acc06409"
    script = CONTROLLER / "tools/migrate_addon_data.sh"
    got = subprocess.run(
        ["sh", str(script), "prefix", WILBOWES],
        check=True, capture_output=True, text=True)
    assert got.stdout.strip() == "acc06409"


def test_keep_slug_build_is_the_local_replacement():
    out = Path("/tmp/echomuse-keep-slug")
    subprocess.run(
        ["sh", str(CONTROLLER / "tools/make_dev_addon.sh"), "--keep-slug", str(out)],
        check=True, cwd=CONTROLLER,
    )
    text = (out / "config.yaml").read_text()
    config = yaml.safe_load(text)
    assert config["slug"] == "controller"
    assert "image" not in config
    assert config["options"]["esphome_port_base"] == 16001
    assert config["environment"]["EM_REQUIRE_EXISTING_DATA"] == "1"
    assert (out / "Dockerfile").is_file()
    assert (out / "em_wakeverify.py").is_file()
    assert (out / "em_micqueue.py").is_file()
    assert (out / "tools/migrate_addon_data.sh").is_file()


def test_default_local_addon_stays_beside_the_released_one():
    out = Path("/tmp/echomuse-dev-slug")
    subprocess.run(
        [sys.executable and "sh", str(CONTROLLER / "tools/make_dev_addon.sh"), str(out)],
        check=True, cwd=CONTROLLER,
    )
    text = (out / "config.yaml").read_text()
    config = yaml.safe_load(text)
    assert config["slug"] == "controller-dev"
    assert config["options"]["esphome_port_base"] == 16201
    assert "image" not in config
    assert "EM_REQUIRE_EXISTING_DATA" not in text
