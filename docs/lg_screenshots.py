#!/usr/bin/env python3
"""Capture the BGP looking glass's pages for the README (docs/screenshots/lg-*.png).

Needs a browser: run it with the cat8000v-ipsec portal venv, which has Playwright —
    ~/cat8000v-ipsec/webapp/.venv/bin/python docs/lg_screenshots.py [http://10.3.0.70:8080]
The looking glass must be running (./lab.sh lg status)."""
import subprocess, sys, time
from pathlib import Path

LAB = Path(__file__).resolve().parents[1]
OUT = LAB / "docs" / "screenshots"
URL = sys.argv[1] if len(sys.argv) > 1 else "http://10.3.0.70:8080"
# a prefix worth showing: a tenant LAN, which exists in the core's VPN table and in every PE's and CE's VRF table
PREFIX = sys.argv[2] if len(sys.argv) > 2 else "172.20.3.0/24"

from playwright.sync_api import sync_playwright                     # noqa: E402

SHOTS = [("lg-overview", "#overview", None), ("lg-prefixes", "#prefixes", None), ("lg-routers", "#routers", None),
         ("lg-path", "#prefixes", "path"), ("lg-prefix", f"#prefix/{PREFIX.replace('/', '%2F')}", None),
         ("lg-query", "#query", "query")]

with sync_playwright() as pw:
    b = pw.chromium.launch(channel="chrome", headless=True)   # the system Chrome, as docs/topology.py does
    pg = b.new_page(viewport={"width": 1500, "height": 1100}, device_scale_factor=2)
    for name, frag, action in SHOTS:
        pg.goto(URL + "/" + frag, wait_until="networkidle"); pg.wait_for_timeout(1500)
        if action == "query":                                        # run one so the page is not an empty form
            pg.fill("#q-command", "show bgp ipv4 vpn rd 65000:103 172.20.3.0/24")
            pg.click("#q-run"); pg.wait_for_timeout(4000)
        if action == "path":                                         # the path card, open on a tenant LAN
            pg.evaluate(f"showPath({PREFIX!r}, 'tenant-a')"); pg.wait_for_timeout(5000)
        pg.screenshot(path=str(OUT / f"{name}.png"), full_page=True)
        print(f"wrote {OUT / f'{name}.png'}")
    b.close()
