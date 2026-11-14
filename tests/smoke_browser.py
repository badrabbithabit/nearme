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


def interactions(page, label: str) -> list[str]:
    """Things that broke once and would look fine in a screenshot.

    a) severity chips: filtered() once ignored state.levels, so clicking a chip
       changed nothing but the chip's styling;
    b) the "N shown" counter: it used to report the fetched set, not the filtered
       set, so it disagreed with the list below it;
    c) row-click popups: flyTo() fires moveend -> refetch -> render() ->
       clearLayers(), which used to remove the popup the click had just opened.
    """
    problems = []
    all_count = page.evaluate("() => document.getElementById('count').textContent")

    # (a) keep only the first severity ("full closure") and re-render
    page.evaluate(
        "() => document.querySelectorAll('#chips .chip').forEach((c, i) => { if (i) c.click(); })"
    )
    page.wait_for_timeout(1800)
    levels = page.evaluate("() => [...state.levels]")
    shown = page.evaluate("() => filtered().length")
    markers = page.evaluate("() => state.layers.markers.getLayers().length")
    bad = page.evaluate("() => filtered().filter(f => !state.levels.has(f.properties.level)).length")
    if levels != ["full"]:
        problems.append(f"chips left levels {levels}, expected ['full']")
    if bad:
        problems.append(f"{bad} rendered features are outside the selected severities")
    if shown == 0 or markers == 0:
        problems.append("full-closure filter rendered nothing")

    # (b) counter must agree with what is actually drawn
    count_txt = page.evaluate("() => document.getElementById('count').textContent")
    if not count_txt.strip().startswith(str(shown)):
        problems.append(f"counter says {count_txt!r} but {shown} features pass the filters ({all_count!r} before)")
    if count_txt == all_count:
        problems.append("counter did not change when the filters did")

    # (c) open a popup from the list; the flyTo it triggers fires moveend ->
    # refetch -> clearLayers() about 0.5-1s later, which used to eat the popup.
    # Assert it is still open AFTER that settles, not merely that it appeared.
    page.evaluate(
        "() => document.querySelectorAll('#chips .chip').forEach((c, i) => { if (i) c.click(); })"
    )
    page.wait_for_timeout(1500)
    uid = page.evaluate(
        "() => { const li = document.querySelector('#list li'); window.__clicked = li.dataset.uid; li.click(); return window.__clicked; }"
    )
    page.wait_for_timeout(4500)  # past the 450ms moveend debounce plus the refetch
    got = page.evaluate(
        "() => { const el = document.querySelector('.leaflet-popup-content');"
        " const f = state.features.find((x) => x.properties.uid === window.__clicked);"
        " return { text: el ? el.innerText : '', kind: f ? f.properties.kind : '', openUid: state.openUid }; }"
    )
    if not got["text"]:
        problems.append("list-row popup vanished during the post-flyTo refetch")
    elif len(got["text"]) < 40:
        problems.append(f"popup looks empty: {got['text'][:60]!r}")
    elif got["kind"] and got["kind"].lower() not in got["text"].lower():
        problems.append(f"popup is not the clicked record ({got['kind']!r}, uid {uid})")
    elif got["openUid"] != uid:
        problems.append(f"openUid is {got['openUid']!r}, expected {uid!r}")
    return problems


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
    problems += interactions(page, label)
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
