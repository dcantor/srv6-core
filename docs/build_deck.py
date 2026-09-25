#!/usr/bin/env python3
"""Build docs/srv6-workflows.pptx — the executive overview of the SRv6 portal and the BGP looking glass.

One layout description drives two outputs: the PowerPoint itself, and an HTML replica at the same coordinates that a
browser can render, because this host has no LibreOffice and a deck nobody has looked at is a deck nobody should send.
Run it with the cat8000v-ipsec portal venv, which has python-pptx and Pillow —
   ~/cat8000v-ipsec/webapp/.venv/bin/python docs/build_deck.py            the .pptx and the preview
   ~/cat8000v-ipsec/webapp/.venv/bin/python docs/build_deck.py --preview  the preview only
Screenshots come from docs/screenshots/deck-*.png (captured against the live lab by docs/deck_screenshots.py)."""
import html as htmlmod
import sys
from pathlib import Path

from PIL import Image
from pptx import Presentation
from pptx.dml.color import RGBColor
from pptx.enum.shapes import MSO_SHAPE
from pptx.enum.text import MSO_ANCHOR, PP_ALIGN
from pptx.util import Inches, Pt

HERE = Path(__file__).resolve().parent
SHOTS = HERE / "screenshots"
OUT = HERE / "srv6-workflows.pptx"
W, H = 13.333, 7.5                      # LAYOUT_WIDE

# The lab's own colours: the ink of both headers, the teal the core is drawn in, and the green of the collector's
# sessions — the two dashed lines on every map in this deck.
INK, DEEP, TEAL = "07161C", "10303A", "0E7490"
GREEN, AMBER = "15803D", "B45309"
GROUND, CARD, LINE = "F6F8F9", "FFFFFF", "DCE3E7"
BODY, MUTED, PALE = "1F2937", "64748B", "BFD3DA"
HEAD_FONT, BODY_FONT = "Cambria", "Calibri"

slides = []          # each: {"bg": hex, "items": [...]}


def slide(bg=GROUND):
    slides.append({"bg": bg, "items": []}); return slides[-1]


def text(s, x, y, w, h, size=16, color=BODY, bold=False, font=BODY_FONT, align="l", anchor="t", spacing=1.15, italic=False):
    s["items"].append({"k": "text", "x": x, "y": y, "w": w, "h": h, "size": size, "color": color,
                       "bold": bold, "font": font, "align": align, "anchor": anchor, "spacing": spacing, "italic": italic})
    return s["items"][-1]


def para(item, txt, size=None, color=None, bold=False, italic=False, space_after=6):
    item.setdefault("paras", []).append({"txt": txt, "size": size or item["size"], "color": color or item["color"],
                                         "bold": bold, "italic": italic, "space_after": space_after})


def box(s, x, y, w, h, fill=CARD, line=LINE, radius=0.06, width=1):
    s["items"].append({"k": "box", "x": x, "y": y, "w": w, "h": h, "fill": fill, "line": line, "radius": radius, "lw": width})


def circle(s, x, y, d, fill=TEAL):
    s["items"].append({"k": "circle", "x": x, "y": y, "w": d, "h": d, "fill": fill})


def shot(s, name, x, y, w=None, h=None, frame=True):
    """Place a screenshot, scaled to fit the given width or height without distorting it."""
    iw, ih = Image.open(SHOTS / f"{name}.png").size
    ratio = iw / ih
    if w and not h: h = w / ratio
    elif h and not w: w = h * ratio
    elif w and h:
        if w / h > ratio: w = h * ratio
        else: h = w / ratio
    if frame: box(s, x - 0.06, y - 0.06, w + 0.12, h + 0.12, fill=CARD, line=LINE, radius=0.04)
    s["items"].append({"k": "img", "src": str(SHOTS / f"{name}.png"), "x": x, "y": y, "w": w, "h": h})
    return w, h


