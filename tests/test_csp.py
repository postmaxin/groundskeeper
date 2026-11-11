"""The connect-src policy the panel sends while Robot video is on (design §13, app/csp.py).

The matcher below follows CSP3's host-source rules closely enough to say whether the Agora statistics and log hosts
are reachable: scheme, a `*.` wildcard standing for one or more leftmost labels, and a port (default for the scheme,
`*` for any). Synthetic hosts only; nothing is contacted."""
import re
from pathlib import Path
from urllib.parse import urlsplit

import pytest
from fastapi.testclient import TestClient

from app import csp, main
from tests.test_video_route import t  # noqa: F401  (the fixture)

DEFAULT_PORT = {"http": 80, "https": 443, "ws": 80, "wss": 443}
SCHEME_UPGRADE = {"http": {"https"}, "ws": {"wss", "https"}}      # a source may be matched by a more secure scheme


def policy_sources(header: str) -> list[str]:
    parts = [p.strip() for p in header.split(";") if p.strip()]
    assert len(parts) == 1 and parts[0].startswith("connect-src "), header     # connect-src and nothing else
    return parts[0].split()[1:]


def allows(sources: list[str], url: str) -> bool:
    u = urlsplit(url)
    port = u.port or DEFAULT_PORT[u.scheme]
    for src in sources:
        if src.startswith("'") or src.endswith(":") and "//" not in src:
            continue                                                     # 'self', blob:, data: never match a remote URL
        m = re.fullmatch(r"(?:([a-z]+)://)?([^/:]+)(?::(\d+|\*))?", src)
        assert m, src
        scheme, host, sport = m.groups()
        if scheme and scheme != u.scheme and u.scheme not in SCHEME_UPGRADE.get(scheme, ()):
            continue
        if host.startswith("*."):
            if not (u.hostname or "").endswith(host[1:]) or u.hostname == host[2:]:
                continue
        elif host != u.hostname:
            continue
        if sport not in (None, "*") and int(sport) != port:
            continue
        if sport is None and port != DEFAULT_PORT.get(scheme or u.scheme):
            continue
        return True
    return False


BLOCKED = [
    "https://statscollector-1.agora.io/events", "https://statscollector-2-us.agora.io/events",
    "https://web-2.statscollector.sd-rtn.com/events", "https://web-3.statscollector.sd-rtn.com/events",
    "https://logservice.agora.io/up", "https://logservice-north-america.agora.io/up",
    "https://data-reporting.agora.io/", "wss://statscollector-1.agora.io:4705/",
    "https://www.agora.io/", "https://x.sd-rtn.com/", "https://evil.example/", "wss://evil.example/",
    "wss://edge.agora.io:4705/", "wss://1-2-3-4.edge.agora.io.evil.example:4705/",
]
ALLOWED = [
    "https://webrtc2-ap-web-1.agora.io/api/v2/transpond/webrtc", "https://webrtc2-ap-web-3.agora.io/x",
    "https://webrtc2-2.ap.sd-rtn.com/api/v2/transpond/webrtc", "wss://203-0-113-9.edge.agora.io:4705/",
    "wss://203-0-113-9.edge.sd-rtn.com:4705/",
]


def test_matcher_knows_its_rules():
    srcs = ["https://*.ap.sd-rtn.com", "wss://*.edge.agora.io:*", "https://exact.example"]
    assert allows(srcs, "https://a.b.ap.sd-rtn.com/") and allows(srcs, "wss://x.edge.agora.io:1/")
    assert not allows(srcs, "https://ap.sd-rtn.com/")                  # a wildcard needs at least one label
    assert not allows(srcs, "https://exact.example:8443/")             # no port in the source: the default only
    assert not allows(srcs, "http://exact.example/") and not allows(srcs, "wss://exact.example/")


def test_policy_admits_gateway_and_edge_but_no_statistics_or_log_host():
    src = policy_sources(csp.video_policy())
    for url in ALLOWED:
        assert allows(src, url), url
    for url in BLOCKED:
        assert not allows(src, url), url
    assert {"'self'", "blob:", "data:"} <= set(src)
    assert not any(s in ("https://*.agora.io", "https://*.sd-rtn.com", "*", "https:", "wss:") for s in src)


def test_policy_blocks_every_stats_and_log_host_in_the_vendored_sdk():
    bundle = Path(__file__).resolve().parent.parent / "web" / "vendor" / "AgoraRTC_N-production.js"
    if not bundle.exists():
        pytest.skip("the vendored SDK is not in this checkout")
    hosts = set(re.findall(r"[a-zA-Z0-9_-]+(?:\.[a-zA-Z0-9_-]+)*\.(?:agora\.io|sd-rtn\.com)", bundle.read_text(errors="ignore")))
    bad = {h for h in hosts if re.search(r"statscollector|logservice|data-reporting", h)}
    assert len(bad) > 10                                               # the scan found them
    src = policy_sources(csp.video_policy())
    for h in sorted(bad):
        assert not allows(src, f"https://{h}/"), h
        assert not allows(src, f"wss://{h}:4705/"), h


def test_header_only_on_the_page_and_only_with_video_on(t):  # noqa: F811
    t.store.set_setting("imagery_source", {"custom": {"url": "https://tiles.imagery-host.invalid/{z}/{x}/{y}.jpg"}})
    off = t.c.get("/")
    assert off.status_code == 200 and "content-security-policy" not in off.headers
    assert 'name="gk-video-csp"' not in off.text
    assert "content-security-policy" not in t.c.get("/index.html").headers

    t.c.put("/api/settings/robot_video_enabled", json=True)
    on = t.c.get("/")
    header = on.headers["content-security-policy"]
    assert header == csp.video_policy() and 'name="gk-video-csp"' in on.text
    assert "https://webrtc2-ap-web-1.agora.io" in header and "wss://*.edge.agora.io:*" in header
    assert "content-security-policy" in t.c.get("/index.html").headers
    assert "imagery-host.invalid" not in header          # tiles are relayed by the panel, so the browser never asks it
    for other in ("/login.html", "/app.js", "/vendor/maplibre-gl.js", "/manifest.json"):
        assert "content-security-policy" not in t.c.get(other).headers, other

    anon = TestClient(main.app, base_url="http://panel.test")          # signed out: redirected, and told nothing
    gone = anon.get("/", follow_redirects=False)
    assert gone.status_code == 303 and "content-security-policy" not in gone.headers

    t.c.put("/api/settings/robot_video_enabled", json=False)
    assert "content-security-policy" not in t.c.get("/").headers
