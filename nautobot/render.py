#!/usr/bin/env python3
"""Render the VyOS configurations from Nautobot (source of truth) with the same renderer lab.conf uses.

Reads the saved GraphQL query `srv6-core-model`, rebuilds the inventory dict (devices, interfaces, cables, addresses,
VRFs / RDs, BGP instances and peerings, config context) and hands it to tools/render.py.
   render.py                 print a summary and the rendered config of every node to stdout (--node NAME for one)
   render.py --write         write nodes/<n>/vyos_config.txt (what bootstrap / configure push) and, for a looking glass,
                             nodes/<n>/frr.conf + lgd.json (what lab.sh lg deploy copies onto the VM)
   render.py --check         exit 1 if Nautobot's rendering differs from nodes/<n>/vyos_config.txt (lab.conf's rendering)
   render.py --live          exit 1 if a node's running configuration lacks any rendered `set` line (netmiko, vyos/vyos)
   render.py --inventory     dump the rebuilt inventory as JSON"""
import argparse, ipaddress, json, os, re, sys
from pathlib import Path
import requests

LAB = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(LAB / "tools")); from render import render_all, render_lg, lg_app_config, lg_nodes   # noqa: E402
p = argparse.ArgumentParser()
p.add_argument("--url", default=os.environ.get("NAUTOBOT_URL", "http://10.0.0.10:8080"))
p.add_argument("--token", default=os.environ.get("NAUTOBOT_TOKEN"))
p.add_argument("--write", action="store_true"); p.add_argument("--check", action="store_true"); p.add_argument("--live", action="store_true")
p.add_argument("--inventory", action="store_true"); p.add_argument("--node")
a = p.parse_args()
H = {"Authorization": f"Token {a.token}", "Accept": "application/json"}


def gql(query):
    r = requests.post(f"{a.url}/api/graphql/", json={"query": query}, headers=H, timeout=120); r.raise_for_status()
    body = r.json()
    if body.get("errors"): sys.exit(f"GraphQL: {body['errors']}")
    return body["data"]


