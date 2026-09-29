#!/usr/bin/env python3
"""Capture the screenshots of the lab guide (docs/screenshots/guide-*.png): a viewport-sized picture of every view the
tour stops at, from the running portal (:8091) and looking glass (10.3.0.70:8080), in the default theme, light. Nothing
is changed: the steering map shows a Preview (not applied), the backup view a restore plan (not run).

    ~/cat8000v-ipsec/webapp/.venv/bin/python docs/guide_screenshots.py [--only NAME]"""
import argparse
from pathlib import Path

from playwright.sync_api import sync_playwright

LAB = Path(__file__).resolve().parents[1]
OUT = LAB / "docs" / "screenshots"; OUT.mkdir(exist_ok=True)
p = argparse.ArgumentParser()
p.add_argument("--portal", default="http://localhost:8091"); p.add_argument("--lg", default="http://10.3.0.70:8080")
p.add_argument("--only", default="")
a = p.parse_args()
PREFIX = "172.20.3.0/24"


def shot(pg, name, el=None, full=False):
    if a.only and a.only not in name:
        return
    f = str(OUT / f"{name}.png")
    if el:
        pg.locator(el).first.screenshot(path=f)
    else:
        pg.screenshot(path=f, full_page=full)
    print(f"  {name}.png")


with sync_playwright() as pw:
    b = pw.chromium.launch(channel="chrome", headless=True)
    ctx = b.new_context(viewport={"width": 1400, "height": 880}, device_scale_factor=1.5, color_scheme="light")
    ctx.add_init_script("try { localStorage.setItem('portal-theme','light'); localStorage.setItem('portal-skin','default');"
                        " localStorage.setItem('lg-theme','light'); localStorage.setItem('lg-skin','default'); } catch (e) {}")
    pg = ctx.new_page()

    # ---- portal ---------------------------------------------------------------------------------------------------
    pg.goto(a.portal + "/#tenants"); pg.wait_for_function("document.querySelectorAll('#tenants .card, #tenants > div').length > 0", timeout=120000)
    pg.wait_for_timeout(8000)                                              # the live state (BGP, routes, hosts) fills in
    shot(pg, "guide-portal-tenants")
    pg.evaluate("show('steering')"); pg.wait_for_function("SMAP !== null", timeout=120000)
    pg.evaluate("""async () => { $('st-pe').value = 'pe1'; $('st-pe').onchange(); $('st-tenant').value = 'tenant-b'; $('st-tenant').onchange();
                   $('st-prefix').value = '172.21.3.0/24'; $('st-via').value = 'p1 p3'; await steerPreview(); }""")
    pg.wait_for_timeout(1500)
    shot(pg, "guide-portal-steering-map", el="#sm-svg >> xpath=ancestor::div[contains(@class,'card')]")
    pg.evaluate("show('sla')"); pg.wait_for_function("SLA && SLA.pairs.length > 0", timeout=120000)
    pg.evaluate("slaChart('tenant-a', 'dc1-h1', 'dc3-h1')"); pg.wait_for_timeout(1500)
    shot(pg, "guide-portal-sla")
    shot(pg, "guide-portal-sla-chart", el="#sla-chart >> xpath=ancestor::div[contains(@class,'card')]")
    pg.evaluate("show('capacity')"); pg.wait_for_function("document.querySelector('#cap-dc tr')", timeout=120000); pg.wait_for_timeout(800)
    shot(pg, "guide-portal-capacity")
    pg.evaluate("show('backups')"); pg.wait_for_timeout(1500)
    first = pg.evaluate("fetch('/api/backups').then(r => r.json()).then(l => (l.find(x => !x.error) || {}).file)")
    if first:
        pg.evaluate(f"bkPlan({first!r})"); pg.wait_for_function("document.querySelector('#bk-go')", timeout=180000)
    shot(pg, "guide-portal-backups")
    pg.evaluate("show('runs')"); pg.wait_for_timeout(1200)
    rid = pg.evaluate("fetch('/api/runs').then(r => r.json()).then(l => (l.find(x => x.status === 'success' && x.mode === 'tenant') || l[0] || {}).id)")
    if rid:
        pg.evaluate(f"watch({rid!r})"); pg.wait_for_timeout(3000); pg.evaluate("if (watching) { clearInterval(watching); watching = null; }")
    shot(pg, "guide-portal-runs")

    # ---- looking glass --------------------------------------------------------------------------------------------
    pg.goto(a.lg + "/#overview", wait_until="networkidle"); pg.wait_for_timeout(2500)
    shot(pg, "guide-lg-overview")
    pg.goto(a.lg + "/#prefixes", wait_until="networkidle"); pg.wait_for_timeout(2000)
    shot(pg, "guide-lg-prefixes")
    pg.evaluate(f"showPath({PREFIX!r}, 'tenant-a')"); pg.wait_for_timeout(5000)
    shot(pg, "guide-lg-path")
    pg.goto(a.lg + "/#history", wait_until="networkidle"); pg.wait_for_timeout(2500)
    shot(pg, "guide-lg-history")
    b.close()
