#!/usr/bin/env python3
"""Draw the lab topology from `lab.sh inventory` as an SVG inside an HTML page, and print it to docs/topology.pdf
(Chrome via Playwright). Re-run after changing lab.conf.   docs/topology.py [--html-only]"""
import html, ipaddress, json, subprocess, sys
from pathlib import Path

LAB_DIR = Path(__file__).resolve().parents[1]; OUT = LAB_DIR / "docs"
inv = json.loads(subprocess.run([str(LAB_DIR / "lab.sh"), "inventory"], capture_output=True, text=True, check=True).stdout)
N = {n["name"]: n for n in inv["nodes"]}; S = inv["service"]
ROWS = {"p": 175, "pe": 370, "ce": 540, "host": 690}
DCX = {"dc1": 190, "dc2": 555, "dc3": 985, "dc4": 1350}
COLS = {**{f"pe{i}": DCX[f"dc{i}"] for i in range(1, 5)}, **{f"ce{i}": DCX[f"dc{i}"] for i in range(1, 5)},
        **{f"dc{i}-h1": DCX[f"dc{i}"] - 82 for i in range(1, 5)}, **{f"dc{i}-h2": DCX[f"dc{i}"] + 82 for i in range(1, 5)}, "p1": 370, "p2": 770, "p3": 1170}
BOX = {"p": (240, 74), "pe": (270, 92), "ce": (270, 92), "host": (156, 56)}
TENANT_COLOR = {"tenant-a": "#475569", "tenant-b": "#7c3aed"}
import math
FILL = {"p": ("#fde7d6", "#c2410c"), "pe": ("#fee2e2", "#b91c1c"), "ce": ("#dbeafe", "#1d4ed8"), "host": ("#dcfce7", "#15803d")}
DC_COLOR = {"dc1": "#eff6ff", "dc2": "#fdf4ff", "dc3": "#f0fdf4", "dc4": "#fefce8"}


def center(n): return COLS[n], ROWS[N[n]["role"]]
def edge(n, towards_y, dx=0):
    x, y = center(n); h = BOX[N[n]["role"]][1] / 2
    return x + dx, (y - h if towards_y < y else y + h)


svg = []
# data-centre swim lanes behind the access rows
for dc, x in DCX.items():
    svg.append(f'<rect x="{x-172}" y="{ROWS["pe"]-58}" width="344" height="{ROWS["host"]-ROWS["pe"]+104}" rx="14" fill="{DC_COLOR[dc]}" stroke="#cbd5e1"/>'
               f'<text x="{x+158}" y="{ROWS["host"]+38}" text-anchor="end" class="lane">{dc}</text>')
svg.append(f'<rect x="150" y="{ROWS["p"]-120}" width="1240" height="180" rx="14" fill="#fff7ed" stroke="#fdba74"/>'
           f'<text x="165" y="{ROWS["p"]-100}" class="lane">core — IS-IS level-2, IPv6-only, MTU 9000, SRv6 block fd00:c::/40</text>')