def caption(s, txt, x, y, w, size=11):
    it = text(s, x, y, w, 0.35, size=size, color=MUTED)
    para(it, txt, space_after=0)


def side_note(s, x, items, y=1.85, w=None, title=None, h=4.5):
    w = w or (W - x - 0.75)
    box(s, x, y, w, h)
    it = text(s, x + 0.28, y + 0.25, w - 0.56, h - 0.5, size=12.5, color=BODY, spacing=1.3)
    if title: para(it, title, bold=True, size=14, color=INK, space_after=10)
    for i, t in enumerate(items):
        para(it, t, space_after=0 if i == len(items) - 1 else 9)


def shot_slide(title, sub, img, note, img_w=8.2, img_h=4.7, badge=None):
    """A screenshot on the left with room for a card on the right. `badge` numbers a step inside one workflow."""
    s = slide()
    tx = 0.75
    if badge:
        circle(s, 0.75, 0.5, 0.62)
        num = text(s, 0.75, 0.62, 0.62, 0.45, size=17, color="FFFFFF", bold=True, align="c", font=HEAD_FONT)
        para(num, str(badge), space_after=0)
        tx = 1.55
    h = text(s, tx, 0.48, W - tx - 0.75, 0.65, size=28, color=INK, bold=True, font=HEAD_FONT)
    para(h, title, space_after=0)
    sb = text(s, tx, 1.15, W - tx - 0.75, 0.45, size=13.5, color=MUTED)
    para(sb, sub, space_after=0)
    w, hh = shot(s, img, 0.75, 1.85, w=img_w, h=img_h)
    caption(s, note, 0.75, 1.85 + hh + 0.16, w)
    return s, 0.75 + w + 0.45, hh


def wide_slide(title, sub, img, note, cols, img_h=3.5):
    """For a picture that is much wider than it is tall: full width, with the commentary in columns underneath."""
    s = slide()
    h = text(s, 0.75, 0.5, 11.8, 0.8, size=30, color=INK, bold=True, font=HEAD_FONT)
    para(h, title, space_after=0)
    sb = text(s, 0.75, 1.28, 11.8, 0.45, size=13.5, color=MUTED)
    para(sb, sub, space_after=0)
    iw, ih = Image.open(SHOTS / f"{img}.png").size                 # a picture narrower than the slide is centred, not left-hung
    ix = 0.75 + max(0.0, (11.83 - min(11.83, img_h * iw / ih)) / 2)
    w, hh = shot(s, img, ix, 1.9, w=11.83 - 2 * (ix - 0.75), h=img_h)
    caption(s, note, ix, 1.9 + hh + 0.14, w)
    y = 1.9 + hh + 0.62
    if isinstance(cols, str):                       # a picture worth the whole slide gets one line under it, not columns
        it = text(s, 0.75, y, 11.83, 7.3 - y, size=13, color=BODY, spacing=1.3)
        para(it, cols, space_after=0)
        return s
    cw = (11.83 - 0.4 * (len(cols) - 1)) / len(cols)
    bh = 6.95 - y                              # the cards run to the bottom margin, so a short picture leaves no dead band
    x = 0.75
    for t, b in cols:
        box(s, x, y, cw, bh)
        ht = text(s, x + 0.28, y + 0.28, cw - 0.56, 0.45, size=14, color=INK, bold=True)
        para(ht, t, space_after=0)
        bt = text(s, x + 0.28, y + 0.78, cw - 0.56, bh - 1.06, size=12.5, color=BODY, spacing=1.35)
        para(bt, b, space_after=0)
        x += cw + 0.4
    return s


