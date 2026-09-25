#!/usr/bin/env python3
"""Record docs/demo/lg-demo.mp4 — the BGP looking glass in detail, driven against the live collector.

Fourteen scenes: what it is and how it is wired, the overview and its map, the reflector sessions, what each view holds
and the transport it came over, the prefix table and its filters, the path router by router from two vantage points,
one prefix in every view at once, the history and the time slider, the global change log, the routers it reads, a live
show command, and the API underneath it all.

It only reads: every page here is a GET, the one command it runs is a `show`, and nothing in the lab is changed.

    ~/cat8000v-ipsec/webapp/.venv/bin/python docs/demo/record_lg.py [--url http://10.3.0.70:8080] [--prefix 172.20.3.0/24]"""
import argparse
import sys
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parent))
from demolib import Recorder                                          # noqa: E402

p = argparse.ArgumentParser()
p.add_argument("--url", default="http://10.3.0.70:8080")
p.add_argument("--out", default=str(Path(__file__).resolve().parent))
p.add_argument("--width", type=int, default=1500); p.add_argument("--height", type=int, default=900)
p.add_argument("--prefix", default="172.20.3.0/24", help="a tenant LAN: it exists in the core's VPN table and in every VRF table")
p.add_argument("--no-gif", action="store_true")
p.add_argument("--cards", action="store_true", help="just draw the title / wiring / closing cards to PNGs and stop")
a = p.parse_args()
OUT = Path(a.out); OUT.mkdir(parents=True, exist_ok=True)
R = Recorder(a.width, a.height)
PX = a.prefix.replace("/", "%2F")

# The looking glass's own colours: its header ink, the teal the core is drawn in, the green of the collector's sessions.
INK, TEAL, GREEN, PALE, MUTED = "#07161C", "#0E7490", "#15803D", "#C7D2E4", "#8FA3BF"
CARD_CSS = f"""
 * {{ box-sizing: border-box }}
 body {{ margin:0; width:{a.width}px; height:{a.height}px; background:{INK}; color:#fff;
         font-family: Calibri, Carlito, system-ui, sans-serif; display:flex; flex-direction:column;
         justify-content:center; padding:0 76px }}
 h1 {{ font-family: Cambria, Caladea, serif; font-size:52px; margin:0 0 14px; font-weight:700 }}
 h2 {{ font-family: Cambria, Caladea, serif; font-size:38px; margin:0 0 22px; font-weight:700 }}
 .sub {{ font-size:23px; color:{PALE}; margin-bottom:26px }}
 .meta {{ font-size:15.5px; color:{MUTED}; line-height:1.55 }}
 .wire {{ display:grid; grid-template-columns:auto minmax(210px,1fr) auto auto; grid-template-rows:auto auto;
          column-gap:18px; row-gap:26px; align-items:center; margin:10px 0 34px }}
 .node {{ background:#10303A; border:2px solid {TEAL}; border-radius:10px; padding:14px 20px }}
 .node b {{ display:block; font:700 21px/1.2 "DejaVu Sans Mono", Consolas, monospace; color:#5EC8D8 }}
 .node span {{ display:block; font-size:13.5px; color:{MUTED}; margin-top:5px }}
 .lg {{ grid-column:1; grid-row:1 / span 2 }}
 .rr {{ grid-column:3; border-color:{GREEN} }}
 .rr b {{ color:#7FD69B }}
 .link {{ grid-column:2; height:0; border-top:2px dashed {GREEN}; position:relative }}
 .link span {{ position:absolute; left:50%; top:-26px; transform:translateX(-50%); white-space:nowrap;
               font:13.5px/1 "DejaVu Sans Mono", Consolas, monospace; color:{MUTED} }}
 .got {{ grid-column:4; grid-row:1 / span 2; font-size:15.5px; color:{PALE}; max-width:250px; line-height:1.45 }}
 .feeds {{ display:flex; gap:20px; margin-bottom:26px }}
 .feed {{ flex:1; border-left:3px solid {TEAL}; padding:2px 0 2px 14px }}
 .feed b {{ display:block; font:700 15px/1.3 "DejaVu Sans Mono", Consolas, monospace; color:#5EC8D8 }}
 .feed span {{ font-size:14px; color:{PALE}; line-height:1.4 }}
 pre {{ font: 14.5px/1.6 "DejaVu Sans Mono", Consolas, monospace; color:#D7E3E8; margin:0;
        background:#0C2129; border-radius:10px; padding:20px 24px }}
 .ep {{ font:15px/1.9 "DejaVu Sans Mono", Consolas, monospace; color:#5EC8D8; margin-bottom:18px }}
 .cols {{ display:flex; gap:24px; margin:26px 0 30px }}
 .col {{ flex:1; background:#10303A; border-radius:10px; padding:24px 22px }}
 .col h3 {{ font-family:Cambria,Caladea,serif; font-size:20px; margin:0 0 10px }}
 .col p {{ font-size:14.5px; line-height:1.45; color:{PALE}; margin:0 }}
"""


