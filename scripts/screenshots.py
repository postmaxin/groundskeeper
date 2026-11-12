"""README screenshots from the synthetic demo (DEMO=1): no real yard, robot, serial or name appears in them.

    pip install playwright pillow       # drives the installed Microsoft Edge (channel="msedge"); no browser download
    python scripts/screenshots.py [--out docs/screenshots] [--keep]

It starts the panel as a subprocess on 127.0.0.1 (a free port) with DEMO=1, PANEL_AUTH=off, TEMPEST_PORT=0 and a
temporary DATA_DIR, waits until the demo robot is mid-mow and the yard's imagery is in, captures each page with
headless Edge, optimises the PNGs with Pillow and stops the panel. Only the demo yard's public park is fetched
(aerial imagery and elevation). Re-runnable: the demo history is seeded with fixed random seeds, so only the clock
differs between runs. --keep leaves the temporary DATA_DIR for a look afterwards.

Pages: live (dark, the default) and live-light, live-3d (3D ground with Follow; needs WebGL, which headless Edge
gets from SwiftShader), runs, run (a past run with a gap), studio, schedule, robot-health, settings (Safety), imagery
(Settings → Map imagery) and phone-live (390x844).
"""
import argparse
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

from PIL import Image
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parent.parent
DESKTOP = {"width": 1440, "height": 900}
PHONE = {"width": 390, "height": 844}
MAX_BYTES = 600_000
EDGE_ARGS = ["--use-angle=swiftshader", "--enable-unsafe-swiftshader", "--ignore-gpu-blocklist"]
# The map is ready: style and tiles loaded, the yard photo and the zones drawn.
MAP_READY = ("() => typeof state !== 'undefined' && state.map && state.map.loaded() && state.map.areTilesLoaded() "
             "&& state.map.getLayer('areas-fill')")
LAUNCH = ("import logging, sys, uvicorn\n"
          "logging.basicConfig(level=logging.WARNING)\n"
          "logging.getLogger('httpx').setLevel(logging.WARNING)\n"      # imagery URLs carry coordinates
          "uvicorn.run('app.main:app', host='127.0.0.1', port=int(sys.argv[1]), log_level='warning', access_log=False)\n")


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def get(url: str, method: str = "GET"):
    req = urllib.request.Request(url, method=method)
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read() or b"null")


def wait_for(what: str, check, timeout: float = 120, every: float = 1.0):
    end = time.time() + timeout
    last = None
    while time.time() < end:
        try:
            got = check()
            if got:
                return got
        except Exception as exc:          # not up yet
            last = exc
        time.sleep(every)
    raise SystemExit(f"timed out waiting for {what}" + (f" ({last})" if last else ""))


def start_panel(data_dir: str, port: int) -> subprocess.Popen:
    env = {k: v for k, v in os.environ.items()
           if k not in ("YARBO_SN", "HA_URL", "HA_TOKEN", "FRIGATE_URL", "PANEL_URL", "DEMO_SEED")}
    env.update(DEMO="1", PANEL_AUTH="off", DATA_DIR=data_dir, TEMPEST_PORT="0", PORT=str(port), DEMO_SPEED="1")
    return subprocess.Popen([sys.executable, "-c", LAUNCH, str(port)], cwd=ROOT, env=env)


def optimise(path: Path) -> int:
    """Smaller PNGs: optimised as they are; a page still over MAX_BYTES (aerial photo) is saved with 256 colours."""
    img = Image.open(path).convert("RGB")
    img.save(path, optimize=True)
    if path.stat().st_size > MAX_BYTES:
        img.quantize(colors=256, method=Image.Quantize.MEDIANCUT, dither=Image.Dither.FLOYDSTEINBERG).save(
            path, optimize=True)
    return path.stat().st_size


def settle(page, ms: int = 1500) -> None:
    page.wait_for_load_state("networkidle")
    page.wait_for_timeout(ms)