# links
for l in inv["links"]:
    a, b = l["a"], l["b"]; (ax, ay), (bx, by) = center(a), center(b)
    off = 0
    if N[a]["role"] == "pe" and N[b]["role"] == "ce": off = -60 if l["tenant"] == "tenant-a" else 60
    if N[a]["role"] == "ce" and N[b]["role"] == "host": off = COLS[b] - COLS[a]
    ax, ay = edge(a, by, off); bx, by = edge(b, ay) if N[a]["role"] != N[b]["role"] else (bx, by)
    if N[a]["role"] == N[b]["role"] == "p":   # the triangle: p1-p2 and p2-p3 straight, p1-p3 arched underneath
        ax, ay = center(a); bx, by = center(b)
        if {a, b} == {"p1", "p3"}:
            svg.append(f'<path d="M{ax},{ay-37} C{ax+120},{ay-120} {bx-120},{by-120} {bx},{by-37}" class="core"/>')
            svg.append(f'<text x="{(ax+bx)/2}" y="{ay-90}" text-anchor="middle" class="lbl core">{l["a_port"]} · {l["prefix"]} · {l["b_port"]}</text>')
        else:
            svg.append(f'<line x1="{ax+120}" y1="{ay}" x2="{bx-120}" y2="{by}" class="core"/>')
            svg.append(f'<text x="{(ax+bx)/2}" y="{ay-8}" text-anchor="middle" class="lbl core">{l["prefix"]}</text>')
            svg.append(f'<text x="{(ax+bx)/2}" y="{ay+16}" text-anchor="middle" class="port">{l["a_port"]} · {l["b_port"]}</text>')
        continue
    cls = "core" if N[b]["role"] == "pe" else "access"
    style = f' style="stroke:{TENANT_COLOR[l["tenant"]]}"' if l.get("tenant") else ""
    svg.append(f'<line x1="{ax}" y1="{ay}" x2="{bx}" y2="{by}" class="{cls}"{style}/>')
    mx, my = (ax + bx) / 2, (ay + by) / 2
    if cls == "core":
        at = lambda t: (ax + (bx - ax) * t, ay + (by - ay) * t)
        ang = math.degrees(math.atan2(by - ay, bx - ax)); ang = ang - 180 if ang > 90 else ang
        lx, ly = at(0.3 if abs(bx - ax) > 250 else 0.62)
        svg.append(f'<text x="{lx}" y="{ly-5}" text-anchor="middle" class="lbl core" transform="rotate({ang:.0f} {lx} {ly})">{l["prefix"]}</text>')
        px, py = at(0.1); qx, qy = at(0.9)
        svg.append(f'<text x="{px + (7 if bx >= ax else -7)}" y="{py+4}" class="port" text-anchor="{"start" if bx >= ax else "end"}">{l["a_port"]}</text>')
        svg.append(f'<text x="{qx + (7 if ax >= bx else -7)}" y="{qy+4}" class="port" text-anchor="{"start" if ax >= bx else "end"}">{l["b_port"]}</text>')
    else:
        left = l["tenant"] == "tenant-a" and N[b]["role"] != "host"; anc = "end" if left else "start"; dx = -6 if left else 6
        svg.append(f'<text x="{mx+dx}" y="{my+4}" class="lbl" text-anchor="{anc}" style="fill:{TENANT_COLOR[l["tenant"]]}">{l["prefix"]}</text>')
        svg.append(f'<text x="{ax+dx}" y="{ay+13}" class="port" text-anchor="{anc}">{l["a_port"]} .{l["a_ip"].split("/")[0].split(".")[-1]}</text>')
        svg.append(f'<text x="{bx+dx}" y="{by-5}" class="port" text-anchor="{anc}">{l["b_port"]} .{l["b_ip"].split("/")[0].split(".")[-1]}</text>')
# nodes
for n in inv["nodes"]:
    x, y = center(n["name"]); w, h = BOX[n["role"]]; fill, stroke = FILL[n["role"]]
    lans = {p["tenant"]: p["prefix"] for p in n["ports"] if p["peer"] and N[p["peer"]]["role"] == "host"}
    tn = sorted(S["tenants"])
    lines = {"p": [],
             "pe": [f'{n["loopback6"]} · rid {n["router_id"]} · AS {n["asn"]}', f'locator {n["locator"]}'] + [f'VRF {t} · RD {S["core_as"]}:{S["tenants"][t]["table"] + n["idx"]} · End.DT4' for t in tn],
             "ce": [f'AS {n["asn"]} · eBGP → {n["pe"]} per tenant'] + [f'VRF {t}: {lans.get(t)}' for t in tn],
             "host": [f'{n["ports"][0]["ip"]} · gw .1', f'{n["ports"][0]["tenant"]}']}[n["role"]] if n["role"] != "p" else [
                 f'{n["loopback6"]} · rid {n["router_id"]}', f'locator {n["locator"]}', "VPNv4 route reflector · AS 65000" if n["name"] == S["rr"] else "IPv6 forwarding only, no BGP / VRF"]
    role = {"p": "P" + (" / RR" if n["name"] == S["rr"] else ""), "pe": "PE", "ce": "CE", "host": "host"}[n["role"]]
    svg.append(f'<g><rect x="{x-w/2}" y="{y-h/2}" width="{w}" height="{h}" rx="9" fill="{fill}" stroke="{stroke}" stroke-width="1.6"/>'
               f'<text x="{x-w/2+10}" y="{y-h/2+19}" class="name">{n["name"]}</text><text x="{x+w/2-10}" y="{y-h/2+19}" text-anchor="end" class="role">{role} · {n["mgmt_ip"]}</text>'
               + "".join(f'<text x="{x-w/2+10}" y="{y-h/2+19+14*(i+1)}" class="sub">{html.escape(t)}</text>' for i, t in enumerate(lines)) + "</g>")

diagram = f'<svg viewBox="0 0 1540 770" xmlns="http://www.w3.org/2000/svg">{"".join(svg)}</svg>'