PEERS_JS = r"""async () => {
    const r = await (await fetch('/api/peers')).json();
    const lines = JSON.stringify(r.peers ? r.peers[0] : r, null, 2).split('\n');
    return lines.slice(0, 20).join('\n') + (lines.length > 20 ? '\n  \u2026' : '');
}"""


def api_card(peers):
    return ("<h2>Everything the page does is the API</h2>"
            "<div class='ep'>/api/prefixes &middot; /api/prefix &middot; /api/path &middot; /api/state?at=&hellip; &middot; "
            "/api/history &middot; /api/routers &middot; /api/peers &middot; /api/query &middot; /metrics</div>"
            f"<pre>$ curl -s http://10.3.0.70:8080/api/peers | jq '.peers[0]'\n\n{peers}</pre>")


def closing_card():
    return """<h2>What it is for</h2>
      <div class='cols'>
        <div class='col'><h3>The attributes, unaltered</h3><p>A reflector client sees the update the PE originated: RD, route
          targets, SRv6 SID, label, originator-id, cluster list, AS path. Nothing is parsed out of a screen.</p></div>
        <div class='col'><h3>Every answer, labelled</h3><p>The core's table, each router's own tables and each router's RIB,
          side by side — with the transport each came over, because the same prefix seen two ways is two answers.</p></div>
        <div class='col'><h3>And yesterday's answer too</h3><p>Reconciled history, not samples: thirty days of announces,
          changes and withdraws, with the tables and the path re-drawn for any moment in that window.</p></div>
      </div>
      <div class='meta'>http://192.168.50.231:8092 &middot; the lab-side address is http://10.3.0.70:8080 &middot; ./lab.sh lg status<br>
      The code is lg/ in this repository; the slides are docs/srv6-workflows.pptx.</div>"""


CARDS = [0]


def card(page, body, seconds=4.0):
    """A title or explainer slide, drawn in the same browser so it encodes identically to the live frames."""
    page.set_content(f"<!doctype html><meta charset='utf-8'><style>{CARD_CSS}</style>{body}")
    time.sleep(0.4)
    if a.cards:
        CARDS[0] += 1
        page.screenshot(path=str(OUT / f"card{CARDS[0]}.png")); print(f"  card{CARDS[0]}.png")
        return
    R.hold(page, seconds)


def go(page, frag, ready=None, settle=1.5, timeout=150000):
    """The page routes on hashchange, so a fragment is enough — but wait for the view to have filled itself in."""
    page.goto(a.url + "/" + frag)
    if ready: page.wait_for_function(ready, timeout=timeout)
    time.sleep(settle); R.overlay(page)