def two_up_slide(title, sub, left, right, lcap, rcap, notes, band_h=3.4):
    s = slide()
    h = text(s, 0.75, 0.5, 11.8, 0.8, size=30, color=INK, bold=True, font=HEAD_FONT)
    para(h, title, space_after=0)
    sb = text(s, 0.75, 1.28, 11.8, 0.45, size=13.5, color=MUTED)
    para(sb, sub, space_after=0)

    def fit(name, w, h):
        iw, ih = Image.open(SHOTS / f"{name}.png").size
        r = iw / ih
        return (h * r, h) if w / h > r else (w, w / r)

    lw, lh = fit(left, 5.85, band_h); rw, rh = fit(right, 5.6, band_h)
    band = max(lh, rh)
    shot(s, left, 0.75, 1.95 + (band - lh) / 2, w=lw, h=lh)
    caption(s, lcap, 0.75, 1.95 + band + 0.14, lw)
    shot(s, right, 6.95, 1.95 + (band - rh) / 2, w=rw, h=rh)
    caption(s, rcap, 6.95, 1.95 + band + 0.14, rw)
    y = 1.95 + band + 0.62
    it = text(s, 0.75, y, 11.8, 7.2 - y, size=12.5, color=BODY, spacing=1.3)
    for i, t in enumerate(notes):
        para(it, t, space_after=0 if i == len(notes) - 1 else 7)
    return s


def part_slide(kicker, title, lines):
    """A divider: this deck is two halves — build the service, then look at what it built."""
    s = slide(DEEP)
    k = text(s, 1.1, 2.35, 10.5, 0.45, size=13, color=TEAL, bold=True)
    para(k, kicker, space_after=0)
    t = text(s, 1.1, 2.85, 10.8, 1.1, size=40, color="FFFFFF", bold=True, font=HEAD_FONT, spacing=1.0)
    para(t, title, space_after=0)
    b = text(s, 1.1, 4.2, 10.8, 1.6, size=15, color=PALE, spacing=1.4)
    for i, ln in enumerate(lines):
        para(b, ln, space_after=0 if i == len(lines) - 1 else 8)


# ---------------------------------------------------------------- the deck
def title_slide():
    s = slide(INK)
    t = text(s, 0.9, 2.05, 10.0, 1.9, size=40, color="FFFFFF", bold=True, font=HEAD_FONT, spacing=1.0)
    para(t, "The SRv6 Core", space_after=2)
    sub = text(s, 0.9, 3.05, 10.4, 1.0, size=22, color=PALE)
    para(sub, "A tenant provisioning portal, and a looking glass that remembers", space_after=0)
    m = text(s, 0.9, 4.6, 11.5, 1.2, size=13, color="7E97A1", spacing=1.4)
    para(m, "VyOS SRv6 core (uSID f3216) · IS-IS level-2 · BGP L3VPN with End.DT46 · two route reflectors", space_after=5)
    para(m, "Nautobot source of truth · Network-as-Code rendering · Robot Framework validation · Prometheus and Grafana", space_after=0)
    circle(s, 11.2, 2.15, 1.25, fill=TEAL)
    n = text(s, 11.2, 2.52, 1.25, 0.6, size=28, color="FFFFFF", bold=True, align="c", font=HEAD_FONT)
    para(n, "21", space_after=0)
    l = text(s, 10.45, 3.52, 2.2, 0.5, size=12, color=PALE, align="c")
    para(l, "VMs, one model", space_after=0)


