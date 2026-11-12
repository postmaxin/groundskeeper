"""Pre-release images (.github/workflows/prerelease.yml): the panel says which channel it is on, and the workflow can
never publish over a release."""
import importlib
import json
import os
import re
from pathlib import Path

import pytest

from app import version

WORKFLOW = Path(__file__).resolve().parent.parent / ".github" / "workflows" / "prerelease.yml"


@pytest.fixture
def reload_version(monkeypatch):
    def load(channel=None, build=None):
        for key, value in (("PANEL_CHANNEL", channel), ("PANEL_BUILD", build)):
            if value is None:
                monkeypatch.delenv(key, raising=False)
            else:
                monkeypatch.setenv(key, value)
        return importlib.reload(version).version_info()
    yield load
    monkeypatch.delenv("PANEL_CHANNEL", raising=False)
    monkeypatch.delenv("PANEL_BUILD", raising=False)
    importlib.reload(version)


@pytest.mark.parametrize("given, shown", [("edge", "edge"), ("nightly", "nightly"), (" Nightly ", "nightly"),
                                          ("", None), (None, None), ("beta", None), ("latest", None)])
def test_the_channel_is_edge_nightly_or_nothing(reload_version, given, shown):
    assert reload_version(channel=given)["channel"] == shown


def test_a_release_build_has_no_channel(reload_version):
    info = reload_version(build="1a756d0790976fbf3101e1e7ef220b46d5ca1025")
    assert info["channel"] is None and info["build"] == "1a756d0"


def test_prerelease_images_go_to_their_own_package_and_never_to_latest_or_a_version():
    text = WORKFLOW.read_text(encoding="utf-8")
    assert "IMAGE: ghcr.io/${{ github.repository_owner }}/groundskeeper-dev" in text
    assert "delete-package-versions" not in text      # nothing prunes automatically: it could split a multi-arch build
    tags = re.findall(r"type=raw,value=([^,\s]+)", text)
    assert tags and all(re.match(r"(edge|nightly|nightly-\$\{\{|sha-\$\{\{)", t) for t in tags), tags
    assert "latest" not in tags and not any(re.match(r"\d", t) for t in tags)
    assert "PANEL_CHANNEL=${{ steps.what.outputs.channel }}" in text


def test_every_version_label_in_the_ui_shows_the_channel():
    """The nav, Help and Settings all use app.js versionLabel(), run here in node with synthetic values."""
    import shutil
    import subprocess
    web = WORKFLOW.parent.parent.parent / "web"
    app = (web / "app.js").read_text(encoding="utf-8")
    fn = re.search(r"^const versionLabel = .*$", app, flags=re.M).group(0)
    help_js = (web / "help.js").read_text(encoding="utf-8")
    settings_js = (web / "settings.js").read_text(encoding="utf-8")
    assert "a.textContent = `v${versionLabel(idx)}`" in help_js        # the nav
    assert "`Groundskeeper ${versionLabel(idx)}`" in help_js            # Help's front page
    assert "versionLabel(a)" in settings_js                             # Settings -> This panel
    node = shutil.which("node")
    if not node:     # optional on a contributor's machine, as for check.py's syntax check; never quietly in CI
        assert not os.environ.get("CI"), "node is missing in CI: the front-end tests must run there"
        pytest.skip("node is not installed")
    script = fn + """
console.log(JSON.stringify([
  versionLabel({version: "0.19.7", channel: "nightly", build: "1a756d0"}),
  versionLabel({version: "0.19.7", channel: "edge", build: "1a756d0"}),
  versionLabel({version: "0.19.7", channel: null, build: "1a756d0"}),
  versionLabel({version: "0.19.7"}),
]));"""
    out = subprocess.run([node, "-e", script], capture_output=True, encoding="utf-8", timeout=30, check=True).stdout
    assert json.loads(out) == ["0.19.7 · nightly · 1a756d0", "0.19.7 · edge · 1a756d0",
                               "0.19.7 · 1a756d0", "0.19.7"]
