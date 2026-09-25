#!/usr/bin/env python3
"""Build docs/looking-glass.pptx — the executive overview of the BGP looking glass, on its own and in detail.

The same walkthrough as docs/demo/lg-demo.mp4, slide by slide. The layout description and both renderers are decklib's;
this file is the deck's content. Alongside the PowerPoint it writes an HTML replica at the same coordinates, because
this host has no LibreOffice and a deck nobody has looked at is a deck nobody should send.
Run it with the cat8000v-ipsec portal venv, which has python-pptx and Pillow —
   ~/cat8000v-ipsec/webapp/.venv/bin/python docs/build_lg_deck.py            the .pptx and the preview
   ~/cat8000v-ipsec/webapp/.venv/bin/python docs/build_lg_deck.py --preview  the preview only
Screenshots come from docs/screenshots/deck-lg-*.png (captured against the live collector by docs/deck_screenshots.py)."""
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from decklib import BODY, CARD, DEEP, GREEN, HEAD_FONT, INK, LINE, MUTED, PALE, TEAL, W, Deck, para   # noqa: E402

OUT = HERE / "looking-glass.pptx"
d = Deck(HERE / "screenshots")
slide, text, box, shot, caption, head = d.slide, d.text, d.box, d.shot, d.caption, d.head
side_note, shot_slide, two_up_slide, wide_slide = d.side_note, d.shot_slide, d.two_up_slide, d.wide_slide
columns_slide, stack_slide = d.columns_slide, d.stack_slide


def title_slide():
    s = slide(INK)
    t = text(s, 0.9, 2.1, 10.2, 1.9, size=40, color="FFFFFF", bold=True, font=HEAD_FONT, spacing=1.0)
    para(t, "The BGP Looking Glass", space_after=2)
    sub = text(s, 0.9, 3.1, 9.5, 1.0, size=21, color=PALE)
    para(sub, "A passive route collector in the SRv6 core — every VPN prefix, its attributes, and how they changed", space_after=0)
    m = text(s, 0.9, 4.65, 11.5, 1.2, size=13, color="7E97A1", spacing=1.4)
    para(m, "Alpine + FRR on 512 MiB · iBGP to both route reflectors · a direct read of all twelve routers", space_after=5)
    para(m, "SQLite history · Flask API and page · Prometheus metrics · http://192.168.50.231:8092", space_after=0)
    d.circle(s, 11.3, 2.2, 1.25)
    n = text(s, 11.3, 2.57, 1.25, 0.6, size=25, color="FFFFFF", bold=True, align="c", font=HEAD_FONT)
    para(n, "30 d", space_after=0)
    l = text(s, 10.55, 3.57, 2.2, 0.5, size=12, color=PALE, align="c")
    para(l, "of every change, kept", space_after=0)


def stat_slide():
    s = slide()
    head(s, "What it holds", "Read from the collector itself while these slides were made — nothing on this page is typed in.", size=32)
    stats = [("76 / 152", "prefixes, paths", "the core's VPNv4 + VPNv6 table, not one router's view"),
             ("2 / 2", "reflector sessions", "one link to p1, one to p3 — 60 VPNv4 and 16 VPNv6 accepted on each, 0 sent"),
             ("13", "routers read directly", "their own RIB, VPN and per-VRF tables, over their API and over SSH"),
             ("25,235", "recorded changes", "announces, attribute changes and withdraws, reconciled per path")]
    x = 0.75
    for big, small, note in stats:
        box(s, x, 2.05, 2.85, 2.05)
        b = text(s, x + 0.25, 2.25, 2.4, 0.75, size=26, color=TEAL, bold=True, font=HEAD_FONT)
        para(b, big, space_after=0)
        l = text(s, x + 0.25, 2.95, 2.4, 0.4, size=13, color=INK, bold=True)
        para(l, small, space_after=0)
        n = text(s, x + 0.25, 3.32, 2.4, 0.8, size=10.5, color=MUTED, spacing=1.15)
        para(n, note, space_after=0)
        x += 3.05
    box(s, 0.75, 4.45, 11.83, 2.25, fill=CARD)
    q = text(s, 1.1, 4.75, 11.1, 1.9, size=15, color=BODY, spacing=1.35)
    para(q, "A looking glass that answers three questions, not one.", bold=True, space_after=8)
    para(q, "What does the core carry, with the attributes the originating PE set? What does each router actually have, "
            "after import and after selection? And what did either of those look like at some moment in the past? The "
            "first needs a BGP session, the second needs the routers themselves, and the third needs a memory — so it "
            "has all three, and says which one every answer came from.", space_after=0)