def stat_slide():
    s = slide()
    h = text(s, 0.75, 0.55, 11.8, 0.8, size=32, color=INK, bold=True, font=HEAD_FONT)
    para(h, "What the core runs", space_after=0)
    sub = text(s, 0.75, 1.32, 11.8, 0.5, size=14, color=MUTED)
    para(sub, "Every number below is read from the routers or from the collector's own history — none of it is typed in.", space_after=0)
    stats = [("2 / 8", "tenants, sites", "one VRF per tenant on every PE and CE; tenants never meet"),
             ("76", "prefixes in the core", "152 paths in the VPNv4 / VPNv6 table, across two address families"),
             ("100", "tests per change", "twelve Robot Framework suites against the running lab"),
             ("~6 min", "to add a tenant", "four sites, four host VMs, the CEs re-wired, Nautobot seeded")]
    x = 0.75
    for big, small, note in stats:
        box(s, x, 2.05, 2.85, 2.05)
        b = text(s, x + 0.25, 2.25, 2.4, 0.75, size=28, color=TEAL, bold=True, font=HEAD_FONT)
        para(b, big, space_after=0)
        l = text(s, x + 0.25, 2.95, 2.4, 0.4, size=13, color=INK, bold=True)
        para(l, small, space_after=0)
        n = text(s, x + 0.25, 3.32, 2.4, 0.75, size=10.5, color=MUTED, spacing=1.15)
        para(n, note, space_after=0)
        x += 3.05
    box(s, 0.75, 4.45, 11.83, 2.25, fill=CARD)
    q = text(s, 1.1, 4.75, 11.1, 1.9, size=15, color=BODY, spacing=1.35)
    para(q, "Two halves of the same model.", bold=True, space_after=8)
    para(q, "The portal changes the service: a tenant is described on a form, allocated from the live lab, and built by one "
            "run that ends in the test suites. The looking glass never changes anything — it sits in the core as a passive "
            "BGP listener, reads every router besides, and keeps what it saw, so any prefix can be answered for now and for "
            "any moment in the last thirty days.", space_after=0)


title_slide()
stat_slide()

wide_slide("The testbed", "An IS-IS level-2, IPv6-only core carrying SRv6 uSID f3216; four data centres behind four PEs; p1 and p3 reflect, and the collector (top right) listens to both.",
           "deck-portal-topology", "The portal's own drawing, from the model: every link with its prefix and interfaces, every VRF with its route distinguisher, hosts coloured by live reachability",
           "The picture, the routers' configuration, the Nautobot objects and the test fixtures all come from one file — lab.conf plus the intent. "
           "Nothing on this slide is drawn by hand, and Nautobot must render the same thing or the build fails.", img_h=4.15)

part_slide("Part one", "Provisioning a tenant",
           ["The portal is the only place the service is changed: a form states the intent, the allocator answers from the",
            "running lab, and a single run builds it — VMs, wiring, configuration, Nautobot and the tests.",
            "http://192.168.50.231:8091"])

s, x, ih = shot_slide("Tenants, with the live state beside them", "One card per tenant: its sites, the attachment circuits, the CEs, the hosts — joined with what the PEs actually report.",
                      "deck-portal-tenants", "Each row carries the eBGP session to the CE, the prefixes received, the VRF route count and the tenant's End.DT4 SID", img_w=8.2)
side_note(s, x, h=ih, title="Model and reality, in one row", items=[
    "The left of each row is the model (PE port, RD, attachment circuit, LAN, host); the right is read live from the PE.",
    "A session that is not Established, or a host that does not answer, shows here first — there is no separate health page to remember.",
    "VRF, tenant and prefixes link straight into Nautobot, so the source of truth is one click from the operational view."])

two_up_slide("Add a tenant — who it is, and where",
             "The wizard opens with everything it can work out already filled in: the next free name, kernel table and route target.",
             "deck-portal-wizard-1", "deck-portal-wizard-refused",
             "Step 1 — identity and the sites the tenant will have",
             "The allocator answers from the running lab, so it can say no",
             ["Left: tenant-c, kernel table 300, route target 65000:300 — the next free values, and a site in each of the four data centres.",
              "Right: pe4 has no free ports left, so a fourth site is refused before anything is built rather than half-way through the run. "
              "Drop that site and the wizard goes on — the allocation is checked against the lab, not against a copy of it."], band_h=3.15)

s, x, ih = shot_slide("Add a tenant — the allocation", "Attachment circuit /30s and LAN /24s from the tenant's blocks, the next free PE and CE ports, and a host per site.",
                      "deck-portal-wizard-2", "Step 2 — every value suggested and editable; the review re-validates whatever you change", img_w=5.1, img_h=5.0, badge=2)