def capture(base: str, out: Path) -> list[tuple[str, int]]:
    shots: list[tuple[str, int]] = []
    status = get(f"{base}/api/status")
    runs = get(f"{base}/api/runs")
    runs = runs.get("runs", runs) if isinstance(runs, dict) else runs
    gap_run = next((r["id"] for r in runs if r.get("ended") and r.get("plan") == "Mow All"), None)
    if gap_run is not None:
        try:
            get(f"{base}/api/runs/{gap_run}/planned", "POST")       # planned vs driven, for the run page
        except Exception as exc:
            print(f"planned vs driven not available: {exc}")

    def save(page, name: str) -> None:
        path = out / f"{name}.png"
        page.screenshot(path=str(path))
        shots.append((path.name, optimise(path)))
        print(f"{path.name}: {shots[-1][1] // 1024} KB", flush=True)

    with sync_playwright() as p:
        browser = p.chromium.launch(channel="msedge", headless=True, args=EDGE_ARGS)

        def context(theme: str = "dark", viewport=DESKTOP, three_d: bool = False, **kw):
            ctx = browser.new_context(viewport=viewport, color_scheme=theme, **kw)
            prefs = {"theme": theme, "yarbo.map.ground3d": "true" if three_d else "false"}
            ctx.add_init_script(f"try {{ for (const [k, v] of Object.entries({json.dumps(prefs)})) "
                                "localStorage.setItem(k, v); } catch (e) {}")
            return ctx

        # -- dark, desktop
        ctx = context()
        page = ctx.new_page()
        page.goto(f"{base}/#live")
        page.wait_for_function(MAP_READY, timeout=90000)
        page.wait_for_function("() => document.querySelector('#p-live').innerText.includes('Plan done')", timeout=30000)
        settle(page, 3000)
        save(page, "live")

        page.goto(f"{base}/#runs")
        page.wait_for_function("() => document.querySelector('#p-runs').innerText.includes('EVERY RUN') || "
                               "document.querySelector('#p-runs table')", timeout=30000)
        settle(page)
        save(page, "runs")

        if gap_run is not None:
            page.goto(f"{base}/#runs/{gap_run}")
            page.wait_for_function("() => document.querySelector('#p-runs').innerText.includes('COVERAGE BY AREA') "
                                   "|| document.querySelector('#p-runs').innerText.includes('Coverage by area')",
                                   timeout=60000)
            page.wait_for_function(MAP_READY, timeout=60000)
            settle(page, 2500)
            save(page, "run")

        page.goto(f"{base}/#studio")
        page.wait_for_function(MAP_READY + " && document.querySelector('.studiobar button')", timeout=60000)
        settle(page, 2000)
        row = page.locator("#p-studio").get_by_text("Back lawn", exact=True).first
        if row.count():
            row.click()
            settle(page, 1500)
        save(page, "studio")

        for name, hash_ in (("schedule", "schedule"), ("robot-health", "health"), ("settings", "settings/safety")):
            page.goto(f"{base}/#{hash_}")
            settle(page, 2500)
            save(page, name)

        # Settings -> Map imagery: one card per source that covers the yard, each with its own sample of the yard.
        page.goto(f"{base}/#settings/imagery")
        page.wait_for_function("() => { const im = [...document.querySelectorAll('#imgpicker img')]; "
                               "return im.length > 1 && im.every(i => i.complete && i.naturalWidth > 0); }",
                               timeout=180000)
        settle(page, 2000)
        save(page, "imagery")
        ctx.close()

        # -- 3D with Follow (WebGL through SwiftShader)
        ctx = context(three_d=True)
        page = ctx.new_page()
        page.goto(f"{base}/#live")
        try:
            page.wait_for_function(MAP_READY + " && state.map.getSource('ground3d')", timeout=90000)
            settle(page, 2000)
            page.click("#follow3d")
            page.wait_for_timeout(4000)
            box = page.locator("#map").bounding_box()
            page.mouse.move(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
            for _ in range(3):
                page.mouse.wheel(0, 240)          # back out a little from the follow camera's close-up
                page.wait_for_timeout(400)
            page.wait_for_timeout(8000)             # the 3D ground keeps loading tiles while it follows
            save(page, "live-3d")
        except Exception as exc:
            print(f"live-3d not captured: {exc}")
        ctx.close()

        # -- light theme
        ctx = context("light")
        page = ctx.new_page()
        page.goto(f"{base}/#live")
        page.wait_for_function(MAP_READY, timeout=90000)
        settle(page, 3000)
        save(page, "live-light")
        ctx.close()

        # -- phone
        ctx = context(viewport=PHONE, device_scale_factor=2, is_mobile=True, has_touch=True)
        page = ctx.new_page()
        page.goto(f"{base}/#live")
        page.wait_for_function(MAP_READY, timeout=90000)
        settle(page, 3000)
        save(page, "phone-live")
        ctx.close()
        browser.close()
    return shots


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--out", default=str(ROOT / "docs" / "screenshots"))
    ap.add_argument("--keep", action="store_true", help="keep the temporary DATA_DIR")
    args = ap.parse_args()
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    data_dir = tempfile.mkdtemp(prefix="gk-demo-")
    port = free_port()
    base = f"http://127.0.0.1:{port}"
    proc = start_panel(data_dir, port)
    try:
        wait_for("the panel", lambda: get(f"{base}/health").get("ok"), timeout=90)
        wait_for("the demo robot mid-mow", lambda: (lambda s: s.get("activity") in ("mowing", "transit")
                                                    and (s.get("run") or {}).get("plan")
                                                    and s["map"]["features"] > 0)(get(f"{base}/api/status")))
        wait_for("the yard imagery", lambda: (get(f"{base}/api/imagery").get("yard") or {}).get("url"), timeout=180)
        shots = capture(base, out)
        print(f"{len(shots)} screenshots in {out}")
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            proc.kill()
        if args.keep:
            print(f"DATA_DIR kept: {data_dir}")
        else:
            shutil.rmtree(data_dir, ignore_errors=True)


if __name__ == "__main__":
    main()