def wiring_slide():
    """Drawn rather than screenshotted: the one thing about this box that no page of it shows."""
    s = slide()
    head(s, "How it is wired", "It joins the core as a reflector client on its own two links, and reads every other router besides.", size=30)
    box(s, 0.85, 2.45, 3.25, 1.65, line=TEAL, width=2)
    t = text(s, 1.12, 2.72, 2.75, 1.15, size=13, color=BODY, spacing=1.3)
    para(t, "lg", size=20, color=TEAL, bold=True, space_after=6)
    para(t, "Alpine + FRR · 512 MiB\nAS 65000 · router-id 10.255.0.21", size=11.5, color=MUTED, space_after=0)
    for y, label, name in ((2.78, "eth1   fd00:b:0:121::2", "p1"), (3.88, "eth2   fd00:b:0:321::2", "p3")):
        box(s, 4.25, y - 0.015, 3.15, 0.03, fill=GREEN, line=GREEN, radius=0)
        lt = text(s, 4.35, y - 0.42, 3.0, 0.35, size=11, color=MUTED)
        para(lt, label, space_after=0)
        box(s, 7.5, y - 0.42, 2.35, 0.84, line=GREEN, width=2)
        nt = text(s, 7.75, y - 0.24, 1.9, 0.6, size=13, color=BODY, spacing=1.25)
        para(nt, name, size=16, color=GREEN, bold=True, space_after=2)
        para(nt, "route reflector · eth5", size=10.5, color=MUTED, space_after=0)
    gt = text(s, 10.05, 2.85, 2.6, 1.0, size=12.5, color=BODY, spacing=1.35)
    para(gt, "the whole VPNv4 / VPNv6 table, one copy per reflector — and nothing announced back", space_after=0)
    feeds = [("SQLite", "every announce, attribute change and withdraw, per path — thirty days, pruned hourly"),
             ("poll", "each PE, CE and the firewall, over their own HTTPS API and over SSH, every two minutes"),
             ("Flask", "the page, /api/… for anything that wants the same answers, and /metrics for Prometheus")]
    x = 0.85
    for name, what in feeds:
        box(s, x, 4.75, 3.85, 1.35, fill=CARD)
        ft = text(s, x + 0.28, 5.0, 3.3, 0.9, size=12, color=BODY, spacing=1.3)
        para(ft, name, size=14, color=TEAL, bold=True, space_after=5)
        para(ft, what, space_after=0)
        x += 3.99
    f = text(s, 0.85, 6.35, 11.8, 0.6, size=12.5, color=MUTED, spacing=1.3)
    para(f, "The reflectors' side of both links is rendered by tools/render.py like every other interface, and the VM is "
            "modelled in Nautobot like every other device — nautobot render --check compares its frr.conf and its lgd.json too.", space_after=0)


title_slide()
stat_slide()
wiring_slide()

s, x, ih = shot_slide("The page you land on", "What the collector holds, the sessions it holds it over, the testbed, the per-family count over time, what each view holds, and the newest changes.",
                      "deck-lg-overview", "One page: the answer to “is it healthy and what does it have?” before any question is asked", img_w=8.3)
side_note(s, x, h=ih, title="Read the top row first", items=[
    "Prefixes and paths are the core's table; tenant VRFs is how many the collector has ever seen.",
    "Reflector sessions is the one number that invalidates everything else if it is not 2/2.",
    "Changes in the last hour is the churn: a quiet core is a flat line, and a noisy one is visible here before anyone reports it.",
    "History kept is the size of the memory behind every question on the following slides."])

