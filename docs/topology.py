#!/usr/bin/env python3
"""Draw the lab topology from `lab.sh inventory` as an SVG inside an HTML page, and print it to docs/topology.pdf
(Chrome via Playwright). Re-run after changing lab.conf.   docs/topology.py [--html-only]"""
import html, ipaddress, json, subprocess, sys
from pathlib import Path

LAB_DIR = Path(__file__).resolve().parents[1]; OUT = LAB_DIR / "docs"
inv = json.loads(subprocess.run([str(LAB_DIR / "lab.sh"), "inventory"], capture_output=True, text=True, check=True).stdout)
N = {n["name"]: n for n in inv["nodes"]}; S = inv["service"]
sys.path.insert(0, str(LAB_DIR / "tools")); from topology_svg import draw   # noqa: E402
diagram, tcolor = draw(inv)
legend = "".join(f'<span><i style="background:{c}"></i>{t} access (IPv4)</span>' for t, c in tcolor.items())

pe1 = N["pe1"]; pe3 = N["pe3"]
page = f"""<!doctype html><html><head><meta charset="utf-8"><title>SRv6 core lab — topology</title>
<style>
 @page {{ size: A3 landscape; margin: 12mm }}
 body {{ font-family: system-ui, -apple-system, "Segoe UI", Roboto, sans-serif; color: #0f172a; margin: 0 }}
 h1 {{ font-size: 24px; margin: 0 0 4px }} h2 {{ font-size: 15px; margin: 18px 0 6px; color: #334155 }}
 p.sub {{ color: #475569; margin: 0 0 10px; font-size: 13px }}
 svg {{ width: 100%; height: auto; display: block }}

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
BGP VPNv4 over SRv6 (End.DT4) reflected by p1; a VyOS CE per data centre with one VRF per tenant ({", ".join(sorted(S["tenants"]))}), each tenant on its own attachment circuit into its own VRF on the PE and with its own host per site. Tenants never meet. {len(inv["nodes"])} VMs on one libvirt/KVM host.
<span class="legend" style="margin-left:14px"><span><i style="background:#c2410c"></i>core link (IPv6 /64, IS-IS, SRv6)</span>{legend}</span></p>
{diagram}
<div class="grid">
<div>
<h2>Addressing</h2>
<table><tr><th>Node</th><th>Role</th><th>OOB</th><th>Loopback</th><th>Router-id</th><th>IS-IS NET</th><th>SRv6 locator</th><th>AS</th></tr>
{"".join(f'<tr><td><b>{n["name"]}</b></td><td>{n["role"]}{" (RR)" if n["name"] in S["rrs"] else ""}</td><td>{n["mgmt_ip"]}</td><td>{n["loopback6"] or "–"}</td><td>{n["router_id"] or "–"}</td><td>{n["isis_net"] or "–"}</td><td>{n["locator"] or "–"}</td><td>{n["asn"] or "–"}</td></tr>' for n in inv["nodes"])}
</table>
<p class="foot">Links: core <code>fd00:b:0:&lt;ab&gt;::/64</code> (first end ::1), PE–CE <code>172.16.n.0/30</code> (PE .1), CE–host <code>172.20.n.0/24</code> (CE .1 = gateway).
OOB network <code>{inv["oob"]["network"]}</code> 10.3.0.0/24 (host {inv["oob"]["gateway"]}), consoles 127.0.0.1:5301–5319.
{" · ".join(f'VRF <code>{t}</code>: table {v["table"]}, RT {v["rt"]}, RD {S["core_as"]}:{v["table"]}+pe#' for t, v in sorted(S["tenants"].items()))}.
Tenant-b uses PE–CE <code>172.18.n.0/30</code> and LANs <code>172.21.n.0/24</code>.</p>
</div>
<div>
<h2>Packet walk: dc1-h1 → dc3-h1 (tenant-a, dc1 → dc3)</h2>
<div class="walk">
<ol>
<li><b>dc1-h1</b> 172.20.1.2 sends to 172.20.3.2 via its gateway <b>ce1</b> (172.20.1.1).</li>
<li><b>ce1</b> has 172.20.3.0/24 from pe1 in its VRF tenant-a over that VRF's eBGP session → forwards to <b>pe1</b> 172.16.1.1 (VRF tenant-a on the PE too).</li>
<li><b>pe1</b>: VRF route 172.20.3.0/24 = <code>encap seg6 segs 1 [ fd00:c:3:e0XX:: ]</code> — the End.DT4 SID pe3 exported with the VPNv4 route (RD 65000:103, RT {S["tenants"]["tenant-a"]["rt"]}, next hop fd00:a::3) via the route reflectors p1 and p3 (the PE keeps both copies; losing one reflector changes nothing).
Outer IPv6 <code>{pe1["loopback6"]} → fd00:c:3:0:X::</code> + SRH.</li>
<li><b>p2</b> (the only shortest path west→east) forwards plain IPv6 towards pe3's locator <code>{pe3["locator"]}</code> learned from IS-IS — no VRF, no IPv4 knowledge.</li>
<li><b>pe3</b>: local SID <code>seg6local End.DT4 vrftable tenant-a</code> decapsulates and looks the inner packet up in the VRF → <b>ce3</b> 172.16.3.2 → <b>dc3-h1</b>.</li>
<li>A packet from <b>dc1-h2</b> (tenant-b) takes the same core path but enters through CE VRF tenant-b, the second attachment circuit, PE VRF tenant-b and pe3's <em>other</em> End.DT4 SID; it can never reach a tenant-a address because no tenant-a route exists in any tenant-b table (different RTs).</li>
<li>Reply mirrors the path with pe1's SID. Forwarded packets need the locator block leaked into the VRF table (<code>static route6 fd00:c::/40 … vrf default</code>) because Linux scopes the encapsulation's outer lookup to the ingress VRF.</li>
</ol></div>
<h2>Local SIDs on a PE (pe1)</h2>
<table><tr><th>SID</th><th>Behaviour</th><th>Installed by</th></tr>
<tr><td><code>fd00:c:1::/48</code></td><td>uN (End, NEXT-C-SID flavour: shift 16 bits, forward)</td><td>IS-IS</td></tr>
<tr><td><code>fd00:c:1:e000::</code>, <code>fd00:c:1:e001::</code></td><td>uA — End.X per core adjacency</td><td>IS-IS</td></tr>
<tr><td><code>fd00:c:1:e002::</code>, <code>fd00:c:1:e003::</code></td><td>uDT4 — End.DT4 → VRF tenant-a / tenant-b (one per tenant)</td><td>BGP (<code>sid vpn export auto</code> in each VRF)</td></tr></table>
<p class="foot">uSID (usid-f3216): block 32 · node 16 · function 16 bits, /48 locators from fd00:c::/32; a steered path p1 → p3 → pe3 is the single segment <code>fd00:c:11:13:3:e001::</code>. Function values are allocated by FRR at run time.</p>
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
        pg.locator("svg").screenshot(path=str(OUT / "topology.png"))   # the diagram alone, for the README
        b.close()
    print(f"wrote docs/topology.pdf ({(OUT / 'topology.pdf').stat().st_size // 1024} KB) and docs/topology.png")
