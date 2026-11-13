"""Guard tests: the rules in AGENTS.md that a test can check, checked. They exist so that a contributor, human or AI,
who has not read the rules still can't break them by accident. When one fails, read the message and the AGENTS.md
rule it names; don't edit the test to make it pass. Changing a guard (a new open path, a new place that publishes to
the robot) is a design decision: say so in the PR and expect a closer review.

Static checks only: they read the source and the module constants, never start the app or touch a network."""
import ast
import subprocess
import re
from pathlib import Path

import pytest

from app import auth, control, mapwrite, mcp_server
from app.main import SETTINGS_DEFAULTS
from app.commands import COMPUTE_ONLY, READ_ONLY_COMMANDS

ROOT = Path(__file__).resolve().parent.parent
APP = sorted((ROOT / "app").rglob("*.py"))
WEB_JS = sorted((ROOT / "web").glob("*.js"))
WEB_HTML = sorted((ROOT / "web").glob("*.html"))


def rel(p: Path) -> str:
    return p.relative_to(ROOT).as_posix()


def read(p: Path) -> str:
    return p.read_text(encoding="utf-8")


def js_code(text: str) -> str:
    """JS with // line comments and /* block */ comments blanked out (good enough for the patterns checked here)."""
    text = re.sub(r"/\*.*?\*/", lambda m: "\n" * m.group().count("\n"), text, flags=re.S)
    return re.sub(r"(^|[^:\"'\\])//.*", r"\1", text)


# -- Safety ---------------------------------------------------------------------------------------------------------

# The only modules that may call .publish(): the cloud transport, the link (the one doorway to it), and the map
# writer (WRITE_COMMANDS only, through the link's write_command, read back after). AGENTS.md → Rules → Safety.
PUBLISHERS = {"app/cloud.py", "app/link.py", "app/mapwrite.py"}
# Only the link reaches the cloud transport's publish.
CLOUD_PUBLISH_USERS = {"app/link.py"}


def _publish_calls(p: Path) -> int:
    tree = ast.parse(read(p))
    return sum(isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr == "publish"
               for n in ast.walk(tree))


def test_only_known_modules_publish_to_the_robot():
    found = {rel(p) for p in APP if _publish_calls(p)}
    extra = found - PUBLISHERS
    assert not extra, (f"{sorted(extra)} call .publish(). Robot commands go through app.control.Controller.send() "
                       "(run commands) or app.mapwrite.MapWriter (map writes); reads through app.link.RobotLink.read. "
                       "See AGENTS.md → Rules → Safety.")


def test_only_the_link_reaches_the_cloud_publish():
    users = {rel(p) for p in APP if re.search(r"\b(cloud|ble)\.publish\b", read(p))}   # ble: ADR 0012
    extra = users - CLOUD_PUBLISH_USERS
    assert not extra, (f"{sorted(extra)} reach cloud.publish directly; commands go Controller/MapWriter → "
                       "app.link.RobotLink (AGENTS.md → Safety)")


def _uses(p: Path, attr: str) -> list[tuple[str, int]]:
    """(qualified place, line) of every `<anything>.<attr>` in a module: a call, a bound method handed on, however the
    object is named or aliased. The place is Class.method; a lambda is its own place ("...<lambda>"), so a lambda
    built inside an allowed method and handed out is not allowed."""
    tree, out, skip = ast.parse(read(p)), [], set()

    def walk(node, place):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            outer = place.rsplit(".", 1)[0] if "." in place else "<module>"
            heads = [*getattr(node, "decorator_list", []), *node.args.defaults, *[d for d in node.args.kw_defaults if d]]
            for head in heads:                    # evaluated where the def stands, not inside it
                for sub_node in ast.walk(head):
                    skip.add(id(sub_node))
                    if isinstance(sub_node, ast.Attribute) and sub_node.attr == attr:
                        out.append((outer, sub_node.lineno))
        for child in ast.iter_child_nodes(node):
            if id(child) in skip:
                continue
            here = place
            if isinstance(child, ast.ClassDef):
                here = child.name if place == "<module>" else f"{place}.{child.name}"
            elif isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                here = child.name if place == "<module>" else f"{place}.{child.name}"
            elif isinstance(child, ast.Lambda):
                here = f"{place}.<lambda>"
            if isinstance(child, ast.Attribute) and child.attr == attr:
                out.append((place, child.lineno))
            walk(child, here)
    walk(tree, "<module>")
    return out


# Where a run command may be sent through the link (Controller.send), and where the link's write door may be handed
# out (Ops wiring the map writer). Anywhere else is a bypass.
COMMAND_USES = {"app/link.py": None, "app/control.py": {"Controller.send"}}
WRITE_DOOR = {"app/link.py": None, "app/ops.py": {"Ops.__init__"}}


def test_only_the_controller_sends_commands_through_the_link():
    for p in APP:
        allowed = COMMAND_USES.get(rel(p), set())
        bad = [(f, n) for f, n in _uses(p, "command") if allowed is not None and f not in allowed]
        assert not bad, (f"{rel(p)} uses .command at {bad}: only Controller.send sends run "
                         "commands, through app.link.RobotLink (AGENTS.md → Safety)")