def inventory_from_nautobot():
    q = requests.get(f"{a.url}/api/extras/graphql-queries/", params={"name": "srv6-core-model"}, headers=H, timeout=30).json()["results"]
    if not q: sys.exit("saved GraphQL query srv6-core-model not found — run nautobot/seed.py")
    d = gql(q[0]["query"]); ctx = d["config_contexts"][0]["data"]
    ROLE = {"srv6-pe": "pe", "srv6-p": "p", "srv6-ce": "ce", "srv6-fw": "fw", "srv6-lg": "lg", "host": "host"}; inet = ctx.get("internet")
    devs = {x["name"]: x for x in d["devices"]}
    # tenant of an address: the VRF its parent prefix belongs to (prefix roles attachment-circuit / site-lan carry a tenant)
    lab_vrfs = [v for v in d["vrfs"] if v["tenant"] and v["tenant"]["tenant_group"] and v["tenant"]["tenant_group"]["name"] == "srv6-core"]
    vrf_of_prefix = {pf["prefix"]: v["name"] for v in lab_vrfs for pf in v["prefixes"]}
    nodes = []
    for name, x in sorted(devs.items(), key=lambda kv: kv[0]):
        role = ROLE[x["role"]["name"]]; loc = x["location"]["name"]; dc = loc if loc != "srv6-core" else "core"
        ri = (x["bgp_routing_instances"] or [None])[0]
        ports, lo6, locator_addr = [], None, None
        for i in sorted(x["interfaces"], key=lambda i: (i["name"][:3], int(i["name"][3:]) if i["name"][3:].isdigit() else 0)):
            addrs = i["ip_addresses"]; v4 = [a for a in addrs if ":" not in a["address"]]; v6 = [a for a in addrs if ":" in a["address"]]
            addr = (v4 or v6 or [{"address": None}])[0]["address"]   # core links are IPv6-only; tenant links are dual-stack (IPv4 first, its twin second)
            if i["name"] == "lo": lo6 = addr.split("/")[0] if addr else None; continue
            if i["name"] == "dum0": locator_addr = addr; continue
            if i["mgmt_only"]: continue
            if role == "fw" and not addrs and i["description"].startswith("internet:"):   # the firewall's DHCP uplink on the host's libvirt NAT network
                ports.append({"name": i["name"], "ip": "dhcp", "peer": None, "network": inet["net"] if inet else i["description"].split()[2]}); continue
            far = i["connected_interface"]; first = (v4 or v6 or [None])[0]; parent = first["parent"]["prefix"] if first and first.get("parent") else None
            twin = v6[0] if v4 and v6 else None
            ports.append({"name": i["name"], "ip": addr, "peer": far["device"]["name"] if far else None, "peer_port": far["name"] if far else None,
                          "prefix": parent, "tenant": vrf_of_prefix.get(parent), "ip6": twin["address"] if twin else None, "prefix6": twin["parent"]["prefix"] if twin and twin.get("parent") else None})
        pe = next((pt["peer"] for pt in ports if pt["peer"] and pt["peer"] in devs and ROLE[devs[pt["peer"]]["role"]["name"]] == "pe"), None) if role in ("ce", "fw") else None
        rd = {va["vrf"]["name"]: va["rd"] for va in x["vrf_assignments"] if va["rd"]}
        nodes.append({"name": name, "role": role, "dc": dc, "mgmt_ip": x["primary_ip4"]["address"].split("/")[0], "loopback6": lo6,
                      "router_id": ri["router_id"]["address"].split("/")[0] if ri and ri["router_id"] else None, "locator": x["cf_srv6_locator"] or None,
                      "isis_net": x["cf_isis_net"] or None, "asn": int(ri["autonomous_system"]["asn"]) if ri else None, "pe": pe, "rd": rd, "ports": ports})
    # external CEs: a PE port cabled to a device outside the lab (an IPsec headend) — modelled minimally, as lab.sh does
    for pe in [n for n in nodes if n["role"] == "pe"]:
        for pt in pe["ports"]:
            if not pt["peer"] or pt["peer"] in devs or any(m["name"] == pt["peer"] for m in nodes): continue
            far = next(i["connected_interface"] for i in devs[pe["name"]]["interfaces"] if i["name"] == pt["name"]); fd = far["device"]
            top = fd["location"]; 
            while top.get("parent"): top = top["parent"]
            nodes.append({"name": fd["name"], "role": "ext-ce", "dc": pe["dc"], "mgmt_ip": fd["primary_ip4"]["address"].split("/")[0], "lab": top["name"], "loopback6": None, "router_id": None,
                          "locator": None, "isis_net": None, "asn": int(fd["bgp_routing_instances"][0]["autonomous_system"]["asn"]), "pe": pe["name"], "rd": {},
                          "ports": [{"name": far["name"], "ip": far["ip_addresses"][0]["address"], "peer": pe["name"], "peer_port": pt["name"], "prefix": pt["prefix"], "tenant": pt["tenant"]}]})
    rrs = sorted(n["name"] for n in nodes if n["role"] == "p" and any(ep["role"] and ep["role"]["name"] == "rr" for ri in devs[n["name"]]["bgp_routing_instances"] for ep in ri["endpoints"]))
    core_as = next(n["asn"] for n in nodes if n["role"] == "pe")
    service = {"core_as": core_as, "rr": rrs[0], "rrs": rrs, "isis_area": ctx["isis"]["area"], "tenants": ctx["tenants"],
               "srv6": {k: ctx["srv6"][k] for k in ("block", "format", "block_len", "node_len", "func_bits")}, **({"internet": inet} if inet else {})}
    links, seen = [], set()
    for n in nodes:
        for pt in n["ports"]:
            if not pt["peer"] or (pt["peer"], pt["peer_port"], n["name"], pt["name"]) in seen: continue
            seen.add((n["name"], pt["name"], pt["peer"], pt["peer_port"]))
            net = ipaddress.ip_network(pt["prefix"]); first = ipaddress.ip_interface(pt["ip"]).ip == net.network_address + 1
            b = next(q for q in next(m for m in nodes if m["name"] == pt["peer"])["ports"] if q["name"] == pt["peer_port"])
            a_, b_ = ((n["name"], pt), (pt["peer"], b)) if first else ((pt["peer"], b), (n["name"], pt))
            links.append({"a": a_[0], "a_port": a_[1]["name"], "a_ip": a_[1]["ip"], "b": b_[0], "b_port": b_[1]["name"], "b_ip": b_[1]["ip"], "prefix": pt["prefix"], "tenant": pt["tenant"],
                          "a_ip6": a_[1].get("ip6"), "b_ip6": b_[1].get("ip6"), "prefix6": pt.get("prefix6")})
    return {"lab": "srv6-core", "source": "nautobot", "oob": {"network": ctx["oob"]["network"], "gateway": ctx["oob"]["gateway"]}, "service": service, "nodes": nodes, "links": links}