wide_slide("Where it sits in the core", "The collector hangs off both route reflectors on its own point-to-point links, and touches nothing else.",
           "deck-lg-topo", "Drawn from the model: the core band with the three P routers, a lane per data centre, and the two green sessions on the right",
           [("It is a client, not a peer", "p1 and p3 reflect to it as they reflect to a PE — the whole table, with the originating "
                                           "PE's own attributes."),
            ("It announces nothing", "An outbound deny route-map says so, and both sessions report 0 sent: the core cannot be "
                                     "affected by it."),
            ("A red link is a real fault", "Core links go red where no router reports an IS-IS adjacency — coloured from what the "
                                           "routers say.")], img_h=2.9)

s, x, ih = shot_slide("Why a session, and not screen-scraping", "The collector receives the same update the reflector sent — route distinguisher, route targets, SRv6 SID and label, originator-id, cluster list, AS path, local preference.",
                      "deck-lg-sessions", "The sessions, and the health of every collection beneath them", img_w=8.3, img_h=4.5)
side_note(s, x, h=ih, title="The subtle part", items=[
    "The session negotiates capability extended-nexthop. Without it FRR rewrites the IPv6 next hop of every VPNv4 route to its own address — and every prefix in the core would look as though the reflector had originated it.",
    "With it, the originating PE's loopback is still the next hop, which is what makes the path on a later slide reconstructable at all.",
    "Screen-scraping a `show` command gives you the text a router chose to print. A session gives you what it sent."])

s, x, ih = shot_slide("And it reads every router directly, too", "The core's table says what travels between PEs. What a tenant actually has is the VRF table after import; what the box will do with a packet is in its RIB.",
                      "deck-lg-views", "What each view holds, per router, VRF and family — with the transport it came over and when it was collected", img_w=8.3, img_h=4.5)
side_note(s, x, h=ih, title="Three transports, one page", items=[
    "rr-session — the whole VPNv4 / VPNv6 table, as the reflectors handed it over.",
    "router-api — each router's own HTTPS API: its RIB per VRF and family, its own VPN table, its IS-IS adjacencies.",
    "router-ssh — the per-VRF BGP tables, where the API has no JSON form for the question and the text form loses the attributes.",
    "Every row, everywhere in the product, carries which one it came from."])

s, x, ih = shot_slide("Its account of its own work", "The Routers page: identity, the transports each box was read through, when, how much it holds, and the IS-IS adjacencies it reported.",
                      "deck-lg-routers", "Thirteen routers, each with its own API key, read on a two-minute cycle", img_w=8.3, img_h=4.5)
side_note(s, x, h=ih, title="Collection is a first-class thing", items=[
    "A collection that failed is visible here rather than showing up as a prefix that quietly went missing.",
    "Each router's HTTPS API is rendered into its configuration like the rest of it — key srv6core-lab-looking-glass, reachable only from the collector and the lab host.",
    "So the same interface is open to anything else that wants it; the looking glass is one consumer, not a privileged one."])

s, x, ih = shot_slide("The prefix table", "One row per path, with its next hop, AS path, local preference, SRv6 SID and route targets — and, on the right, the view it came from and how it was read.",
                      "deck-lg-prefixes", "Filters for view, family, VRF, route distinguisher, origin AS and transport, plus free text over prefix, next hop, SID, route target and AS path", img_w=8.3, img_h=4.5)
side_note(s, x, h=ih, title="The classic questions", items=[
    "“Who originates this?” — origin AS and originating node are columns, not something to derive from an AS path.",
    "“Is it in the tenant's VRF, or only in the core?” — the view filter answers it directly.",
    "“What SID does it carry?” — the egress PE's End.DT46 is on the row, and the free-text box searches it.",
    "Every row has a path button, which is the next slide."])

