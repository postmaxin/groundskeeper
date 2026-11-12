"""The ADRs in docs/dev/decisions/ (ADR 0001): each has the template's shape, numbers run in order, the README index
lists every one with its status, and what an ADR says enforces it (files, tests) still exists."""
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
DIR = ROOT / "docs" / "dev" / "decisions"
ADRS = sorted(DIR.glob("[0-9][0-9][0-9][0-9]-*.md"))
STATUS = re.compile(r"^- \*\*Status:\*\* (Proposed|Accepted|Rejected|Superseded by (\d{4}))$", re.M)
SECTIONS = ("## Context", "## Decision", "## Consequences", "## Enforced by")
PATH = re.compile(r"`((?:app|web|tests|scripts|docs|catalog|deploy|brand)/[\w./-]+|[\w-]+\.(?:md|txt|toml))`")
TEST_NAME = re.compile(r"`(test_\w+)`")


def _text(p: Path) -> str:
    return p.read_text(encoding="utf-8")


def test_there_are_decisions():
    assert ADRS, "docs/dev/decisions/ has no ADRs"


def test_numbers_run_in_order_without_gaps():
    numbers = [int(p.name[:4]) for p in ADRS]
    assert numbers == list(range(1, len(ADRS) + 1)), f"ADR numbers must run 0001, 0002, ... without gaps: {numbers}"


@pytest.mark.parametrize("adr", ADRS, ids=lambda p: p.name)
def test_an_adr_has_the_template_shape(adr):
    text = _text(adr)
    assert text.startswith(f"# {adr.name[:4]}. "), f"{adr.name}: the title must start '# {adr.name[:4]}. '"
    m = STATUS.search(text)
    assert m, f"{adr.name}: needs a '- **Status:** Proposed | Accepted | Rejected | Superseded by NNNN' line"
    if m.group(2):
        assert any(p.name.startswith(m.group(2)) for p in ADRS), f"{adr.name}: superseded by a missing ADR"
    assert re.search(r"^- \*\*Date:\*\* \d{4}-\d{2}-\d{2}", text, re.M), f"{adr.name}: needs a Date line"
    missing = [s for s in SECTIONS if f"\n{s}\n" not in text]
    assert not missing, f"{adr.name}: missing sections {missing}"


def test_the_index_lists_every_adr_with_its_status():
    index = _text(DIR / "README.md")
    for adr in ADRS:
        row = next((line for line in index.splitlines() if f"]({adr.name})" in line), None)
        assert row, f"README.md index has no row for {adr.name}"
        status = STATUS.search(_text(adr)).group(1)
        last = re.sub(r"\[([^]]*)\]\([^)]*\)", r"\1", row.rstrip(" |").rsplit("|", 1)[-1]).strip()
        assert last == status, f"README.md index: {adr.name} says '{last}', the ADR says '{status}'"


def test_the_index_links_only_to_adrs_that_exist():
    linked = re.findall(r"\]\((\d{4}-[\w-]+\.md)\)", _text(DIR / "README.md"))
    gone = [name for name in linked if not (DIR / name).exists()]
    assert not gone, f"README.md index links to ADRs that don't exist: {gone}"


def test_only_numbered_adrs_live_here():
    stray = [p.name for p in DIR.glob("*.md") if p.name != "README.md" and p not in ADRS]
    assert not stray, f"name ADRs NNNN-title.md: {stray}"


@pytest.mark.parametrize("adr", ADRS, ids=lambda p: p.name)
def test_an_accepted_adr_says_what_enforces_it(adr):
    text = _text(adr)
    if STATUS.search(text).group(1) != "Accepted":
        return
    body = text.split("\n## Enforced by\n", 1)[-1].strip()
    assert "`" in body, f"{adr.name}: name the code or test that holds the decision under Enforced by"


@pytest.mark.parametrize("adr", ADRS, ids=lambda p: p.name)
def test_what_an_adr_names_still_exists(adr):
    """An ADR that points at a file or test that is gone no longer says what holds the decision in place."""
    text = _text(adr)
    paths = [m for m in PATH.findall(text)                  # a bare page name is one of docs/dev/'s
             if not (ROOT / m).exists() and not ("/" not in m and (ROOT / "docs" / "dev" / m).exists())]
    assert not paths, f"{adr.name} names files that don't exist: {paths}"
    tests = "\n".join(_text(p) for p in (ROOT / "tests").glob("test_*.py"))
    names = [n for n in TEST_NAME.findall(text) if not re.search(rf"def {n}\(", tests)]
    assert not names, f"{adr.name} names tests that don't exist: {names}"
