"""The custom add-on keeps the upstream slug, which is where /data lives.

Supervisor's Addon.path_data is addons/data/<slug>
(home-assistant/supervisor addon.py). Uninstall deletes that directory.
Slug `controller` is the upstream add-on, so a replacement with that slug
uses the same database and the same device-link CA.
"""

import subprocess
import sys
from pathlib import Path

import yaml

CONTROLLER = Path(__file__).resolve().parents[1]
ROOT = CONTROLLER.parent


def test_committed_addon_slug_is_the_upstream_slug():
    config = yaml.safe_load((CONTROLLER / "config.yaml").read_text())
    assert config["slug"] == "controller"
    assert 'DB_PATH: "/data/echomuse.db"' in (CONTROLLER / "config.yaml").read_text()
    repo = yaml.safe_load((ROOT / "repository.yaml").read_text())
    assert repo["name"] == "EchoMuse"


def test_keep_slug_build_replaces_the_released_addon():
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
    assert (out / "Dockerfile").is_file()
    assert (out / "em_wakeverify.py").is_file()
    assert (out / "em_micqueue.py").is_file()


def test_default_local_addon_stays_beside_the_released_one():
    out = Path("/tmp/echomuse-dev-slug")
    subprocess.run(
        [sys.executable and "sh", str(CONTROLLER / "tools/make_dev_addon.sh"), str(out)],
        check=True, cwd=CONTROLLER,
    )
    config = yaml.safe_load((out / "config.yaml").read_text())
    assert config["slug"] == "controller-dev"
    assert config["options"]["esphome_port_base"] == 16201
    assert "image" not in config