stack_slide("Two answers to one question",
            "The same tenant, asked of the reflectors' table and then of the routers' own tables — and the difference is the point.",
            ["deck-lg-filter-session", "deck-lg-filter-api"],
            ["How it was read: session — tenant-a as the reflectors handed it over",
             "How it was read: router API — tenant-a as the routers themselves installed it"],
            ["The core's table is one path per prefix per originating PE. A router's own tables are what survived import into the VRF and what "
             "selection actually installed in its RIB — different counts, legitimately, and the gap between them is where import and selection "
             "problems live.",
             "A looking glass that silently merged the two, or showed one and called it the truth, would be one you could not trust. So the "
             "transport is a filter here, a column on every row, and a pill beside every view — everywhere in the product."])

s, x, ih = shot_slide("The path, router by router", "For one prefix from a chosen vantage point: the map with the path lit up and its hops numbered, then a card per hop.",
                      "deck-lg-path", "dc1-h1 → 172.20.3.0/24: the CE's eBGP hand-off, pe1's import and SRv6 encapsulation, p2, pe3's End.DT46 decapsulation, ce3, the host", img_w=8.4, img_h=4.6)
side_note(s, x, h=ih, title="Two sources, and the card says which", items=[
    "The route and its attributes come from the session; what the router will really do with the packet comes from its own RIB.",
    "Equal-cost paths are named rather than silently collapsed into one line.",
    "A steered prefix is drawn along the segment list its policy installed — the uSID carrier is unpacked back into the routers it names.",
    "Ask the same prefix from a different router and the diagram is rebuilt from that router's view."])

s, x, ih = shot_slide("One prefix, every view at once", "The core's VPN table, each reflector's and PE's own VPN table, each PE's and CE's VRF table, and every router's RIB — in one page.",
                      "deck-lg-prefix-views", "Each with its full attribute set and a pill saying how it was read", img_w=8.3, img_h=4.5)
side_note(s, x, h=ih, title="Where the answers differ is the diagnosis", items=[
    "In the core but missing from a VRF: an import problem — a route target that does not match.",
    "In the VRF but not in the RIB: a selection problem — something else won.",
    "Attributes are shown whole: route targets, originator-id, cluster list, SID, transposed SID, label, and the selection reason the router gave."])

s, x, ih = shot_slide("Everything that has happened to it", "Each collection is reconciled against what is stored, so the history is a log of events rather than a pile of snapshots.",
                      "deck-lg-history", "168 events for this one prefix, each with the fields that changed", img_w=8.3, img_h=4.5)
side_note(s, x, h=ih, title="Reconciled, not sampled", items=[
    "A path that is new raises an announce; one whose attributes differ raises a change carrying the difference — local_pref: 100 → 200; one that has gone raises a withdraw.",
    "So the storage is proportional to what actually changed, not to how often it was polled.",
    "Thirty days are kept and pruned hourly, and the database survives a restart of the collector or of the VM."])

s, x, ih = shot_slide("And the slider replays it", "Drag it and every table on the page re-renders as it stood at that moment — and so does the path diagram.",
                      "deck-lg-timetravel", "“As it stood 31 minutes ago”: 33 paths in 12 views, with 168 of 168 recorded changes having happened", img_w=8.3, img_h=4.6)
side_note(s, x, h=ih, title="Why the path matters here", items=[
    "The forwarding state comes out of the RIB history, not out of today's routers — so a policy that steered this prefix an hour ago is still drawn the way it steered it.",
    "◀ and ▶ snap to the moment just after each recorded change, so nothing is missed between two drags of a slider.",
    "GET /api/state?at=… and GET /api/path?…&at=… are the same replay, for anything that wants it."])

s, x, ih = shot_slide("“When did this prefix go away?”", "The change log for the whole core, newest first, filterable by prefix, by kind and by age.",
                      "deck-lg-withdraws", "Withdraws only — the question a looking glass without a memory cannot answer at all", img_w=8.3, img_h=4.5)