with sync_playwright() as pw:
    browser = pw.chromium.launch(channel="chrome", headless=True)
    ctx = browser.new_context(viewport={"width": a.width, "height": a.height})
    page = ctx.new_page(); page.on("dialog", lambda d: d.accept())

    # ---- title and the wiring ------------------------------------------------------------------------------------
    card(page, f"""<h1>The BGP Looking Glass</h1>
      <div class='sub'>A passive route collector in the SRv6 core &mdash; every VPN prefix, its attributes, and how they changed</div>
      <div class='meta'>Alpine + FRR on 512 MiB &middot; iBGP to both route reflectors &middot; a direct read of all twelve routers<br>
      SQLite history &middot; Flask API and page &middot; Prometheus metrics</div>""", 5.0)

    card(page, """<h2>How it is wired</h2>
      <div class='wire'>
        <div class='node lg'><b>lg</b><span>Alpine + FRR &middot; 512 MiB<br>AS 65000 &middot; router-id 10.255.0.21</span></div>
        <div class='link'><span>eth1 &nbsp;fd00:b:0:121::2</span></div>
        <div class='node rr'><b>p1</b><span>route reflector &middot; eth5</span></div>
        <div class='link'><span>eth2 &nbsp;fd00:b:0:321::2</span></div>
        <div class='node rr'><b>p3</b><span>route reflector &middot; eth5</span></div>
        <div class='got'>the whole VPNv4 / VPNv6 table,<br>one copy per reflector</div>
      </div>
      <div class='feeds'>
        <div class='feed'><b>SQLite</b><span>every announce, attribute change and withdraw, per path</span></div>
        <div class='feed'><b>poll</b><span>each PE, CE and the firewall, over their HTTPS API and over SSH</span></div>
        <div class='feed'><b>Flask</b><span>/api/&hellip; &middot; the page &middot; /metrics</span></div>
      </div>
      <div class='meta'>It joins as a reflector <b>client</b>, so it receives the table the PEs originated &mdash; and an
      outbound deny route-map means it announces nothing back.</div>""", 7.5)

    if a.cards:                            # just look at the drawn cards; the live scenes are skipped
        page.goto(a.url + "/#overview"); time.sleep(3.0)
        card(page, api_card(page.evaluate(PEERS_JS)))
        card(page, closing_card())
        ctx.close(); browser.close(); raise SystemExit(0)

    # ---- 1. the overview -----------------------------------------------------------------------------------------
    go(page, "#overview", "document.querySelectorAll('#kpis .kpi').length > 0", settle=3.0)
    R.caption(page, "What the collector holds, right now",
              "prefixes and paths in the core's VPN table, the tenant VRFs it has seen, the reflector sessions, the last hour's churn — and the size of the history behind it")
    R.hold(page, 4.0)
    R.note(page, "#kpis .kpi >> nth=0", "the core's VPNv4 + VPNv6 table, not one router's view"); R.hold(page, 3.0); R.note_off(page)
    R.note(page, "#kpis .kpi >> nth=5", "every announce, change and withdraw it has ever reconciled"); R.hold(page, 3.0); R.note_off(page)

    # ---- 2. the map ----------------------------------------------------------------------------------------------
    R.caption(page, "The testbed, drawn from the model",
              "the core band with the three P routers, a lane per data centre, and the collector on its two sessions — a core link goes red if no router reports an IS-IS adjacency for it")
    R.scroll_to(page, "#topo", settle=1.2); R.hold(page, 4.5)
    R.note(page, "#topo", "the two green dashed lines are the collector's iBGP sessions to p1 and p3"); R.hold(page, 3.5); R.note_off(page)

    # ---- 3. the sessions -----------------------------------------------------------------------------------------
    R.caption(page, "Why a session, and not screen-scraping",
              "the collector receives the same update the reflector sent: route distinguisher, route targets, SRv6 SID and label, originator-id, cluster list, AS path, local preference")
    R.scroll_to(page, "#ov-peers", settle=1.0)
    page.evaluate("window.scrollBy(0, -140)"); time.sleep(0.6); R.hold(page, 4.5)
    R.note(page, "#ov-peers tr >> nth=0", "Established to both reflectors — 60 VPNv4 and 16 VPNv6 accepted, 0 sent"); R.hold(page, 4.0); R.note_off(page)
    R.caption(page, "The subtle part: extended next-hop capability",
              "without it FRR would rewrite the IPv6 next hop of every VPNv4 route to its own address, and every prefix would look as though the reflector had originated it", numbered=False)
    R.hold(page, 4.5)

    # ---- 4. what each view holds ---------------------------------------------------------------------------------
    R.caption(page, "And it reads every router directly, too",
              "the core's table says what travels between PEs; what a tenant actually has is the VRF table after import, and what the box will do with a packet is in its RIB")
    R.scroll_to(page, "#ov-counts", settle=1.2)
    page.evaluate("window.scrollBy(0, -120)"); time.sleep(0.6); R.hold(page, 4.5)
    R.note(page, "#ov-counts tr >> nth=1", "every row carries the transport it came over, and when it was last collected"); R.hold(page, 4.0); R.note_off(page)
    R.caption(page, "Three transports, one page",
              "router-api — the RIB per VRF and family, the router's own VPN table, its IS-IS adjacencies · router-ssh — the per-VRF BGP tables, where the API has no JSON form · rr-session — the whole VPN table", numbered=False)
    R.hold(page, 5.0)

    # ---- 5. the change log on the overview -----------------------------------------------------------------------
    R.caption(page, "Every collection is reconciled, not snapshotted",
              "a path that is new raises an announce, one whose attributes differ raises a change carrying the fields that changed, one that has gone raises a withdraw")
    R.scroll_to(page, "#ov-events", settle=1.2); R.hold(page, 4.5)

    # ---- 6. the routers it reads ---------------------------------------------------------------------------------
    go(page, "#routers", "document.querySelectorAll('#rt-rows tr').length > 3")
    R.caption(page, "Every box it reads, and how",
              "identity, the transports it was read through, when, how many RIB / BGP / VPN entries it holds — and the IS-IS adjacencies it reported")
    R.hold(page, 5.0)
    R.note(page, "#rt-transports", "the API key is rendered into each router's configuration like everything else"); R.hold(page, 3.5); R.note_off(page)
    R.scroll_to(page, "#rt-holds", settle=1.0)
    R.caption(page, "…and what each one is holding", "by family and VRF, per router — the second half of the same page", numbered=False)
    R.hold(page, 4.0)

    # ---- 7. the prefix table -------------------------------------------------------------------------------------
    go(page, "#prefixes", "document.querySelectorAll('#pf-rows tr').length > 3")
    R.caption(page, "The prefix table",
              "one row per path, with its next hop, AS path, local preference, SRv6 SID and route targets — and, on the right, the view it came from and how it was read")
    R.hold(page, 4.5)
    R.note(page, "#pf-rows tr >> nth=0", "the SID column is the egress PE's End.DT46 — the tenant's decapsulation instruction"); R.hold(page, 4.0); R.note_off(page)

    R.caption(page, "Filter it the way an operator asks",
              "view, address family, VRF, route distinguisher, origin AS — and “how it was read”, because the same prefix seen two ways is two answers")
    el = R.move_to(page, "#f-vrf"); page.select_option("#f-vrf", "tenant-a"); time.sleep(0.4)
    page.evaluate("loadPrefixes()"); time.sleep(2.0); R.overlay(page); R.snap(page, 1.2)
    R.note(page, "#pf-count", "tenant-a only"); R.hold(page, 2.5); R.note_off(page)
    el = R.move_to(page, "#f-via"); page.select_option("#f-via", "rr-session"); time.sleep(0.4)
    page.evaluate("loadPrefixes()"); time.sleep(2.0); R.overlay(page); R.snap(page, 1.2)
    R.note(page, "#pf-count", "…and only what the reflectors handed over"); R.hold(page, 3.5); R.note_off(page)
    page.select_option("#f-via", "router-api"); page.evaluate("loadPrefixes()"); time.sleep(2.0); R.overlay(page)
    R.note(page, "#pf-count", "…versus what the routers themselves installed"); R.hold(page, 4.0); R.note_off(page)
    page.evaluate("resetFilters(); loadPrefixes()"); time.sleep(2.0); R.overlay(page); R.snap(page, 1.0)

    # ---- 8. the path ---------------------------------------------------------------------------------------------
    R.caption(page, "The path, router by router",
              f"every row has a path button: where traffic to {a.prefix} actually goes, from a vantage point you choose")
    page.evaluate("f => showPath(f, 'tenant-a')", a.prefix); time.sleep(6.0)
    R.scroll_to(page, "#pf-path-card", settle=1.2); R.overlay(page); R.hold(page, 5.0)
    R.note(page, "#pp-body", "the map lights the path up and numbers its hops"); R.hold(page, 3.5); R.note_off(page)
    page.evaluate("window.scrollBy(0, 560)"); time.sleep(0.8)
    R.caption(page, "A card per hop, and who said so",
              "the host, the CE's eBGP hand-off, the ingress PE's import and SRv6 encapsulation, the P routers it crosses, the egress PE's End.DT46 decapsulation, the CE that owns it", numbered=False)
    R.hold(page, 5.5)
    R.caption(page, "Two sources, and the card says which",
              "the route and its attributes come from the session; what the router will really do with the packet comes from its own RIB", numbered=False)
    R.hold(page, 4.0)
    R.caption(page, "Change the vantage point", "the same prefix, asked from a different router — equal-cost paths are named rather than silently collapsed into one line")
    R.scroll_to(page, "#pf-path-card", settle=0.8)
    opts = page.evaluate("[...document.querySelectorAll('#pp-from option')].map(o => o.value)")
    other = next((o for o in opts if o and o not in ("pe1",)), None)
    if other:
        R.move_to(page, "#pp-from"); page.select_option("#pp-from", other)
        time.sleep(6.0); R.overlay(page); R.hold(page, 5.0)

    # ---- 9. one prefix, every view -------------------------------------------------------------------------------
    go(page, f"#prefix/{PX}", "document.querySelectorAll('#px-paths .card').length > 1", settle=3.0)
    R.caption(page, "One prefix, every view at once",
              "the core's VPN table, each reflector's and PE's own VPN table, each PE's and CE's VRF table, and every router's RIB — each with its full attribute set and a pill saying how it was read")
    R.scroll_to(page, "#px-paths", settle=1.2); R.hold(page, 5.0)
    page.evaluate("window.scrollBy(0, 520)"); time.sleep(0.8); R.hold(page, 4.0)
    R.caption(page, "Where the answers differ is the diagnosis",
              "in the core but missing from a VRF is an import problem; in the VRF but not in the RIB is a selection problem — the page puts them side by side", numbered=False)
    page.evaluate("window.scrollBy(0, 520)"); time.sleep(0.8); R.hold(page, 4.5)

    # ---- 10. the history and the slider --------------------------------------------------------------------------
    page.evaluate("window.scrollTo(0, 0)"); time.sleep(0.6)
    R.caption(page, "Everything that has happened to it",
              "the timeline and the log: announce, change with the fields that changed, withdraw — kept per path, for thirty days")
    R.scroll_to(page, "#px-timeline", settle=1.0)
    page.evaluate("window.scrollBy(0, -110)"); time.sleep(0.6); R.overlay(page); R.hold(page, 5.0)

    page.evaluate("window.scrollTo(0, 0)"); time.sleep(0.6); R.overlay(page)
    R.caption(page, "And the slider replays it",
              "drag it and every table on the page re-renders as it stood at that moment — and so does the path diagram")
    R.move_to(page, "#px-slider")
    n = page.evaluate("(PX.events || []).length")
    for frac in (0.75, 0.5, 0.25):                      # walk it back in three steps, letting each state settle
        page.evaluate("""f => {
            const s = document.querySelector('#px-slider');
            const to = Math.round(+s.min + f * (+s.max - +s.min));
            s.value = to; sliderMoved(to);
        }""", frac)
        time.sleep(4.5); R.overlay(page); R.snap(page, 1.4)
    time.sleep(2.5); R.overlay(page); R.hold(page, 3.5)
    R.note(page, "#px-when", "how many paths, in how many views, and how much of the history had happened by then"); R.hold(page, 4.0); R.note_off(page)
    R.caption(page, "Step between the changes themselves",
              "◀ and ▶ snap to the moment just after each recorded change, so nothing is missed between two drags of a slider", numbered=False)
    R.click(page, "button[title*='previous change']", settle=4.0); R.hold(page, 3.0)
    R.caption(page, "The path is rebuilt for that moment too",
              "the forwarding state comes out of the RIB history, not out of today's routers — so a policy that steered this prefix an hour ago is still drawn the way it steered it", numbered=False)
    R.scroll_to(page, "#px-path", settle=1.5); R.overlay(page); R.hold(page, 5.0)
    page.evaluate("window.scrollTo(0, 0)"); time.sleep(0.5)
    R.click(page, "#px-now", settle=3.0)
    R.caption(page, "…and back to now", "“Now” returns the whole page to live", numbered=False)
    R.hold(page, 3.0)

    # ---- 11. the global change log -------------------------------------------------------------------------------
    go(page, "#history", "document.querySelectorAll('#h-rows tr').length > 3")
    R.caption(page, "The change log for the whole core",
              "every announce, change and withdraw the collector has reconciled, newest first, each with its diff — filterable by prefix, by kind and by age")
    R.hold(page, 4.5)
    R.move_to(page, "#h-kind"); page.select_option("#h-kind", "withdraw"); time.sleep(0.4)
    page.evaluate("loadHistory()"); time.sleep(2.5); R.overlay(page)
    R.caption(page, "“When did this prefix go away?”", "withdraws only — the question a looking glass without a memory cannot answer at all", numbered=False)
    R.hold(page, 4.5)

    # ---- 12. the live query --------------------------------------------------------------------------------------
    go(page, "#query", settle=2.0)
    R.caption(page, "And the classic button: a command on a router",
              "only show …, ping and traceroute, and only on the lab's own devices — the same HTTPS API the collector polls with")
    R.hold(page, 3.5)
    R.move_to(page, "#q-device"); page.select_option("#q-device", "pe1"); time.sleep(0.4)
    el = R.move_to(page, "#q-command"); el.click(); el.fill("")
    el.type(f"show bgp ipv4 vpn rd 65000:103 {a.prefix}", delay=28); R.snap(page, 1.0)
    R.click(page, "#q-run", settle=1.0)
    page.wait_for_function("document.getElementById('q-out').textContent.length > 80", timeout=120000); time.sleep(1.5)
    R.overlay(page); R.hold(page, 6.0)
    R.note(page, "#q-out", "the same route the collector's session carries — RT, originator, cluster list, remote SID"); R.hold(page, 4.5); R.note_off(page)

    # ---- 13. the API ---------------------------------------------------------------------------------------------
    peers = page.evaluate(PEERS_JS)
    R.scene += 1                                # the API is a scene of its own, even though it is drawn rather than filmed
    card(page, api_card(peers), 7.5)
    page.goto(a.url + "/metrics"); time.sleep(2.5)
    R.caption(page, "…and the numbers go to Prometheus",
              "paths per VRF and family, churn, session state and collection health — scraped for the dashboard's looking-glass row and three alerts", numbered=False)
    R.hold(page, 5.5)

    # ---- closing -------------------------------------------------------------------------------------------------
    card(page, closing_card(), 6.0)

    ctx.close(); browser.close()

mp4 = R.write_mp4(OUT / "lg-demo.mp4")
secs = sum(d for _, d in R.frames)
print(f"{mp4} ({mp4.stat().st_size / 1e6:.1f} MB, {len(R.frames)} frames, {secs / 60:.1f} min, {R.scene} scenes)")
if not a.no_gif:
    gif, g = R.write_gif(OUT / "lg-demo.gif")
    print(f"{gif} ({gif.stat().st_size / 1e6:.1f} MB, {g:.0f}s)")
