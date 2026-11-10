"""pyproject.toml repeats requirements.txt and requirements-dev.txt (the Docker build and CONTRIBUTING.md still use
those). These tests keep the two lists the same, so a dependency bump in one can't leave the other behind."""
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _requirements(name: str) -> set[str]:
    lines = (ROOT / name).read_text(encoding="utf-8").splitlines()
    return {line.split("#", 1)[0].strip() for line in lines} - {""}


def _pyproject() -> dict:
    return tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))


def test_the_package_needs_what_requirements_txt_lists():
    assert set(_pyproject()["project"]["dependencies"]) == _requirements("requirements.txt")


def test_the_dev_group_needs_what_requirements_dev_txt_lists():
    assert set(_pyproject()["dependency-groups"]["dev"]) == _requirements("requirements-dev.txt")


def test_the_package_version_is_read_from_app_version_py():
    project = _pyproject()
    assert "version" in project["project"]["dynamic"]
    assert project["tool"]["hatch"]["version"]["path"] == "app/version.py"