def test_only_the_map_writer_gets_the_write_door():
    for p in APP:
        allowed = WRITE_DOOR.get(rel(p), set())
        bad = [(f, n) for f, n in _uses(p, "write_command") if allowed is not None and f not in allowed]
        assert not bad, f"{rel(p)} uses .write_command at {bad}: only MapWriter writes, wired in Ops.__init__"


def test_the_guard_sees_handoffs_and_lambdas(tmp_path):
    bad = tmp_path / "bad.py"
    bad.write_text("class Controller:\n"
                   "    def send(self):\n"
                   "        f = lambda: self.link.command('pause', {})\n"
                   "        return f\n"
                   "class Ops:\n"
                   "    def tick(self):\n"
                   "        MapWriter(self.store, self.link.command)\n", encoding="utf-8")
    assert _uses(bad, "command") == [("Controller.send.<lambda>", 3), ("Ops.tick", 7)]


def test_the_guard_places_defaults_and_decorators_outside_the_function(tmp_path):
    bad = tmp_path / "bad.py"
    bad.write_text("class Controller:\n"
                   "    def send(self, cb=link.command):\n"
                   "        return cb\n", encoding="utf-8")
    assert _uses(bad, "command") == [("Controller", 2)]


@pytest.mark.parametrize("cmd", ["cmd_vel", "set_sound_param", "light_ctrl", "get_controller", "shutdown"])
def test_the_robot_is_sent_nothing_but_run_commands_and_captured_writes(cmd):
    from app.commands import ReadOnlyViolation, check_command
    with pytest.raises(ReadOnlyViolation):
        check_command(cmd)


@pytest.mark.parametrize("cmd", sorted(control.CATALOGUE))
def test_the_run_commands_may_be_sent(cmd):
    from app.commands import check_command
    check_command(cmd)


def test_manual_driving_stays_blocked():
    assert "cmd_vel" in control.HARD_BLOCKED
    for name, cmds in [("control.CATALOGUE", control.CATALOGUE), ("READ_ONLY_COMMANDS", READ_ONLY_COMMANDS),
                       ("mapwrite.WRITE_COMMANDS", mapwrite.WRITE_COMMANDS)]:
        assert "cmd_vel" not in cmds, f"cmd_vel must never be in {name}"


def test_read_requests_are_reads_or_listed_compute_only():
    for cmd in READ_ONLY_COMMANDS:
        assert cmd.startswith(("get_", "read_")) or cmd in COMPUTE_ONLY, (
            f"{cmd} is not a read. A read request changes nothing; a request that only computes must be named in "
            "COMPUTE_ONLY with a comment saying why nothing on the robot changes.")


def test_a_new_install_has_every_switch_off():
    for key in ("control_enabled", "map_write_enabled", "mcp_control_enabled", "ble_enabled"):
        assert SETTINGS_DEFAULTS[key] is False, f"{key} must default to off (AGENTS.md: never loosen a default)"
    assert control.GATE_DEFAULTS["guard_resume"] is False, "guard auto-resume spins blades; it stays opt-in"
    assert control.GATE_DEFAULTS["yard_clear_required"] is True
    assert control.GATE_DEFAULTS["presence_required"] is True


def test_mcp_is_off_until_a_token_is_made():
    class NoSettings:
        def setting(self, key, default=None):
            return default
    assert mcp_server.authorised(NoSettings(), "Bearer anything") is None


DOCS = ROOT / "docs" / "dev"


@pytest.mark.parametrize("cmd", sorted({*READ_ONLY_COMMANDS, *control.CATALOGUE, *mapwrite.WRITE_COMMANDS}))
def test_every_robot_command_is_documented(cmd):
    docs = read(DOCS / "protocol.md") + read(DOCS / "map-writes.md")
    assert re.search(rf"\b{re.escape(cmd)}\b", docs), (
        f"{cmd} is sent to the robot but not described in docs/dev/protocol.md or docs/dev/map-writes.md. Document "
        "its format and the capture it came from first (AGENTS.md → Safety: no command without a capture).")


# -- Access ---------------------------------------------------------------------------------------------------------

# Every path that skips sign-in. Adding one is a security decision: change this set in the same PR and say why.
OPEN_PATHS = {"/health", "/login.html", "/login.js", "/style.css", "/favicon.ico", "/manifest.json",
              "/api/auth/status", "/api/auth/login", "/api/auth/setup",
              # OIDC sign-in: out to the provider and back. Both are no-ops (404) unless OIDC_ISSUER is set, and the
              # callback only signs someone in after the signed flow cookie, the ID token and the allowlist check out.
              "/api/auth/oidc/start", "/api/auth/oidc/callback"}