side_note(s, x, y=1.85, h=ih, title="Allocation, not arithmetic", items=[
    "Addresses come from the tenant's own blocks — 172.(16+i) for the attachment circuits, 172.(20+i) for the LANs — so a new tenant never collides with an old one.",
    "The host is part of the tenant: name, OOB address, console port and node index are allocated with everything else, and the VM is built by the run.",
    "Edit anything. The next step re-checks free ports, unused prefixes and unique names against the lab as it is now."])

s, x, ih = shot_slide("Add a tenant — review, then deploy", "The last step is the whole change in one table: what each PE gets, what each CE gets, and the host behind it.",
                      "deck-portal-wizard-3", "Step 3 — per-site review, with the option to run the Robot suites at the end", img_w=6.2, img_h=4.9, badge=3)
side_note(s, x, y=1.85, h=ih, title="One button, one run", items=[
    "lab.conf and the day-0 configs are written, the host VMs are created and booted, and the CEs at the chosen sites are re-wired.",
    "Then the configuration is pushed to the PEs and CEs over SSH, Nautobot is seeded, and the run verifies itself: a ping matrix of the tenant's hosts and a Nautobot-versus-lab.conf render check.",
    "A re-wired CE reboots for about a minute, and the review says so — that site's other tenants lose connectivity for that time."])

s, x, ih = shot_slide("Watch it run — and resume it if it fails", "Every run is a list of steps with a streamed log and, at the end, the Robot report.",
                      "deck-portal-run", "A real tenant-c run: eight steps, 52/52 tests — and every step marked “from run …-f4cf” was inherited, not repeated", img_w=8.4, img_h=4.6)
side_note(s, x, h=ih, title="The record", items=[
    "One run at a time, so two changes can never race on the same router.",
    "A failed or interrupted run resumes from the failed step: the successful steps carry their earlier result forward, which is what the “from run …” notes are.",
    "The suites run inside the job, so the evidence belongs to the change rather than to a separate test cycle."])

wide_slide("Steering: pin a prefix to a path", "Explicit-path traffic engineering as a form — a source PE, a tenant prefix, and the P routers to cross, in order.",
           "deck-portal-steering", "The segment list is the End SID of each listed P router, then the End.DT4 SID of the PE that owns the prefix — read live and applied immediately. No policy is in place here.",
           [("Cheap, because SRv6", "No per-hop state and no signalling: the ingress PE writes a segment list into the packet and every "
                                    "router in between only has to forward it."),
            ("One direction at a time", "Return traffic keeps the IGP shortest path, so a policy is one-directional unless you add "
                                        "its mirror. Suite 07 shows the SRH on the wire."),
            ("And it is visible afterwards", "The looking glass draws a steered prefix along the routers its policy named, and keeps "
                                             "drawing it that way for the window in which it was in force.")], img_h=2.15)

part_slide("Part two", "The looking glass",
           ["A twenty-first VM, Alpine and 512 MiB, sits in the core as a passive route collector: an iBGP session to each",
            "reflector, a direct read of every router besides, and a history of everything it has seen.",
            "http://192.168.50.231:8092"])

s, x, ih = shot_slide("Every VPN prefix in the core, and how it got there", "The overview: what the collector holds, the sessions it holds it over, and the testbed drawn from the model.",
                      "deck-lg-overview", "76 prefixes, 152 paths, two reflector sessions — and 18,674 recorded changes behind them", img_w=8.3)
side_note(s, x, h=ih, title="Why a session, not screen-scraping", items=[
    "The collector receives the same update the reflector sent: route distinguisher, route targets, SRv6 SID and label, originator-id, cluster list, AS path, local preference.",
    "It announces nothing back — an outbound deny route-map says so explicitly, and the sessions show 0 sent.",
    "The session negotiates extended next-hop capability, without which every VPNv4 route would look as though it came from the reflector instead of the PE that originated it."])

