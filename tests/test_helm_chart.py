"""The Helm chart (deploy/helm/groundskeeper) runs the image tagged with its appVersion unless image.tag says
otherwise, so appVersion has to move with each release. This keeps it equal to app/version.py."""
import re
from pathlib import Path

from app.version import __version__

CHART = Path(__file__).resolve().parent.parent / "deploy" / "helm" / "groundskeeper" / "Chart.yaml"


def test_the_chart_defaults_to_this_release():
    m = re.search(r'^appVersion:\s*"?([^"\s]+)"?\s*$', CHART.read_text(encoding="utf-8"), re.M)
    assert m, "Chart.yaml has no appVersion"
    assert m.group(1) == __version__
