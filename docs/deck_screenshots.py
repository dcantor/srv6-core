#!/usr/bin/env python3
"""Capture the screenshots the executive deck is built from (docs/screenshots/deck-*.png).

Separate from lg_screenshots.py and its full-page captures: a slide wants a viewport-sized, slide-shaped picture, not a
twelve-thousand-pixel column. Everything here comes from the running lab — the portal at :8091 and the looking glass at
10.3.0.70:8080 — and nothing is deployed: the wizard is opened, walked and cancelled.

    ~/cat8000v-ipsec/webapp/.venv/bin/python docs/deck_screenshots.py [--portal URL] [--lg URL] [--prefix 172.20.3.0/24]"""
import argparse
import time
from pathlib import Path

from PIL import Image
from playwright.sync_api import sync_playwright

LAB = Path(__file__).resolve().parents[1]
OUT = LAB / "docs" / "screenshots"; OUT.mkdir(exist_ok=True)

p = argparse.ArgumentParser()
p.add_argument("--portal", default="http://localhost:8091")
p.add_argument("--lg", default="http://10.3.0.70:8080")
p.add_argument("--prefix", default="172.20.3.0/24", help="a tenant LAN: it exists in the core's VPN table and in every VRF table")
p.add_argument("--only", default="", help="substring: capture only the shots whose name contains it")
a = p.parse_args()
W, H = 1500, 950
want = lambda n: not a.only or a.only in n or n in a.only        # noqa: E731 — either way round, so --only deck-portal-topology still opens the portal


def shoot(pg, name, clip=None, sel=None, pad=10, el=None, trim=0.0):
    """One capture. `el` shoots a whole element (even taller than the viewport), `sel` clips to one with a little air
    around it, `clip` to a rectangle. `trim` drops that fraction off the bottom afterwards."""
    def finish():
        if trim:
            f = OUT / f"{name}.png"
            im = Image.open(f); im.crop((0, 0, im.width, int(im.height * (1 - trim)))).save(f)
        print(f"  {name}.png")
    if el:
        pg.locator(el).first.screenshot(path=str(OUT / f"{name}.png"))
        finish(); return
    if sel:
        b = pg.locator(sel).first.bounding_box()
        clip = {"x": max(0, b["x"] - pad), "y": max(0, b["y"] - pad),
                "width": min(W - max(0, b["x"] - pad), b["width"] + 2 * pad), "height": min(H - max(0, b["y"] - pad), b["height"] + 2 * pad)}
    pg.screenshot(path=str(OUT / f"{name}.png"), clip=clip)
    finish()