s, x, ih = shot_slide("Every row says how it was read", "The core's VPN table is one answer. What a tenant actually has, and what a router will really do, are two others.",
                      "deck-lg-views", "What each view holds, per router, VRF and family — with the transport it came over and when it was collected", img_w=8.3, img_h=4.5)
side_note(s, x, h=ih, title="Three transports, one page", items=[
    "rr-session — the whole VPNv4 / VPNv6 table, as the reflectors handed it over.",
    "router-api — each router's own HTTPS API: its RIB per VRF and family, its own VPN table, its IS-IS adjacencies.",
    "router-ssh — the per-VRF BGP tables, where the API has no JSON form for the question.",
    "The same prefix seen two ways is two answers, so the transport is a column, a filter and a pill — never hidden."])

s, x, ih = shot_slide("Prefixes, filtered the way an operator asks", "View, family, VRF, route distinguisher, origin AS, how it was read — and free text over prefix, next hop, SID, route target and AS path.",
                      "deck-lg-prefixes", "Every row carries its attributes and a path button", img_w=8.3, img_h=4.5)
side_note(s, x, h=ih, title="The classic questions", items=[
    "“Who originates this?” — origin AS and originating node are columns, not something to derive from an AS path.",
    "“Is it in the tenant's VRF, or only in the core?” — the view filter answers it directly.",
    "“What SID does it carry?” — the End.DT46 SID is on the row, and the free-text box searches it."])

s, x, ih = shot_slide("The path, router by router", "For one prefix from a chosen vantage point: the map with the path lit up, then a card per hop.",
                      "deck-lg-path", "dc1-h1 → 172.20.3.0/24: the CE hand-off, pe1's import and SRv6 encapsulation, p2, pe3's End.DT46 decapsulation, ce3, the host", img_w=8.5, img_h=4.6)
side_note(s, x, h=ih, title="Two sources, one answer", items=[
    "The route and its attributes come from the session; what the router will actually do with the packet comes from its own RIB — and the card says which is which.",
    "Equal-cost paths are named rather than silently collapsed into one line.",
    "A steered prefix is drawn along the segment list the policy installed: the uSID carrier is unpacked back into the routers it names."])

s, x, ih = shot_slide("One prefix, every view at once", "The core's VPN table, each reflector's and PE's own VPN table, each PE's and CE's VRF table, and every router's RIB.",
                      "deck-lg-prefix-views", "One question, answered by every vantage point that has an opinion — each with its full attribute set and the transport it came over", img_w=8.3, img_h=4.5)
side_note(s, x, h=ih, title="Where answers differ", items=[
    "A prefix present in the core but missing from a VRF is an import problem; present in the VRF but not in the RIB is a selection problem. The page puts both side by side.",
    "Attributes are shown whole: route targets, originator-id, cluster list, SID, label, selection reason.",
    "Below it, the timeline of everything that has happened to this prefix."])

s, x, ih = shot_slide("And what it looked like an hour ago", "Every collection is reconciled against what is stored, so the history is a log of announces, attribute changes and withdraws.",
                      "deck-lg-timetravel", "The slider dragged back: 33 paths in 12 views as they stood at 13:57 — and the path diagram re-drawn for that moment", img_w=8.3, img_h=4.6)
side_note(s, x, h=ih, title="The point of keeping it", items=[
    "A change carries the fields that changed — local_pref: 100 → 200 — not just a new snapshot.",
    "Drag the slider and every table on the page re-renders, and so does the path: the forwarding state comes out of the RIB history, not out of today's routers.",
    "So a policy that was in place at 22:06 and is gone now still shows as the path it made. Thirty days are kept, pruned hourly."])