pe1 = N["pe1"]; pe3 = N["pe3"]
page = f"""<!doctype html><html><head><meta charset="utf-8"><title>SRv6 core lab — topology</title>
<style>
 @page {{ size: A3 landscape; margin: 12mm }}
 body {{ font-family: system-ui, -apple-system, "Segoe UI", Roboto, sans-serif; color: #0f172a; margin: 0 }}
 h1 {{ font-size: 24px; margin: 0 0 4px }} h2 {{ font-size: 15px; margin: 18px 0 6px; color: #334155 }}
 p.sub {{ color: #475569; margin: 0 0 10px; font-size: 13px }}
 svg {{ width: 100%; height: auto; display: block }}
 svg .name {{ font: 600 14px system-ui }} svg .role {{ font: 11px system-ui; fill: #475569 }} svg .sub {{ font: 10.5px ui-monospace, Menlo, monospace; fill: #334155 }}
 svg .lane {{ font: 600 12px system-ui; fill: #64748b; letter-spacing: .04em }}
 svg line.core, svg path.core {{ stroke: #c2410c; stroke-width: 2; fill: none }} svg line.access {{ stroke: #64748b; stroke-width: 1.6 }}
 svg .lbl {{ font: 10.5px ui-monospace, Menlo, monospace; fill: #334155 }} svg .lbl.core {{ fill: #9a3412 }} svg .port {{ font: 9.5px ui-monospace, Menlo, monospace; fill: #64748b }}
 .grid {{ display: grid; grid-template-columns: 1.15fr 1fr; gap: 22px; margin-top: 8px; break-before: page }}
 svg {{ margin-top: 18px }}
 table {{ border-collapse: collapse; font-size: 11.5px; width: 100% }} th, td {{ border: 1px solid #e2e8f0; padding: 4px 7px; text-align: left; vertical-align: top }} th {{ background: #f1f5f9 }}
 code {{ font: 11px ui-monospace, Menlo, monospace }}
 .walk {{ background: #f8fafc; border: 1px solid #e2e8f0; border-radius: 8px; padding: 10px 14px; font-size: 12px }}
 .walk ol {{ margin: 6px 0 0; padding-left: 18px }} .walk li {{ margin: 3px 0 }}
 .legend span {{ display: inline-block; margin-right: 16px; font-size: 12px }} .legend i {{ display: inline-block; width: 26px; height: 3px; vertical-align: middle; margin-right: 6px }}
 .foot {{ font-size: 11px; color: #64748b; margin-top: 10px }}
</style></head><body>
<h1>SRv6 WAN core lab — topology</h1>
<p class="sub">Four VyOS PEs (one per data centre) dual-homed to a VyOS P-router triangle; IS-IS level-2 IPv6-only underlay carrying the SRv6 locators;
BGP VPNv4 over SRv6 (End.DT4) reflected by p1; a VyOS CE per data centre with two tenants — <b>tenant-a</b> (h1) and <b>tenant-b</b> (h2) — each in its own VRF on the CE and on its own attachment circuit into its own VRF on the PE. Tenants never meet: h1s reach h1s, h2s reach h2s. 19 VMs on one libvirt/KVM host, ≈13 GiB RAM.
<span class="legend" style="margin-left:14px"><span><i style="background:#c2410c"></i>core link (IPv6 /64, IS-IS, SRv6)</span><span><i style="background:#475569"></i>tenant-a access (IPv4)</span><span><i style="background:#7c3aed"></i>tenant-b access (IPv4)</span></span></p>
{diagram}
<div class="grid">
<div>
<h2>Addressing</h2>
<table><tr><th>Node</th><th>Role</th><th>OOB</th><th>Loopback</th><th>Router-id</th><th>IS-IS NET</th><th>SRv6 locator</th><th>AS</th></tr>
{"".join(f'<tr><td><b>{n["name"]}</b></td><td>{n["role"]}{" (RR)" if n["name"] == S["rr"] else ""}</td><td>{n["mgmt_ip"]}</td><td>{n["loopback6"] or "–"}</td><td>{n["router_id"] or "–"}</td><td>{n["isis_net"] or "–"}</td><td>{n["locator"] or "–"}</td><td>{n["asn"] or "–"}</td></tr>' for n in inv["nodes"])}
</table>
<p class="foot">Links: core <code>fd00:b:0:&lt;ab&gt;::/64</code> (first end ::1), PE–CE <code>172.16.n.0/30</code> (PE .1), CE–host <code>172.20.n.0/24</code> (CE .1 = gateway).
OOB network <code>{inv["oob"]["network"]}</code> 10.3.0.0/24 (host {inv["oob"]["gateway"]}), consoles 127.0.0.1:5301–5319.
{" · ".join(f'VRF <code>{t}</code>: table {v["table"]}, RT {v["rt"]}, RD {S["core_as"]}:{v["table"]}+pe#' for t, v in sorted(S["tenants"].items()))}.
Tenant-b uses PE–CE <code>172.17.n.0/30</code> and LANs <code>172.21.n.0/24</code>.</p>
</div>
<div>
<h2>Packet walk: dc1-h1 → dc3-h1 (tenant-a, dc1 → dc3)</h2>
<div class="walk">
<ol>
<li><b>dc1-h1</b> 172.20.1.2 sends to 172.20.3.2 via its gateway <b>ce1</b> (172.20.1.1).</li>
<li><b>ce1</b> has 172.20.3.0/24 from pe1 in its VRF tenant-a over that VRF's eBGP session → forwards to <b>pe1</b> 172.16.1.1 (VRF tenant-a on the PE too).</li>
<li><b>pe1</b>: VRF route 172.20.3.0/24 = <code>encap seg6 segs 1 [ fd00:c:3:0:X:: ]</code> — the End.DT4 SID pe3 exported with the VPNv4 route (RD 65000:103, RT {S["tenants"]["tenant-a"]["rt"]}, next hop fd00:a::3) via the route reflector p1.
Outer IPv6 <code>{pe1["loopback6"]} → fd00:c:3:0:X::</code> + SRH.</li>
<li><b>p2</b> (the only shortest path west→east) forwards plain IPv6 towards pe3's locator <code>{pe3["locator"]}</code> learned from IS-IS — no VRF, no IPv4 knowledge.</li>
<li><b>pe3</b>: local SID <code>seg6local End.DT4 vrftable tenant-a</code> decapsulates and looks the inner packet up in the VRF → <b>ce3</b> 172.16.3.2 → <b>dc3-h1</b>.</li>
<li>A packet from <b>dc1-h2</b> (tenant-b) takes the same core path but enters through CE VRF tenant-b, the second attachment circuit, PE VRF tenant-b and pe3's <em>other</em> End.DT4 SID; it can never reach a tenant-a address because no tenant-a route exists in any tenant-b table (different RTs).</li>
<li>Reply mirrors the path with pe1's SID. Forwarded packets need the locator block leaked into the VRF table (<code>static route6 fd00:c::/40 … vrf default</code>) because Linux scopes the encapsulation's outer lookup to the ingress VRF.</li>
</ol></div>
<h2>Local SIDs on a PE (pe1)</h2>
<table><tr><th>SID</th><th>Behaviour</th><th>Installed by</th></tr>
<tr><td><code>fd00:c:1::</code></td><td>End (node SID)</td><td>IS-IS</td></tr>
<tr><td><code>fd00:c:1:0:X::</code></td><td>End.X per core adjacency (eth1 → p1, eth2 → p2)</td><td>IS-IS</td></tr>
<tr><td><code>fd00:c:1:0:Y::</code>, <code>fd00:c:1:0:Z::</code></td><td>End.DT4 → VRF tenant-a, End.DT4 → VRF tenant-b (one per tenant)</td><td>BGP (<code>sid vpn export auto</code> in each VRF)</td></tr></table>
<p class="foot">Locator structure: block 40 bits · node 24 bits · function 16 bits; function values are allocated by FRR at run time.</p>
</div></div>
<p class="foot">Generated from <code>lab.conf</code> by <code>docs/topology.py</code> · https://github.com/dcantor/srv6-core</p>
</body></html>"""
(OUT / "topology.html").write_text(page); print("wrote docs/topology.html")
if "--html-only" not in sys.argv:
    from playwright.sync_api import sync_playwright
    with sync_playwright() as pw:
        b = pw.chromium.launch(channel="chrome", headless=True); pg = b.new_page(viewport={"width": 1600, "height": 1100})
        pg.goto((OUT / "topology.html").as_uri()); pg.wait_for_load_state("networkidle")
        pg.pdf(path=str(OUT / "topology.pdf"), format="A3", landscape=True, print_background=True, margin={"top": "12mm", "bottom": "12mm", "left": "12mm", "right": "12mm"})
        b.close()
    print(f"wrote docs/topology.pdf ({(OUT / 'topology.pdf').stat().st_size // 1024} KB)")
