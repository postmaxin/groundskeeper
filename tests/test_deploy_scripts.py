"""The deploy scripts send the Docker build context as an explicit tar file list. Every local COPY/ADD source in the
Dockerfile must be in that list, or a remote build fails on a file the build can't see."""
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ["scripts/deploy.sh", "scripts/deploy_demo.sh"]


def _dockerfile_sources() -> set[str]:
    text = (ROOT / "Dockerfile").read_text(encoding="utf-8").replace("\\\n", " ")
    out = set()
    for line in text.splitlines():
        parts = line.split()
        if not parts or parts[0] not in ("COPY", "ADD"):
            continue
        args = [p for p in parts[1:] if not p.startswith("--")]
        out.update(a.rstrip("/") for a in args[:-1] if "://" not in a)
    return out


def _tar_list(script: str) -> set[str]:
    text = (ROOT / script).read_text(encoding="utf-8").replace("\\\n", " ")
    line = next(ln for ln in text.splitlines() if ln.lstrip().startswith("tar "))
    return {t.rstrip("/") for t in line.split("-cf -", 1)[1].split("|")[0].split()}


def test_dockerfile_sources_found():
    assert {"THIRD_PARTY_NOTICES.md", "LICENSE", "app"} <= _dockerfile_sources()


@pytest.mark.parametrize("script", SCRIPTS)
def test_tar_list_covers_dockerfile_sources(script):
    listed = _tar_list(script)
    missing = [s for s in sorted(_dockerfile_sources())
               if not any(s == t or s.startswith(t + "/") for t in listed)]
    assert not missing, f"{script} does not send what the Dockerfile copies: {missing}"