def test_open_paths_are_pinned():
    assert auth.OPEN_PATHS == OPEN_PATHS, "the set of paths open without sign-in changed (AGENTS.md → Access)"
    assert auth.OPEN_PREFIXES == ("/brand/",)
    assert set(auth.LOCKED_PREFIXES) >= {"/api/", "/tiles/", "/imagery/"}


@pytest.mark.parametrize("path", ["/api/status", "/api/runs/1/points", "/api/new-route", "/tiles/1/2/3.png",
                                  "/imagery/yard.jpg", "/"])
def test_data_paths_need_sign_in(path):
    assert auth.needs_auth(path)


# -- Front end ------------------------------------------------------------------------------------------------------

def test_no_html_injection_in_the_ui():
    bad = re.compile(r"\.(?:innerHTML|outerHTML)\s*[+]?=|insertAdjacentHTML\s*\(|document\.write\s*\(")
    hits = [f"{rel(p)}:{n}" for p in WEB_JS for n, line in enumerate(js_code(read(p)).splitlines(), 1)
            if bad.search(line)]
    assert not hits, f"build DOM with h()/createElement + textContent, never HTML strings: {hits}"


def test_nothing_loads_from_a_cdn_at_runtime():
    cdn = re.compile(r"""(?:src|href)\s*=\s*["']https?://|import\s*\(?\s*["']https?://""")
    hits = [rel(p) for p in [*WEB_HTML, *WEB_JS] if cdn.search(js_code(read(p)) if p.suffix == ".js" else read(p))]
    assert not hits, f"libraries are vendored by the Dockerfile with checksums, not loaded from the web: {hits}"


# Hard-coded colours left in the UI when this test was written. The rule is colours through the tokens in
# web/style.css (tok(), MAP, SERIES); this only stops the number from growing. Lower a count when you remove some.
HEX_BASELINE = {"web/app.js": 17, "web/ops.js": 1, "web/index.html": 2}
HEX = re.compile(r"#[0-9a-fA-F]{6}\b|#[0-9a-fA-F]{3}\b(?![\w-])")
TOK_FALLBACK = re.compile(r"""tok\(\s*["']--[\w-]+["']\s*,\s*["']#[0-9a-fA-F]{3,8}["']\s*\)""")


def _hex_count(p: Path) -> int:
    text = js_code(read(p)) if p.suffix == ".js" else read(p)
    return len(HEX.findall(TOK_FALLBACK.sub("", text)))


@pytest.mark.parametrize("p", [*WEB_JS, *WEB_HTML], ids=rel)
def test_no_new_hard_coded_colours(p):
    n, allowed = _hex_count(p), HEX_BASELINE.get(rel(p), 0)
    assert n <= allowed, (f"{rel(p)} has {n} hard-coded colours (allowed {allowed}). Add a token in web/style.css "
                          "and read it with tok() (AGENTS.md → Code and UI)")
    assert n == allowed, f"{rel(p)} is down to {n} hard-coded colours: lower HEX_BASELINE to match, so it stays down"


# -- Docs -----------------------------------------------------------------------------------------------------------

AGENT_DOCS = [ROOT / "AGENTS.md", *sorted(ROOT.glob("*/AGENTS.md"))]
PATH_REF = re.compile(r"`((?:app|web|tests|docs|catalog|scripts|tools|brand|deploy|docker|\.github)/[\w./-]*)`")


def _in_repo(path: str) -> bool:
    """Tracked in git, a folder holding tracked files, or a path the repo deliberately ignores (web/vendor/ is filled
    in at build time). Not the local disk: a file that exists only on one machine must not pass."""
    path = path.rstrip("/")
    tracked = subprocess.run(["git", "ls-files", "--", path], cwd=ROOT, capture_output=True, text=True).stdout
    ignored = subprocess.run(["git", "check-ignore", "-q", "--no-index", path + "/x"], cwd=ROOT).returncode == 0
    return bool(tracked.strip()) or ignored


@pytest.mark.parametrize("doc", AGENT_DOCS, ids=rel)
def test_paths_in_agent_docs_exist(doc):
    missing = [m for m in PATH_REF.findall(read(doc)) if "*" not in m and "<" not in m and not _in_repo(m)]
    assert not missing, f"{rel(doc)} names paths that don't exist: {missing}. Fix the doc in the same PR."


def test_brand_tokens_match_the_css():
    """brand/tokens.json documents the palette; web/style.css is what the panel uses. They must not drift apart."""
    import json
    css = read(ROOT / "web" / "style.css").split("@media")[0]
    dark = {k: v.lower() for k, v in re.findall(r"--([\w-]+):\s*(#[0-9A-Fa-f]{3,8})", css)}
    tokens = json.loads(read(ROOT / "brand" / "tokens.json"))["color"]["tokens"]
    differ = [(t["name"], v, dark[t["name"]]) for t in tokens
              for v in [(t["value"]["dark"] if isinstance(t["value"], dict) else t["value"]).lower()]
              if t["name"] in dark and v != dark[t["name"]]]
    assert not differ, f"brand/tokens.json and web/style.css disagree (name, tokens, css): {differ}"