with sync_playwright() as pw:
    b = pw.chromium.launch(channel="chrome", headless=True)
    pg = b.new_context(viewport={"width": W, "height": H}, device_scale_factor=2).new_page()
    pg.on("dialog", lambda d: d.accept())

    # ---- the tenant portal ---------------------------------------------------------------------------------------
    print("portal:" if want("deck-portal") else "portal: skipped")
    if not want("deck-portal"): pg.goto("about:blank")
    if want("deck-portal"):
      pg.goto(a.portal + "/#tenants", wait_until="domcontentloaded")
      pg.wait_for_function("document.querySelectorAll('#kpis .kpi').length > 0", timeout=120000)
      pg.wait_for_function("ST && ST.live_done", timeout=240000)                  # the live state collector has finished
      time.sleep(1.5)
      if want("deck-portal-tenants"): shoot(pg, "deck-portal-tenants")
      pg.evaluate("document.querySelector('.card.topo').scrollIntoView({block:'start'})"); time.sleep(1.2)
      if want("deck-portal-topology"): shoot(pg, "deck-portal-topology", el=".card.topo")

      if want("deck-portal-wizard"):
          pg.evaluate("window.scrollTo(0, 0)"); time.sleep(0.4)
          pg.evaluate("openWizard()"); pg.wait_for_function("document.getElementById('w-name').value !== ''", timeout=60000); time.sleep(1.0)
          shoot(pg, "deck-portal-wizard-1", sel="#wiz")
          pg.evaluate("wizNext()"); time.sleep(3.0)
          # the allocator answers from the running lab, so a site it cannot serve is refused here rather than half-built:
          # pe4's ports are all taken, so a third tenant cannot have a dc4 site. Keep that answer, then drop the site and go on.
          msg = pg.eval_on_selector("#w-msg", "e => e.textContent").strip()
          if msg:
              print(f"  (the wizard refused a site: {msg})")
              shoot(pg, "deck-portal-wizard-refused", sel="#wiz")
              dc = msg.split(":")[0].strip()
              pg.evaluate("dc => { const c = [...document.querySelectorAll('.w-dc')].find(x => x.value === dc); if (c) c.checked = false; }", dc)
              pg.evaluate("document.getElementById('w-msg').textContent = ''")
              shoot(pg, "deck-portal-wizard-1", sel="#wiz")
              pg.evaluate("wizNext()")
          pg.wait_for_function("document.querySelectorAll('#w-sites input[data-k]').length > 0", timeout=90000); time.sleep(1.2)
          shoot(pg, "deck-portal-wizard-2", sel="#wiz")
          pg.evaluate("wizNext()"); pg.wait_for_function("document.querySelectorAll('#w-review table tbody tr').length > 0", timeout=120000); time.sleep(1.2)
          shoot(pg, "deck-portal-wizard-3", sel="#wiz")
          pg.evaluate("document.getElementById('wiz').close()"); time.sleep(0.5)   # nothing is deployed

      # the portal has no hashchange handler (it routes once, at load), so the view is switched the way the nav does it
      if want("deck-portal-steering"):
          pg.evaluate("show('steering')"); time.sleep(3.0)
          pg.evaluate("window.scrollTo(0, 0)"); time.sleep(0.4)
          shoot(pg, "deck-portal-steering", el="#v-steering .card")

      if want("deck-portal-run"):
          pg.evaluate("show('runs')")
          pg.wait_for_function("document.querySelectorAll('#runs tbody tr').length > 0", timeout=90000); time.sleep(1.0)
          pg.evaluate("""() => {                     // the newest run that actually deployed something, else the newest
              const rows = [...document.querySelectorAll('#runs tbody tr')];
              (rows.find(r => /tenant|site/i.test(r.cells[1].textContent)) || rows[0]).click();
          }""")
          time.sleep(4.0)
          pg.evaluate("document.getElementById('runcard').scrollIntoView({block:'start'})"); time.sleep(0.8)
          shoot(pg, "deck-portal-run")

    # ---- the looking glass ---------------------------------------------------------------------------------------
    print("looking glass:" if want("deck-lg") else "looking glass: skipped")
    px = a.prefix.replace("/", "%2F")
    if want("deck-lg-overview"):
        pg.goto(a.lg + "/#overview", wait_until="domcontentloaded")
        pg.wait_for_function("document.querySelectorAll('#kpis .kpi').length > 0", timeout=120000); time.sleep(2.5)
        shoot(pg, "deck-lg-overview")
        shoot(pg, "deck-lg-topo", el=".card:has(#topo)")
        pg.evaluate("document.getElementById('ov-counts').scrollIntoView({block:'center'})"); time.sleep(1.0)
        shoot(pg, "deck-lg-views")

    if want("deck-lg-sessions"):
        pg.goto(a.lg + "/#peers", wait_until="domcontentloaded")
        pg.wait_for_function("document.querySelectorAll('#pe-rows tr').length > 0", timeout=120000); time.sleep(1.5)
        shoot(pg, "deck-lg-sessions")

    if want("deck-lg-prefixes"):
        pg.goto(a.lg + "/#prefixes", wait_until="domcontentloaded")
        pg.wait_for_function("document.querySelectorAll('#pf-rows tr').length > 3", timeout=120000); time.sleep(1.5)
        shoot(pg, "deck-lg-prefixes")

    if want("deck-lg-filter"):
        # the same tenant, asked of the reflectors' table and then of the routers' own tables: two answers, two counts
        pg.goto(a.lg + "/#prefixes", wait_until="domcontentloaded")
        pg.wait_for_function("document.querySelectorAll('#pf-rows tr').length > 3", timeout=120000); time.sleep(1.0)
        for via, name in (("rr-session", "deck-lg-filter-session"), ("router-api", "deck-lg-filter-api")):
            pg.select_option("#f-vrf", "tenant-a"); pg.select_option("#f-via", via)
            pg.evaluate("loadPrefixes()"); time.sleep(2.5)
            # the filter bar and the count it produced; the hint below it is about the *view* filter, not this one
            shoot(pg, name, el="#v-prefixes .card", trim=0.21)

    if want("deck-lg-path"):
        pg.goto(a.lg + "/#prefixes", wait_until="domcontentloaded")
        pg.wait_for_function("document.querySelectorAll('#pf-rows tr').length > 3", timeout=120000); time.sleep(1.0)
        pg.evaluate(f"showPath({a.prefix!r}, 'tenant-a')"); time.sleep(6.0)
        pg.evaluate("document.getElementById('pf-path-card').scrollIntoView({block:'start'})"); time.sleep(1.0)
        shoot(pg, "deck-lg-path")

    if want("deck-lg-prefix"):
        pg.goto(a.lg + f"/#prefix/{px}", wait_until="domcontentloaded")
        pg.wait_for_function("document.querySelectorAll('#px-paths .card').length > 1", timeout=120000); time.sleep(2.5)
        pg.evaluate("document.getElementById('px-paths').scrollIntoView({block:'start'}); window.scrollBy(0, -90)"); time.sleep(1.0)
        shoot(pg, "deck-lg-prefix-views")

    if want("deck-lg-timetravel"):
        pg.goto(a.lg + f"/#prefix/{px}", wait_until="domcontentloaded")
        pg.wait_for_function("document.querySelectorAll('#px-paths .card').length > 1", timeout=120000); time.sleep(2.0)
        # drag the slider back to just after the last recorded change, so the page shows a state that is not "now"
        pg.evaluate("""() => {
            const e = (PX.events || []).filter(x => x.kind !== 'withdraw');
            if (!e.length) return;
            const to = Math.ceil(e[0].ts) + 1;
            document.querySelector('#px-slider').value = to; sliderMoved(to);
        }""")
        time.sleep(7.0)
        pg.evaluate("document.getElementById('px-slider').scrollIntoView({block:'start'}); window.scrollBy(0, -80)"); time.sleep(1.0)
        shoot(pg, "deck-lg-timetravel")

    if want("deck-lg-history"):
        pg.goto(a.lg + f"/#prefix/{px}", wait_until="domcontentloaded")
        pg.wait_for_function("document.querySelectorAll('#px-events tr').length > 3", timeout=120000); time.sleep(2.0)
        pg.evaluate("document.getElementById('px-timeline').closest('.card').scrollIntoView({block:'start'})"); time.sleep(1.0)
        shoot(pg, "deck-lg-history")

    if want("deck-lg-withdraws"):
        pg.goto(a.lg + "/#history", wait_until="domcontentloaded")
        pg.wait_for_function("document.querySelectorAll('#h-rows tr').length > 3", timeout=120000); time.sleep(1.0)
        pg.select_option("#h-kind", "withdraw"); pg.evaluate("loadHistory()"); time.sleep(2.5)
        shoot(pg, "deck-lg-withdraws")

    if want("deck-lg-metrics"):
        pg.goto(a.lg + "/metrics", wait_until="domcontentloaded"); time.sleep(1.5)
        shoot(pg, "deck-lg-metrics", clip={"x": 0, "y": 0, "width": W, "height": 330})

    if want("deck-lg-api"):
        # the browser renders a JSON body as one unreadable line, so the real response is fetched and laid out as the
        # listing it would be on a terminal — the bytes are the API's, the formatting is jq's
        pg.goto(a.lg + "/#overview", wait_until="domcontentloaded"); time.sleep(2.0)
        body = pg.evaluate(r"""async () => {
            const r = await (await fetch('/api/peers')).json();
            const lines = JSON.stringify(r.peers ? r.peers[0] : r, null, 2).split('\n');
            return lines.slice(0, 26).join('\n') + (lines.length > 26 ? '\n  \u2026' : '');
        }""")
        pg.set_content("<!doctype html><meta charset='utf-8'>"
                       "<style>body{margin:0;background:#0C2129;padding:26px 30px;"
                       "font:15px/1.65 'DejaVu Sans Mono',Consolas,monospace;color:#D7E3E8}"
                       "b{color:#5EC8D8;font-weight:400}</style>"
                       f"<pre style='margin:0'><b>$ curl -s http://10.3.0.70:8080/api/peers | jq '.peers[0]'</b>\n\n{body}</pre>")
        time.sleep(0.5)
        shoot(pg, "deck-lg-api", el="pre")

    if want("deck-lg-routers"):
        pg.goto(a.lg + "/#routers", wait_until="domcontentloaded")
        pg.wait_for_function("document.querySelectorAll('#rt-rows tr').length > 3", timeout=120000); time.sleep(1.5)
        shoot(pg, "deck-lg-routers")

    if want("deck-lg-query"):
        pg.goto(a.lg + "/#query", wait_until="domcontentloaded"); time.sleep(1.5)
        pg.select_option("#q-device", "pe1")                       # a PE: it has both the VPN table and the tenant's VRF
        pg.fill("#q-command", f"show bgp ipv4 vpn rd 65000:103 {a.prefix}")
        pg.click("#q-run")
        pg.wait_for_function("document.getElementById('q-out').textContent.length > 80", timeout=90000); time.sleep(1.5)
        shoot(pg, "deck-lg-query", el="#v-query")

    b.close()
print("done")