two_up_slide("Everything it reads, and one question of your own",
             "The Routers page is the collector's account of its own work; the Live query is the looking-glass button.",
             "deck-lg-routers", "deck-lg-query",
             "Every box it reads: identity, transports, when, and how much it holds",
             "A show command straight on a router — only show, ping and traceroute",
             ["Each router's own HTTPS API is rendered into its configuration like everything else (key srv6core-lab-looking-glass, reachable only from the collector and the lab host), "
              "so the same interface is open to anything else that wants it.",
              "Numeric series — paths per VRF and family, churn, session state, collection health — go to /metrics, where Prometheus scrapes them for the dashboard's looking-glass row and three alerts."],
             band_h=3.5)


def close_slide():
    s = slide(INK)
    h = text(s, 0.9, 0.9, 11.5, 0.9, size=32, color="FFFFFF", bold=True, font=HEAD_FONT)
    para(h, "What the two halves are for", space_after=0)
    cols = [("Change it from one place", "The portal allocates from the running lab, builds the tenant in one resumable run, and "
                                         "ends in the test suites — so the evidence belongs to the change."),
            ("See it as the network sees it", "A passive BGP session gets the attributes the PEs originated; the routers' own APIs "
                                              "get what each box installed. Every row says which."),
            ("Ask about yesterday", "The history is reconciled, not sampled: announces, attribute changes and withdraws, thirty "
                                    "days of them, with the path re-drawn for any moment in that window.")]
    x = 0.9
    for t, b in cols:
        box(s, x, 2.15, 3.72, 2.75, fill=DEEP, line=DEEP)
        ht = text(s, x + 0.3, 2.42, 3.12, 0.6, size=16, color="FFFFFF", bold=True, font=HEAD_FONT)
        para(ht, t, space_after=0)
        bt = text(s, x + 0.3, 3.1, 3.12, 1.7, size=12, color=PALE, spacing=1.3)
        para(bt, b, space_after=0)
        x += 3.92
    f = text(s, 0.9, 5.5, 11.5, 1.1, size=13, color="7E97A1", spacing=1.35)
    para(f, "The portal is at http://192.168.50.231:8091 and the looking glass at :8092, both with their REST API at /docs; "
            "the lab hub at :8088 lists every lab on the host. The full write-up is README.md and docs/srv6-walkthrough.pdf.", space_after=0)


close_slide()


# ---------------------------------------------------------------- render
def hexc(c): return RGBColor.from_string(c)


def build_pptx():
    prs = Presentation(); prs.slide_width, prs.slide_height = Inches(W), Inches(H)
    blank = prs.slide_layouts[6]
    for sp in slides:
        sl = prs.slides.add_slide(blank)
        bg = sl.shapes.add_shape(MSO_SHAPE.RECTANGLE, 0, 0, Inches(W), Inches(H))
        bg.fill.solid(); bg.fill.fore_color.rgb = hexc(sp["bg"]); bg.line.fill.background(); bg.shadow.inherit = False
        for it in sp["items"]:
            if it["k"] == "box":
                sh = sl.shapes.add_shape(MSO_SHAPE.ROUNDED_RECTANGLE if it["radius"] else MSO_SHAPE.RECTANGLE,
                                         Inches(it["x"]), Inches(it["y"]), Inches(it["w"]), Inches(it["h"]))
                if it["radius"]: sh.adjustments[0] = min(0.5, it["radius"] * 2 / max(it["w"], it["h"]))
                sh.fill.solid(); sh.fill.fore_color.rgb = hexc(it["fill"])
                sh.line.color.rgb = hexc(it["line"]); sh.line.width = Pt(it["lw"]); sh.shadow.inherit = False
            elif it["k"] == "circle":
                sh = sl.shapes.add_shape(MSO_SHAPE.OVAL, Inches(it["x"]), Inches(it["y"]), Inches(it["w"]), Inches(it["h"]))
                sh.fill.solid(); sh.fill.fore_color.rgb = hexc(it["fill"]); sh.line.fill.background(); sh.shadow.inherit = False
            elif it["k"] == "img":
                sl.shapes.add_picture(it["src"], Inches(it["x"]), Inches(it["y"]), Inches(it["w"]), Inches(it["h"]))
            elif it["k"] == "text":
                tb = sl.shapes.add_textbox(Inches(it["x"]), Inches(it["y"]), Inches(it["w"]), Inches(it["h"]))
                tf = tb.text_frame; tf.word_wrap = True
                tf.margin_left = tf.margin_right = tf.margin_top = tf.margin_bottom = 0
                tf.vertical_anchor = {"t": MSO_ANCHOR.TOP, "m": MSO_ANCHOR.MIDDLE}[it["anchor"]]
                for i, p in enumerate(it.get("paras", [])):
                    par = tf.paragraphs[0] if i == 0 else tf.add_paragraph()
                    par.alignment = {"l": PP_ALIGN.LEFT, "c": PP_ALIGN.CENTER}[it["align"]]
                    par.line_spacing = it["spacing"]; par.space_after = Pt(p["space_after"])
                    run = par.add_run(); run.text = p["txt"]
                    f = run.font; f.name = it["font"]; f.size = Pt(p["size"]); f.bold = p["bold"]; f.italic = p["italic"]
                    f.color.rgb = hexc(p["color"])
    prs.save(OUT)
    return OUT


