"""Draw the lab topology as an SVG from an inventory dict (`lab.sh inventory` shape): the P triangle in a core lane,
one swim lane per data centre with the PE, the CE and one host per tenant, every link labelled with its prefix and both
interface names. Used by docs/topology.py (PDF/PNG) and the portal (live topology). Any number of tenants / hosts."""
import html

TENANT_COLORS = ["#475569", "#7c3aed", "#0891b2", "#b45309", "#be185d", "#15803d", "#4338ca", "#a16207"]
DC_COLORS = ["#eff6ff", "#fdf4ff", "#f0fdf4", "#fefce8", "#fff1f2", "#f0f9ff"]
FILL = {"p": ("#fde7d6", "#c2410c"), "pe": ("#fee2e2", "#b91c1c"), "ce": ("#dbeafe", "#1d4ed8"), "host": ("#dcfce7", "#15803d")}


def draw(inv, live=None):
    """SVG string. `live` (optional): {host name: {"reachable": bool}} / {tenant: colour overrides} for the portal."""
    N = {n["name"]: n for n in inv["nodes"]}; S = inv["service"]
    tenants = sorted(S["tenants"], key=lambda t: S["tenants"][t]["table"]); tcolor = {t: TENANT_COLORS[i % len(TENANT_COLORS)] for i, t in enumerate(tenants)}
    dcs = sorted({n["dc"] for n in inv["nodes"] if n["dc"] != "core"})
    hosts_of = {dc: sorted((n for n in inv["nodes"] if n["role"] == "host" and n["dc"] == dc), key=lambda h: tenants.index(next(p["tenant"] for p in h["ports"] if p["peer"])) if any(p["peer"] for p in h["ports"]) else 99) for dc in dcs}
    HW, HGAP = 150, 14; lane_w = {dc: max(320, len(hosts_of[dc]) * (HW + HGAP) + 40) for dc in dcs}
    GAP = 24; x = 30; DCX = {}
    for dc in dcs: DCX[dc] = x + lane_w[dc] / 2; x += lane_w[dc] + GAP
    W = max(x + 6, 1200); ROWS = {"p": 175, "pe": 380, "ce": 560, "host": 720}; H = 790
    BOX = {"p": (240, 74), "pe": (270, 62 + 14 * len(tenants)), "ce": (min(280, min(lane_w.values()) - 30), 48 + 14 * len(tenants)), "host": (HW, 56)}
    ps = sorted(n["name"] for n in inv["nodes"] if n["role"] == "p")
    core_left, core_right = 150, W - 150; PX = {p: core_left + (core_right - core_left) * (i + 0.5) / len(ps) for i, p in enumerate(ps)}
    COLS = {**{n["name"]: DCX[n["dc"]] for n in inv["nodes"] if n["role"] in ("pe", "ce")}, **PX}
    for dc in dcs:
        k = len(hosts_of[dc])
        for i, h in enumerate(hosts_of[dc]): COLS[h["name"]] = DCX[dc] + (i - (k - 1) / 2) * (HW + HGAP)

    def center(n): return COLS[n], ROWS[N[n]["role"]]
    def edge(n, towards_y, dx=0):
        cx, cy = center(n); h = BOX[N[n]["role"]][1] / 2; return cx + dx, (cy - h if towards_y < cy else cy + h)

    out = [f'<svg viewBox="0 0 {W:.0f} {H}" xmlns="http://www.w3.org/2000/svg" font-family="system-ui, sans-serif">',
           '<style>.name{font-weight:600;font-size:14px}.role{font-size:11px;fill:#475569}.sub{font-size:10.5px;font-family:ui-monospace,Menlo,monospace;fill:#334155}'
           '.lane{font-weight:600;font-size:12px;fill:#64748b;letter-spacing:.04em}line.core,path.core{stroke:#c2410c;stroke-width:2;fill:none}line.access{stroke-width:1.6}'
           '.lbl{font-size:10.5px;font-family:ui-monospace,Menlo,monospace;fill:#334155}.lbl.core{fill:#9a3412}.port{font-size:9.5px;font-family:ui-monospace,Menlo,monospace;fill:#64748b}</style>']
    for i, dc in enumerate(dcs):
        out.append(f'<rect x="{DCX[dc] - lane_w[dc] / 2:.0f}" y="{ROWS["pe"] - 58}" width="{lane_w[dc]:.0f}" height="{ROWS["host"] - ROWS["pe"] + 104}" rx="14" fill="{DC_COLORS[i % len(DC_COLORS)]}" stroke="#cbd5e1"/>'
                   f'<text x="{DCX[dc] + lane_w[dc] / 2 - 12:.0f}" y="{ROWS["host"] + 38}" text-anchor="end" class="lane">{dc}</text>')
    out.append(f'<rect x="{core_left - 20}" y="{ROWS["p"] - 120}" width="{core_right - core_left + 40:.0f}" height="180" rx="14" fill="#fff7ed" stroke="#fdba74"/>'
               f'<text x="{core_left - 5}" y="{ROWS["p"] - 100}" class="lane">core — IS-IS level-2, IPv6-only, MTU 9000, SRv6 block fd00:c::/40 · reflectors {", ".join(S["rrs"])}</text>')
    pe_ac_slots = {}   # per PE: how many attachment circuits drawn so far, to spread them under the box
    for l in inv["links"]:
        a, b = l["a"], l["b"]; ra, rb = N[a]["role"], N[b]["role"]
        if ra == rb == "p":
            (ax, ay), (bx, by) = center(a), center(b); i, j = ps.index(a), ps.index(b)
            if abs(i - j) > 1:   # not adjacent in the row: arch over the top
                out.append(f'<path d="M{ax},{ay - 37} C{ax + 120},{ay - 120} {bx - 120},{by - 120} {bx},{by - 37}" class="core"/>'
                           f'<text x="{(ax + bx) / 2}" y="{ay - 90}" text-anchor="middle" class="lbl core">{l["a_port"]} · {l["prefix"]} · {l["b_port"]}</text>')
            else:
                out.append(f'<line x1="{ax + 120}" y1="{ay}" x2="{bx - 120}" y2="{by}" class="core"/><text x="{(ax + bx) / 2}" y="{ay - 8}" text-anchor="middle" class="lbl core">{l["prefix"]}</text>'
                           f'<text x="{(ax + bx) / 2}" y="{ay + 16}" text-anchor="middle" class="port">{l["a_port"]} · {l["b_port"]}</text>')
            continue
        if rb == "pe":   # P -> PE
            (ax, ay), (bx, by) = center(a), center(b); ax, ay = edge(a, by); bx, by = edge(b, ay)
            out.append(f'<line x1="{ax}" y1="{ay}" x2="{bx}" y2="{by}" class="core"/>')
            import math
            at = lambda t: (ax + (bx - ax) * t, ay + (by - ay) * t); ang = math.degrees(math.atan2(by - ay, bx - ax)); ang = ang - 180 if ang > 90 else ang
            lx, ly = at(0.3 if abs(bx - ax) > 250 else 0.62)
            out.append(f'<text x="{lx}" y="{ly - 5}" text-anchor="middle" class="lbl core" transform="rotate({ang:.0f} {lx} {ly})">{l["prefix"]}</text>')
            px, py = at(0.1); qx, qy = at(0.9)
            out.append(f'<text x="{px + (7 if bx >= ax else -7)}" y="{py + 4}" class="port" text-anchor="{"start" if bx >= ax else "end"}">{l["a_port"]}</text>'
                       f'<text x="{qx + (7 if ax >= bx else -7)}" y="{qy + 4}" class="port" text-anchor="{"start" if ax >= bx else "end"}">{l["b_port"]}</text>')
            continue
        col = tcolor.get(l.get("tenant"), "#64748b")
        if ra == "pe" and rb == "ce":   # attachment circuits fan out under the PE
            k = len(tenants); i = tenants.index(l["tenant"]); off = (i - (k - 1) / 2) * (BOX["pe"][0] / (k + 0.5))
            ax, ay = edge(a, ROWS["ce"], off); bx, by = edge(b, ROWS["pe"], off)
        else:                            # CE -> host: straight down to the host's column
            (hx, _) = center(b); ax, ay = edge(a, ROWS["host"], hx - COLS[a]); bx, by = edge(b, ROWS["ce"])
        out.append(f'<line x1="{ax:.0f}" y1="{ay}" x2="{bx:.0f}" y2="{by}" class="access" style="stroke:{col}"/>')
        mx, my = (ax + bx) / 2, (ay + by) / 2; anc = "start"; dx = 6
        out.append(f'<text x="{mx + dx:.0f}" y="{my + 4}" class="lbl" text-anchor="{anc}" style="fill:{col}">{l["prefix"]}</text>'
                   f'<text x="{ax + dx:.0f}" y="{ay + 13}" class="port" text-anchor="{anc}">{l["a_port"]} .{l["a_ip"].split("/")[0].split(".")[-1]}</text>'
                   f'<text x="{bx + dx:.0f}" y="{by - 5}" class="port" text-anchor="{anc}">{l["b_port"]} .{l["b_ip"].split("/")[0].split(".")[-1]}</text>')
    for n in inv["nodes"]:
        x0, y0 = center(n["name"]); w, h = BOX[n["role"]]; fill, stroke = FILL[n["role"]]
        lans = {p["tenant"]: p["prefix"] for p in n["ports"] if p["peer"] and N[p["peer"]]["role"] == "host"}
        if n["role"] == "p": lines = [f'{n["loopback6"]} · rid {n["router_id"]}', f'locator {n["locator"]}', "VPNv4 route reflector · AS 65000" if n["name"] in S["rrs"] else "IPv6 forwarding only, no BGP / VRF"]
        elif n["role"] == "pe": lines = [f'{n["loopback6"]} · rid {n["router_id"]} · AS {n["asn"]}', f'locator {n["locator"]}'] + [f'VRF {t} · RD {n["rd"].get(t, "?")} · End.DT4' for t in tenants if t in n["rd"]]
        elif n["role"] == "ce": lines = [f'AS {n["asn"]} · eBGP → {n["pe"]} per tenant'] + [f'VRF {t}: {lans[t]}' for t in tenants if t in lans]
        else:
            t = next((p["tenant"] for p in n["ports"] if p["peer"]), None); lines = [f'{n["ports"][0]["ip"]} · gw .1', t or "unwired"]
            if live and n["name"] in live: fill = "#dcfce7" if live[n["name"]].get("reachable") else "#fee2e2"; stroke = "#15803d" if live[n["name"]].get("reachable") else "#b91c1c"
        role = {"p": "P" + (" / RR" if n["name"] in S["rrs"] else ""), "pe": "PE", "ce": "CE", "host": "host"}[n["role"]]
        out.append(f'<g><rect x="{x0 - w / 2:.0f}" y="{y0 - h / 2:.0f}" width="{w:.0f}" height="{h:.0f}" rx="9" fill="{fill}" stroke="{stroke}" stroke-width="1.6"/>'
                   f'<text x="{x0 - w / 2 + 10:.0f}" y="{y0 - h / 2 + 19:.0f}" class="name">{n["name"]}</text><text x="{x0 + w / 2 - 10:.0f}" y="{y0 - h / 2 + 19:.0f}" text-anchor="end" class="role">{role} · {n["mgmt_ip"]}</text>'
                   + "".join(f'<text x="{x0 - w / 2 + 10:.0f}" y="{y0 - h / 2 + 19 + 14 * (i + 1):.0f}" class="sub">{html.escape(t)}</text>' for i, t in enumerate(lines)) + "</g>")
    out.append("</svg>"); return "".join(out), tcolor
