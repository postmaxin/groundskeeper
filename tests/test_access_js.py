"""Settings → Access (web/settings.js): the Metrics switch is there with sign-in off (PANEL_AUTH=off) too, where
the API tokens are not. The page script is run in node against stubs; no browser."""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


@pytest.mark.skipif(not shutil.which("node"), reason="node is not installed")
def test_the_metrics_switch_shows_with_sign_in_off_and_on():
    out = subprocess.run([shutil.which("node"), str(ROOT / "tests" / "access_flow.cjs"), str(ROOT / "web" / "settings.js")],
                         capture_output=True, encoding="utf-8", timeout=60, check=True).stdout
    got = json.loads(out)
    assert "Metrics" in got["off"] and "Off" in got["off"]
    assert not any("token" in n.lower() for n in got["off"])         # tokens mean nothing with sign-in off
    assert "Metrics" in got["on"] and "New token" in got["on"]
    assert got["button"] == "Switch metrics off"
    assert got["saved"] == [["/api/settings/metrics_enabled", False]]
