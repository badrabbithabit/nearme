#!/usr/bin/env python3
"""Manual browser smoke test for BOTH hosting modes. Needs Playwright.

    pip install playwright && playwright install chromium
    python tests/smoke_browser.py

Starts `server.py` (server mode) and a plain static file server over a
`build_site.py` bundle (Pages mode), loads each in headless Chromium, and fails
if the map renders nothing or the console reports an error. Not part of
tests/test_nearme.py: it needs a browser and (for the static bundle) network.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PY = sys.executable


def wait(url: str, seconds: float = 10.0) -> bool:
    end = time.time() + seconds
    while time.time() < end:
        try:
            urllib.request.urlopen(url, timeout=1).read()
            return True
        except Exception:
            time.sleep(0.3)
    return False


def check(page, label: str, expect_mode: str) -> bool:
    page.goto(f"http://127.0.0.1:{PORTS[label]}/", wait_until="load")
    page.wait_for_function("() => document.querySelectorAll('#list li').length > 0", timeout=20000)
    page.wait_for_timeout(1200)
    problems = []
    mode = page.evaluate("() => state.mode")
    rows = page.evaluate("() => document.querySelectorAll('#list li').length")
    # preferCanvas:true, so count Leaflet layers rather than SVG paths
    markers = page.evaluate("() => state.layers.markers.getLayers().length")
    traces = page.evaluate("() => state.layers.traces.getLayers().length")
    feed = page.evaluate("() => document.getElementById('feed').textContent")
    count = page.evaluate("() => document.getElementById('count').textContent")
    if mode != expect_mode:
        problems.append(f"mode is {mode!r}, expected {expect_mode!r}")
    if rows < 5:
        problems.append(f"only {rows} list rows")
    if markers < 5:
        problems.append(f"only {markers} markers")
    if traces < 1:
        problems.append("no pavement traces drawn")
    if "error" in feed.lower() or "failed" in feed.lower():
        problems.append(f"feed says: {feed}")
    for m in page.console_errors:
        problems.append(f"console: {m}")
    for m in page.request_errors:
        problems.append(f"request: {m}")
    # static mode probes /api/meta on purpose to decide its mode - the 404 is the signal
    if expect_mode == "static":
        problems = [p for p in problems if "/api/meta" not in p and "404" not in p]
    print(f"  {label}: mode={mode} rows={rows} markers={markers} traces={traces} | {count} | {feed}")
    for p in problems:
        print(f"    !! {p}")
    return not problems


PORTS = {"server": 8391, "static": 8392}


def main() -> int:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("playwright not installed - skipping")
        return 0

    site = os.path.join(os.environ.get("TEMP", "/tmp"), "nearme_smoke_site")
    print("building the static bundle...")
    subprocess.run([PY, os.path.join(ROOT, "build_site.py"), "--out", site], cwd=ROOT, check=True)

    procs = [
        subprocess.Popen([PY, os.path.join(ROOT, "server.py"), "--port", str(PORTS["server"])], cwd=ROOT),
        subprocess.Popen([PY, "-m", "http.server", str(PORTS["static"])], cwd=site),
    ]
    ok = True
    try:
        for port in PORTS.values():
            if not wait(f"http://127.0.0.1:{port}/"):
                print(f"  server on {port} never came up")
                return 1
        with sync_playwright() as pw:
            browser = pw.chromium.launch()
            for label, mode in (("server", "server"), ("static", "static")):
                page = browser.new_page(viewport={"width": 1280, "height": 800})
                page.console_errors = []
                page.request_errors = []
                page.on("console", lambda m: page.console_errors.append(m.text) if m.type == "error" else None)
                page.on("requestfailed", lambda r: page.request_errors.append(f"{r.url} {r.failure}"))
                ok &= check(page, label, mode)
                page.close()
            browser.close()
    finally:
        for p in procs:
            p.terminate()
    print("SMOKE OK" if ok else "SMOKE FAILED")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