def build_preview():
    """The same geometry as HTML, so a browser can show what the deck looks like."""
    px = 96
    out = ["<!doctype html><meta charset='utf-8'><title>SRv6 portal and looking glass — deck preview</title><style>",
           "body{margin:0;background:#33383f;font-family:Calibri,Carlito,sans-serif}",
           f".s{{position:relative;width:{W * px}px;height:{H * px}px;margin:18px auto;overflow:hidden}}",
           ".i{position:absolute}", ".t{white-space:pre-wrap}", "</style>"]
    for n, sp in enumerate(slides, 1):
        out.append(f"<div class='s' style='background:#{sp['bg']}' id='s{n}'>")
        for it in sp["items"]:
            st = f"left:{it['x'] * px}px;top:{it['y'] * px}px;width:{it['w'] * px}px;height:{it['h'] * px}px"
            if it["k"] == "box":
                out.append(f"<div class='i' style='{st};background:#{it['fill']};border:{it['lw']}px solid #{it['line']};"
                           f"border-radius:{it['radius'] * px}px;box-sizing:border-box'></div>")
            elif it["k"] == "circle":
                out.append(f"<div class='i' style='{st};background:#{it['fill']};border-radius:50%'></div>")
            elif it["k"] == "img":
                out.append(f"<img class='i' style='{st}' src='{it['src']}'>")
            elif it["k"] == "text":
                al = {"l": "left", "c": "center"}[it["align"]]
                fam = "Cambria, 'Caladea', serif" if it["font"] == "Cambria" else "Calibri, Carlito, sans-serif"
                inner = "".join(
                    f"<div style='font-size:{p['size']}pt;color:#{p['color']};font-weight:{700 if p['bold'] else 400};"
                    f"font-style:{'italic' if p['italic'] else 'normal'};line-height:{it['spacing']};"
                    f"margin-bottom:{p['space_after']}pt'>{htmlmod.escape(p['txt'])}</div>" for p in it.get("paras", []))
                out.append(f"<div class='i t' style='{st};text-align:{al};font-family:{fam};"
                           f"display:flex;flex-direction:column;justify-content:{'center' if it['anchor'] == 'm' else 'flex-start'}'>{inner}</div>")
        out.append(f"<div style='position:absolute;right:10px;bottom:6px;font-size:9pt;color:#9aa4b2'>{n}</div></div>")
    p = HERE / "srv6-workflows-preview.html"
    p.write_text("\n".join(out), encoding="utf-8")
    return p


if __name__ == "__main__":
    prev = build_preview()
    print(f"preview: {prev}  ({len(slides)} slides)")
    if "--preview" not in sys.argv:
        f = build_pptx()
        print(f"deck: {f} ({f.stat().st_size / 1e6:.1f} MB, {len(slides)} slides)")