side_note(s, x, h=ih, title="The operational value", items=[
    "A prefix that is missing now tells you nothing about when it went, or which PE stopped originating it. This does.",
    "The same log answers “what changed in the last hour?” across the whole core, which is the churn number on the front page.",
    "Every event carries its view and its transport, so a withdraw seen only by one router is distinguishable from one the core agreed on."])

s, x, ih = shot_slide("And the classic button: a command on a router", "Only show …, ping and traceroute, and only on the lab's own devices — over the same HTTPS API the collector polls with.",
                      "deck-lg-query", "pe1, asked for the route the collector's session carries — route target, originator, cluster list, remote SID and its structure", img_w=8.4, img_h=4.4)
side_note(s, x, h=ih, title="Narrow on purpose", items=[
    "No extra access path exists for the button: it is the router API already rendered into every configuration, with the same key and the same allow-client.",
    "Three verbs, validated server-side, against the lab's own device list — a looking glass is a read-only instrument and this keeps it one.",
    "And it is the tie-breaker: when the stored answer and the live box disagree, this says which one is stale."])

s, x, ih = shot_slide("Everything the page does is the API", "The page is one consumer of it; Prometheus is another; anything you write is a third.",
                      "deck-lg-api", "GET /api/peers, as the page itself asks for it", img_w=8.4, img_h=4.5)
side_note(s, x, h=ih, title="The read API", items=[
    "/api/prefixes and /api/prefix — the table, and everything known about one prefix in every view.",
    "/api/path — the hop-by-hop reconstruction, from a vantage point you name.",
    "/api/state?at=… and /api/path?…&at=… — the same answers, replayed to any moment in the window.",
    "/api/history, /api/routers, /api/peers and /api/query — and no write verb anywhere in it."])

wide_slide("…and the numbers go to Prometheus", "Paths and prefixes per node, source, transport, family, VRF and table — plus churn, session state and collection health.",
           "deck-lg-metrics", "GET /metrics, scraped by the shared lab monitoring on the NMS",
           [("One row per view", "The same breakdown the page shows, as a time series — so “when did pe3 stop holding "
                                 "tenant-a?” has a graph as well as a change log."),
            ("A dashboard row", "The shared Grafana has a looking-glass row built from these: paths over time, per transport, and the "
                                "age of each router's last collection."),
            ("And three alerts", "A reflector session down, a collection failing, and churn above its threshold — beside every other "
                                 "alert for the host.")], img_h=2.5)

columns_slide("What it is for", "Three properties, and every one of them is a choice that shows up in the product.",
              [("The attributes, unaltered", "A reflector client sees the update the PE originated: route distinguisher, route targets, "
                                             "SRv6 SID, label, originator-id, cluster list, AS path. Nothing is parsed out of a screen, "
                                             "and extended-nexthop keeps the originating PE as the next hop."),
               ("Every answer, labelled", "The core's table, each router's own tables and each router's RIB, side by side — with the "
                                          "transport each came over, because the same prefix seen two ways is two answers and hiding "
                                          "which is which makes both useless."),
               ("And yesterday's answer too", "Reconciled history rather than samples: thirty days of announces, changes and withdraws, "
                                              "with every table and the path itself re-drawn for any moment in that window.")],
              y=2.35, h=3.0,
              foot="http://192.168.50.231:8092 · the lab-side address is http://10.3.0.70:8080 · ./lab.sh lg status · the code is lg/ in this repository.\n"
                   "A 3½-minute walkthrough of the same ground is docs/demo/lg-demo.mp4; the portal and the core are in docs/srv6-workflows.pptx.")


if __name__ == "__main__":
    prev = d.save_preview(HERE / "looking-glass-preview.html", "The BGP looking glass — deck preview")
    print(f"preview: {prev}  ({len(d.slides)} slides)")
    if "--preview" not in sys.argv:
        f = d.save_pptx(OUT)
        print(f"deck: {f} ({f.stat().st_size / 1e6:.1f} MB, {len(d.slides)} slides)")