inv = inventory_from_nautobot()
if a.inventory: print(json.dumps(inv, indent=1)); sys.exit()
rendered = render_all(inv)
extra = {}                       # the collector is not a VyOS node: FRR syntax plus the model its service reads
for n in lg_nodes(inv):
    extra[f"{n['name']}/frr.conf"] = render_lg(inv, n["name"])
    extra[f"{n['name']}/lgd.json"] = json.dumps(lg_app_config(inv, n["name"]), indent=1, sort_keys=True) + "\n"
if a.node:
    rendered = {a.node: rendered[a.node]} if a.node in rendered else {}
    extra = {k: v for k, v in extra.items() if k.split("/")[0] == a.node}
rc = 0
if a.write:
    for name, text in list(rendered.items()) + [(k, v) for k, v in extra.items()]:
        path = LAB / "nodes" / (f"{name}/vyos_config.txt" if "/" not in name else name)
        if not path.exists() or path.read_text() != text: path.write_text(text); print(f"wrote {path.relative_to(LAB)}")
        else: print(f"{name}: unchanged")
elif a.check:
    for name, text in list(rendered.items()) + [(k, v) for k, v in extra.items()]:
        path = LAB / "nodes" / (f"{name}/vyos_config.txt" if "/" not in name else name); have = path.read_text() if path.exists() else ""
        if have == text: print(f"{name}: Nautobot == lab.conf ({text.count(chr(10))} lines)")
        else:
            import difflib; rc = 1; print(f"{name}: DIFFERS"); print("".join(difflib.unified_diff(have.splitlines(True), text.splitlines(True), "lab.conf", "nautobot", n=1)))
elif a.live:
    from netmiko import ConnectHandler
    for name, text in rendered.items():
        node = next(n for n in inv["nodes"] if n["name"] == name)
        c = ConnectHandler(device_type="vyos", host=node["mgmt_ip"], username=os.environ.get("VYOS_USERNAME", "vyos"), password=os.environ.get("VYOS_PASSWORD", "vyos"))
        running = {re.sub(r"'", "", l.strip()) for l in c.send_command("show configuration commands", read_timeout=120).splitlines()}; c.disconnect()
        want = [re.sub(r"'", "", l.strip()) for l in text.splitlines() if l.startswith("set ") and "plaintext-password" not in l]   # VyOS stores the hash
        missing = [l for l in want if l not in running]
        if missing: rc = 1; print(f"{name}: {len(missing)} rendered line(s) missing from the running configuration:\n  " + "\n  ".join(missing[:10]))
        else: print(f"{name}: in sync ({len(want)} set lines present)")
else:
    print(f"# {len(inv['nodes'])} devices, {len(inv['links'])} links, tenants {list(inv['service']['tenants'])}, reflectors {inv['service']['rrs']} — from Nautobot")
    for name, text in list(rendered.items()) + [(k, v) for k, v in extra.items()]: print(f"\n##### {name}\n{text}")
sys.exit(rc)
