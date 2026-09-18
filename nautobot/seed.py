#!/usr/bin/env python3
"""Seed the shared Nautobot with the SRv6 core lab (idempotent). Source: `lab.sh inventory` (i.e. lab.conf).

What is modelled
  locations      site "srv6-core" (type Site) with a "Data Center" location per DC (dc1..dc4); the P routers live at the site
  tenancy        tenant group "srv6-core", tenants tenant-a / tenant-b
  devices        VyOS PEs / Ps / CEs (roles srv6-pe / srv6-p / srv6-ce, platform vyos) and Alpine hosts (role host, platform
                 linux); eth0 = OOB (primary IPv4), ethN with the lab MACs, lo and dum0 as virtual interfaces; cables from LINKS
  custom fields  device: isis_net, srv6_locator (grouping SRv6); interface: none — link roles are prefix roles
  IPAM           prefixes with roles oob-management / loopback / wan-p2p / srv6-locator / attachment-circuit / site-lan / router-id,
                 tenant prefixes in VRFs tenant-a / tenant-b (route targets 65000:100 / 65000:200), VRF device assignments with
                 the per-PE RD; every interface address
  BGP            nautobot-bgp-models: AS 65000 + one per CE, a routing instance per BGP speaker (router-id), address families
                 (vpnv4 on PEs and RRs, ipv4 per tenant VRF on PEs and CEs), peerings PE<->RR (vpnv4) and PE<->CE (ipv4 in the VRF);
                 the RR role on p1/p3 endpoints; `sid vpn export auto` as extra attribute of the PE VRF address families
  config context srv6-core: IS-IS area / level, SRv6 locator structure, BFD, MTU, OOB gateway, tenant kernel tables
  GraphQL        saved query srv6-core-model — what nautobot/render.py reads to rebuild the inventory and render the configs
Usage: NAUTOBOT_TOKEN=... seed.py [--url http://10.0.0.10:8080]"""
import argparse, ipaddress, json, os, subprocess, sys
from pathlib import Path
import pynautobot, requests

LAB = Path(__file__).resolve().parents[1]
p = argparse.ArgumentParser()
p.add_argument("--url", default=os.environ.get("NAUTOBOT_URL", "http://10.0.0.10:8080"))
p.add_argument("--token", default=os.environ.get("NAUTOBOT_TOKEN"))
a = p.parse_args()
inv = json.loads(subprocess.run([str(LAB / "lab.sh"), "inventory"], capture_output=True, text=True, check=True).stdout)
SVC, OOB = inv["service"], inv["oob"]; N = {n["name"]: n for n in inv["nodes"]}
SITE = "srv6-core"; MAC_OUI = "52:54:00:c6"
nb = pynautobot.api(a.url, token=a.token); H = {"Authorization": f"Token {a.token}", "Accept": "application/json"}
created = []


# ---- helpers (same idioms as the IPsec lab's seed) --------------------------------------------------------------
def get_or_create(ep, lookup, **d):
    o = ep.get(**lookup)
    if o is None: o = ep.create(**lookup, **d); created.append(f"{ep.name}:{list(lookup.values())[0]}")
    return o


def current(v):
    if hasattr(v, "id"): return str(v.id)
    if hasattr(v, "value") and hasattr(v, "label"): return v.value
    if hasattr(v, "serialize"): return {k: current(getattr(v, k)) for k in v.serialize()}
    return v


def ensure(obj, **fields):
    ch = {}
    for k, v in fields.items():
        c = current(getattr(obj, k, None))
        same = c == v if isinstance(v, (dict, list)) else (str(c).lower() == str(v).lower() if k == "mac_address" else str(c) == str(v))
        if not same: ch[k] = v
    if ch: obj.update(ch); created.append(f"updated {getattr(obj, 'name', None) or getattr(obj, 'prefix', None) or getattr(obj, 'address', obj)}: {', '.join(ch)}")
    return obj


def ensure_cf(obj, **fields):
    cur = obj.custom_fields or {}
    if any(cur.get(k) != v for k, v in fields.items()): obj.update({"custom_fields": {**cur, **fields}}); created.append(f"custom fields on {getattr(obj, 'name', obj)}")


def gql(query):
    r = requests.post(f"{a.url}/api/graphql/", json={"query": query}, headers=H, timeout=60); r.raise_for_status()
    body = r.json()
    if body.get("errors"): sys.exit(f"GraphQL: {body['errors']}")
    return body["data"]


def patch(path, **fields):
    r = requests.patch(f"{a.url}/api/{path}/", json=fields, headers={**H, "Content-Type": "application/json"}, timeout=60); r.raise_for_status()


def ensure_status_ct(status, *cts):
    need = [ct for ct in cts if ct not in status.content_types]
    if need: status.update({"content_types": list(status.content_types) + need})


active = nb.extras.statuses.get(name="Active"); connected = nb.extras.statuses.get(name="Connected")
ensure_status_ct(active, "nautobot_bgp_models.autonomoussystem", "nautobot_bgp_models.bgproutinginstance", "nautobot_bgp_models.peering", "ipam.vrf")
ns = nb.ipam.namespaces.get(name="Global")

# ---- locations ---------------------------------------------------------------------------------------------------
lt_site = nb.dcim.location_types.get(name="Site")
lt_dc = get_or_create(nb.dcim.location_types, {"name": "Data Center"}, parent=lt_site.id, content_types=["dcim.device", "ipam.prefix", "ipam.vlan"], description="a tenant site of the SRv6 core lab")
site = get_or_create(nb.dcim.locations, {"name": SITE}, location_type=lt_site.id, status=active.id)
ensure(site, description="SRv6 WAN core lab: 4 VyOS PEs, P triangle (p1/p3 route reflectors), 2 tenant VRFs, VyOS CEs and Alpine tenant hosts (github.com/dcantor/srv6-core)")
dcs = {}
for dc in sorted({n["dc"] for n in inv["nodes"] if n["dc"] != "core"}):
    dcs[dc] = get_or_create(nb.dcim.locations, {"name": dc}, location_type=lt_dc.id, parent=site.id, status=active.id)
    ensure(dcs[dc], parent=site.id, description=f"data centre {dc}: PE, CE and one host per tenant")
loc_of = lambda n: dcs[n["dc"]] if n["dc"] in dcs else site

# ---- tenancy ---------------------------------------------------------------------------------------------------------
tg = get_or_create(nb.tenancy.tenant_groups, {"name": SITE}, description="customers of the SRv6 core lab")
tenants = {t: get_or_create(nb.tenancy.tenants, {"name": t}, tenant_group=tg.id) for t in sorted(SVC["tenants"])}
for t in tenants: ensure(tenants[t], tenant_group=tg.id, description=f"{t}: VRF on every PE and CE, kernel table {SVC['tenants'][t]['table']}, RT {SVC['tenants'][t]['rt']}")

# ---- roles, platforms, device types, custom fields -----------------------------------------------------------------
drole = {"pe": get_or_create(nb.extras.roles, {"name": "srv6-pe"}, color="b71c1c", content_types=["dcim.device"]),
         "p": get_or_create(nb.extras.roles, {"name": "srv6-p"}, color="e65100", content_types=["dcim.device"]),
         "ce": get_or_create(nb.extras.roles, {"name": "srv6-ce"}, color="1565c0", content_types=["dcim.device"]),
         "host": get_or_create(nb.extras.roles, {"name": "host"}, color="4caf50", content_types=["dcim.device"])}
prole = {n: get_or_create(nb.extras.roles, {"name": n}, color=c, content_types=["ipam.prefix"])
         for n, c in (("oob-management", "9e9e9e"), ("loopback", "795548"), ("wan-p2p", "607d8b"), ("srv6-locator", "e65100"), ("attachment-circuit", "3f51b5"), ("site-lan", "4caf50"), ("router-id", "9c27b0"))}
for r in prole.values():
    if "ipam.prefix" not in r.content_types: r.update({"content_types": list(r.content_types) + ["ipam.prefix"]})
brole = {n: get_or_create(nb.extras.roles, {"name": n}, color=c, content_types=["nautobot_bgp_models.peerendpoint"]) for n, c in (("rr", "e65100"), ("rr-client", "b71c1c"), ("pe", "b71c1c"), ("ce", "1565c0"))}
for r in brole.values():
    if "nautobot_bgp_models.peerendpoint" not in r.content_types: r.update({"content_types": list(r.content_types) + ["nautobot_bgp_models.peerendpoint"]})
plat = {"vyos": nb.dcim.platforms.get(name="vyos"), "linux": nb.dcim.platforms.get(name="linux")}
mf_vyos = get_or_create(nb.dcim.manufacturers, {"name": "VyOS"}); mf_alpine = get_or_create(nb.dcim.manufacturers, {"name": "Alpine Linux"})
dt = {"vyos": get_or_create(nb.dcim.device_types, {"model": "VyOS"}, manufacturer=mf_vyos.id, u_height=0),
      "alpine": get_or_create(nb.dcim.device_types, {"model": "Alpine host"}, manufacturer=mf_alpine.id, u_height=0, comments="Alpine Linux tenant host with iperf3 / tcpdump / mtr (cloud-init NoCloud), 256 MiB")}
cf = {c.key: c for c in nb.extras.custom_fields.all()}
for key, label, desc in (("isis_net", "IS-IS NET", "network entity title of the IS-IS level-2 instance"), ("srv6_locator", "SRv6 locator", "the node's locator prefix (block 40 / node 24 / function 16 bits)")):
    if key not in cf: cf[key] = nb.extras.custom_fields.create(key=key, label=label, type="text", content_types=["dcim.device"], grouping="SRv6", description=desc); created.append(f"custom-field:{key}")

# ---- prefixes ---------------------------------------------------------------------------------------------------------
pq = {x["prefix"]: x for x in gql('{ prefixes { prefix locations { name } } }')["prefixes"]}   # locations are M2M and invisible to REST reads
def ensure_prefix(prefix, role, description, location=None, **more):
    pf = nb.ipam.prefixes.get(prefix=prefix, namespace=ns.id)
    if pf is None:
        pf = nb.ipam.prefixes.create(prefix=prefix, namespace=ns.id, status=active.id, role=prole[role].id, description=description, **({"locations": [location]} if location else {}), **more); created.append(f"prefix:{prefix}")
    else:
        ensure(pf, role=prole[role].id, description=description, **more)
        want = {nb.dcim.locations.get(id=location).name} if location else set()
        if {x["name"] for x in pq.get(prefix, {}).get("locations", [])} != want: patch(f"ipam/prefixes/{pf.id}", location=location); created.append(f"prefix {prefix} location")   # 'locations' is ignored on PATCH; the singular alias works
    return pf

ensure_prefix("10.3.0.0/24", "oob-management", f"srv6-core OOB network (libvirt {OOB['network']}, host {OOB['gateway']}, NMS 10.3.0.10)", location=site.id)
ensure_prefix("fd00:a::/48", "loopback", "core loopbacks (IS-IS passive, BGP sessions, SRv6 encapsulation source)", location=site.id, type="container")
ensure_prefix("fd00:b::/48", "wan-p2p", "core point-to-point links (/64 each, IS-IS level-2, MTU 9000)", location=site.id, type="container")
SR = SVC["srv6"]
ensure_prefix(SR["block"], "srv6-locator", f"SRv6 block ({SR['format']}): one /{SR['block_len'] + SR['node_len']} locator per core node — block {SR['block_len']} / node {SR['node_len']} / function {SR['func_bits']} bits", location=site.id, type="container")
ensure_prefix("10.255.0.0/24", "router-id", "BGP router-ids of the core nodes (not interface addresses)", location=site.id, type="container")
for t in tenants:   # one /16 container per tenant and kind, derived from the tenant's links (172.(16+i) circuits, 172.(20+i) LANs)
    for kind, role, label in (("ac", "attachment-circuit", "PE-CE attachment circuits, {t} (/30 per site)"), ("lan", "site-lan", "{t} site LANs (/24 per DC)")):
        for sup in sorted({str(ipaddress.ip_network(l["prefix"]).supernet(new_prefix=16)) for l in inv["links"] if l.get("tenant") == t and (N[l["b"]]["role"] == "host") == (kind == "lan") and ipaddress.ip_network(l["prefix"]).version == 4}):
            ensure_prefix(sup, role, label.format(t=t), type="container", tenant=tenants[t].id)
for n in inv["nodes"]:
    if n.get("locator"): ensure_prefix(n["locator"], "srv6-locator", f"SRv6 locator of {n['name']} ({'uN' if SR['format'].startswith('usid') else 'End'} SID {n['locator'].split('/')[0]}; uA / uDT4 functions allocated by FRR)", location=loc_of(n).id)
# VRFs (before the tenant prefixes, which belong to them)
rts = {t: get_or_create(nb.ipam.route_targets, {"name": SVC["tenants"][t]["rt"]}, tenant=tenants[t].id, description=f"{t} import/export") for t in tenants}
vrfs = {}
for t in tenants:
    vrfs[t] = nb.ipam.vrfs.get(name=t, namespace=ns.id)
    if vrfs[t] is None:
        vrfs[t] = nb.ipam.vrfs.create(name=t, namespace=ns.id, status=active.id, tenant=tenants[t].id, import_targets=[rts[t].id], export_targets=[rts[t].id],
                                      description=f"{t}: L3VPN over SRv6 (End.DT4), kernel table {SVC['tenants'][t]['table']} on every PE and CE"); created.append(f"vrf:{t}")
    else: ensure(vrfs[t], tenant=tenants[t].id, description=f"{t}: L3VPN over SRv6 (End.DT4), kernel table {SVC['tenants'][t]['table']} on every PE and CE")
# M2M fields (targets, prefixes) are not returned by the REST API: read them through GraphQL, PATCH when they differ
vq = {v["name"]: v for v in gql('{ vrfs(name: ["' + '", "'.join(tenants) + '"]) { name import_targets { name } export_targets { name } prefixes { prefix } } }')["vrfs"]}
for t in tenants:
    if {x["name"] for x in vq.get(t, {}).get("import_targets", [])} != {SVC["tenants"][t]["rt"]} or {x["name"] for x in vq.get(t, {}).get("export_targets", [])} != {SVC["tenants"][t]["rt"]}:
        patch(f"ipam/vrfs/{vrfs[t].id}", import_targets=[rts[t].id], export_targets=[rts[t].id]); created.append(f"vrf targets:{t}")
vrf_prefixes = {t: {x["prefix"] for x in vq.get(t, {}).get("prefixes", [])} for t in tenants}; vrf_prefix_ids = {t: [] for t in tenants}
link_prefix = {}
for l in inv["links"]:
    net = ipaddress.ip_network(l["prefix"]); a_n, b_n = N[l["a"]], N[l["b"]]
    if net.version == 6:
        pf = ensure_prefix(l["prefix"], "wan-p2p", f"core link {l['a']} {l['a_port']} <-> {l['b']} {l['b_port']}", location=site.id)
    elif b_n["role"] == "host":
        pf = ensure_prefix(l["prefix"], "site-lan", f"{l['tenant']} LAN of {a_n['dc']}: {l['a']} {l['a_port']} (gateway) -> {l['b']}", location=loc_of(a_n).id, tenant=tenants[l["tenant"]].id)
    else:
        pf = ensure_prefix(l["prefix"], "attachment-circuit", f"{l['tenant']} attachment circuit {l['a']} {l['a_port']} <-> {l['b']} {l['b_port']}", location=loc_of(b_n).id, tenant=tenants[l["tenant"]].id)
    if l.get("tenant"): vrf_prefix_ids[l["tenant"]].append(pf.id)
    link_prefix[(l["a"], l["a_port"])] = link_prefix[(l["b"], l["b_port"])] = (pf, l)
for t in tenants:
    want = [l["prefix"] for l in inv["links"] if l.get("tenant") == t]
    for pf_id, pfx in zip(vrf_prefix_ids[t], want):
        if pfx not in vrf_prefixes[t]: nb.ipam.vrf_prefix_assignments.create(vrf=vrfs[t].id, prefix=pf_id); created.append(f"vrf {t} += {pfx}")
    for pfx in vrf_prefixes[t] - set(want):   # a renumbered circuit leaves its old prefix in the VRF: drop the assignment (the prefix itself may belong to another lab)
        old = nb.ipam.prefixes.get(prefix=pfx, namespace=ns.id); va = old and nb.ipam.vrf_prefix_assignments.get(vrf=vrfs[t].id, prefix=old.id)
        if va: va.delete(); created.append(f"vrf {t} -= {pfx}")

# ---- devices, interfaces, addresses, cables -------------------------------------------------------------------------
devs, ifs, ips = {}, {}, {}
def ensure_ip(address, description, iface=None, tenant=None):
    ip = nb.ipam.ip_addresses.get(address=address, namespace=ns.id)
    if ip is None: ip = nb.ipam.ip_addresses.create(address=address, namespace=ns.id, status=active.id, description=description, **({"tenant": tenant.id} if tenant else {})); created.append(f"ip:{address}")
    else: ensure(ip, description=description, **({"tenant": tenant.id} if tenant else {}))
    if iface is not None:
        for x in nb.ipam.ip_address_to_interface.filter(interface=iface.id):   # one address per lab interface: a renumbered link drops the old one
            if str(getattr(x.ip_address, "id", x.ip_address)) != ip.id: x.delete(); created.append(f"unassign old address from {iface.device.name} {iface.name}")
        if not nb.ipam.ip_address_to_interface.get(ip_address=ip.id, interface=iface.id):
            nb.ipam.ip_address_to_interface.create(ip_address=ip.id, interface=iface.id); created.append(f"assign {address} -> {iface.device.name} {iface.name}")
    return ip

for n in inv["nodes"]:
    role = n["role"]
    if role == "ext-ce":   # another lab's router (its seed owns the device, OOB, other ports): only the attachment circuit port is ours
        d = nb.dcim.devices.get(name=n["name"])
        if d is None: sys.exit(f"{n['name']} (external CE, {n['lab']} lab) is not in Nautobot — seed that lab first")
        devs[n["name"]] = d
        for port in [pt for pt in n["ports"] if pt["peer"]]:
            i = nb.dcim.interfaces.get(device=d.id, name=port["name"])
            if i is None: sys.exit(f"{n['name']} {port['name']} does not exist in Nautobot — the {n['lab']} lab's seed creates it")
            ensure(i, description=f"{port['tenant']}: {port['peer']} {port['peer_port']} (srv6-core)", enabled=True); ifs[(n["name"], port["name"])] = i
            ensure_ip(port["ip"], f"{n['name']} {port['name']} ({port['tenant']}, srv6-core attachment)", i, tenants.get(port.get("tenant")))
        continue
    loc = loc_of(n)
    d = nb.dcim.devices.get(name=n["name"])
    fields = dict(role=drole[role].id, device_type=dt["alpine" if role == "host" else "vyos"].id, location=loc.id, platform=plat["linux" if role == "host" else "vyos"].id, status=active.id,
                  comments={"pe": "PE: IS-IS L2 + SRv6 locator, VPNv4 to both reflectors, one VRF per tenant (End.DT4)", "p": "P: IPv6 forwarding only" + (" + VPNv4 route reflector" if n["name"] in SVC["rrs"] else ""),
                            "ce": "CE: one VRF per tenant, eBGP to the PE per VRF", "host": f"Alpine tenant host with iperf3 ({[p for p in n['ports'] if p['peer']][0]['tenant']})"}[role])
    if role != "host" and n.get("tenant") is None:
        pass
    if d is None: d = nb.dcim.devices.create(name=n["name"], **fields); created.append(f"device:{n['name']}")
    else: ensure(d, **fields)
    if role in ("pe", "p"): ensure_cf(d, isis_net=n["isis_net"], srv6_locator=n["locator"])
    devs[n["name"]] = d
    # eth0 = OOB, ethN = links, lo / dum0 virtual
    idx = n["idx"]
    def ensure_if(name, itype, description, mac=None, mgmt_only=False):
        i = nb.dcim.interfaces.get(device=d.id, name=name)
        f = dict(type=itype, description=description, mgmt_only=mgmt_only, status=active.id, **({"mac_address": mac} if mac else {}))
        if i is None: i = nb.dcim.interfaces.create(device=d.id, name=name, **f); created.append(f"interface:{n['name']} {name}")
        else: ensure(i, **f)
        ifs[(n["name"], name)] = i; return i
    e0 = ensure_if("eth0", "1000base-t", "OOB management", mac=f"{MAC_OUI}:{idx:02x}:00", mgmt_only=True)
    mip = ensure_ip(f"{n['mgmt_ip']}/24", f"{n['name']} OOB", e0)
    ensure(d, primary_ip4=mip.id)
    for port in n["ports"]:
        pnum = int(port["name"][3:]); pf_l = link_prefix.get((n["name"], port["name"]))
        if port["peer"]:
            peer = N[port["peer"]]; kind = "core" if peer["role"] in ("pe", "p") and role in ("pe", "p") else (port.get("tenant") or "")
            desc = f"{kind}: {port['peer']} {port['peer_port']}" if kind == "core" else (f"{port['tenant']} LAN: {port['peer']}" if peer["role"] == "host" else (f"{port['tenant']}: {port['peer']} {port['peer_port']}" if role in ("pe", "ce") and peer["role"] in ("pe", "ce") else f"{port['peer']} {port['peer_port']}"))
            if role == "host": desc = f"{port['tenant']} LAN, gateway {port['peer']} {port['peer_port']}"
        else: desc = "unwired"
        i = ensure_if(port["name"], "1000base-t", desc, mac=f"{MAC_OUI}:{idx:02x}:{pnum:02x}")
        if port["ip"]: ensure_ip(port["ip"], f"{n['name']} {port['name']}" + (f" ({port['tenant']})" if port.get("tenant") else ""), i, tenants.get(port.get("tenant")))
        else:   # unwired (e.g. after a tenant was removed): no cable, no address
            cur = requests.get(f"{a.url}/api/dcim/interfaces/{i.id}/", params={"depth": 1}, headers=H, timeout=30).json()
            if cur.get("cable"): requests.delete(f"{a.url}/api/dcim/cables/{cur['cable']['id']}/", headers=H, timeout=30); created.append(f"cable removed from unwired {n['name']} {port['name']}")
            for x in nb.ipam.ip_address_to_interface.filter(interface=i.id): x.delete(); created.append(f"address unassigned from unwired {n['name']} {port['name']}")
    if role in ("pe", "p"):
        lo = ensure_if("lo", "virtual", "loopback: IS-IS passive, BGP source, SRv6 encapsulation source")
        ensure_ip(f"{n['loopback6']}/128", f"{n['name']} loopback", lo)
        dum = ensure_if("dum0", "virtual", f"SRv6 locator {n['locator']} (local SIDs live here; IS-IS passive)")
        ensure_ip(f"{ipaddress.ip_network(n['locator']).network_address + 1}/64", f"{n['name']} locator anchor", dum)
        ensure_ip(f"{n['router_id']}/32", f"{n['name']} BGP router-id", None)
# cables (a cable left with one end, or going to another peer after a re-wiring, is replaced)
def ensure_cable(x, y, label):
    for itf in (x, y):
        cur = requests.get(f"{a.url}/api/dcim/interfaces/{itf.id}/", params={"depth": 1}, headers=H, timeout=30).json().get("cable")
        if not cur: continue
        c = requests.get(f"{a.url}/api/dcim/cables/{cur['id']}/", headers=H, timeout=30)
        if c.status_code == 404: continue
        c = c.json(); ends = {c.get("termination_a_id"), c.get("termination_b_id")}
        if None in ends or ends != {x.id, y.id}:
            requests.delete(f"{a.url}/api/dcim/cables/{cur['id']}/", headers=H, timeout=30); created.append(f"removed cable on {itf.device.name}/{itf.name} (dangling or re-wired)")
    x, y = nb.dcim.interfaces.get(x.id), nb.dcim.interfaces.get(y.id)
    if x.cable or y.cable: return
    nb.dcim.cables.create(termination_a_type="dcim.interface", termination_a_id=x.id, termination_b_type="dcim.interface", termination_b_id=y.id, status=connected.id, label=label)
    created.append(f"cable:{x.device.name}:{x.name}-{y.device.name}:{y.name}")
for l in inv["links"]:
    ensure_cable(ifs[(l["a"], l["a_port"])], ifs[(l["b"], l["b_port"])], l["prefix"])

# ---- VRF device assignments (the per-PE RD lives here) ------------------------------------------------------------
for n in inv["nodes"]:
    if n["role"] not in ("pe", "ce"): continue
    for t in tenants:
        rd = n["rd"].get(t) if n["role"] == "pe" else None
        va = nb.ipam.vrf_device_assignments.get(vrf=vrfs[t].id, device=devs[n["name"]].id)
        if va is None: nb.ipam.vrf_device_assignments.create(vrf=vrfs[t].id, device=devs[n["name"]].id, **({"rd": rd} if rd else {})); created.append(f"vrf {t} on {n['name']}")
        elif rd and (va.rd or "") != rd: va.update({"rd": rd}); created.append(f"rd {rd} on {n['name']} {t}")

# ---- BGP ----------------------------------------------------------------------------------------------------------------
bgp = nb.plugins.bgp
asn = {}
def ensure_asn(num, desc):
    if num not in asn: asn[num] = get_or_create(bgp.autonomous_systems, {"asn": int(num)}, status=active.id, description=desc); ensure(asn[num], description=desc)
    return asn[num]
ensure_asn(SVC["core_as"], "SRv6 core (PEs and route reflectors)")
for n in inv["nodes"]:
    if n["role"] == "ce": ensure_asn(n["asn"], f"{n['name']} ({n['dc']}) — same AS for both tenant VRFs")
    if n["role"] == "ext-ce":
        asn[n["asn"]] = bgp.autonomous_systems.get(asn=n["asn"])
        if asn[n["asn"]] is None: sys.exit(f"AS {n['asn']} of {n['name']} is not in Nautobot — seed the {n['lab']} lab first")
ri = {}
for n in inv["nodes"]:
    if not n.get("asn"): continue
    if n["role"] == "ext-ce":   # routing instance owned by the other lab; peer endpoints are added to it below
        ri[n["name"]] = bgp.routing_instances.get(device=devs[n["name"]].id)
        if ri[n["name"]] is None: sys.exit(f"{n['name']} has no BGP routing instance in Nautobot — seed the {n['lab']} lab first")
        continue
    rid_addr = f"{n['router_id']}/32" if n["role"] in ("pe", "p") else next(pt["ip"] for pt in n["ports"] if pt["peer"] and N[pt["peer"]]["role"] == "host" and pt["tenant"] == "tenant-a")
    rid = nb.ipam.ip_addresses.get(address=rid_addr, namespace=ns.id)
    inst = bgp.routing_instances.get(device=devs[n["name"]].id)
    desc = {"pe": "PE: VPNv4 to the reflectors (SRv6 SIDs), eBGP to the CE in each tenant VRF", "p": "VPNv4 route reflector", "ce": "CE: eBGP to the PE in each tenant VRF (router-id = tenant-a LAN address)"}[n["role"]]
    if inst is None:
        inst = bgp.routing_instances.create(device=devs[n["name"]].id, autonomous_system=asn[n["asn"]].id, router_id=rid.id, status=active.id, description=desc,
                                            extra_attributes={"log_neighbor_changes": True, **({"srv6_locator": "main", "cluster_id": n["router_id"]} if n["name"] in SVC["rrs"] else ({"srv6_locator": "main"} if n["role"] == "pe" else {}))}); created.append(f"bgp-ri:{n['name']}")
    else: ensure(inst, autonomous_system=asn[n["asn"]].id, router_id=rid.id, description=desc)
    ri[n["name"]] = inst
    afs = []
    if n["role"] in ("pe", "p"): afs.append(("vpnv4_unicast", None, {}))
    if n["role"] in ("pe", "ce"): afs += [("ipv4_unicast", t, {"sid_vpn_export": "auto", "rd": n["rd"][t], "route_target": SVC["tenants"][t]["rt"], "redistribute": ["connected"]} if n["role"] == "pe" else {"network": next(pt["prefix"] for pt in n["ports"] if pt["peer"] and pt["tenant"] == t and N[pt["peer"]]["role"] == "host")}) for t in tenants]
    for afi, t, extra in afs:
        af = bgp.address_families.get(routing_instance=inst.id, afi_safi=afi, **({"vrf": vrfs[t].id} if t else {"vrf__isnull": True}))
        if af is None: bgp.address_families.create(routing_instance=inst.id, afi_safi=afi, extra_attributes=extra, **({"vrf": vrfs[t].id} if t else {})); created.append(f"bgp-af:{n['name']} {afi}{' ' + t if t else ''}")
        elif current(af.extra_attributes) != extra: af.update({"extra_attributes": extra}); created.append(f"bgp-af attrs:{n['name']} {afi}{' ' + t if t else ''}")

def ensure_peering(a_name, a_ip, a_role, a_desc, b_name, b_ip, b_role, b_desc, afi, label):
    """One peering with two endpoints (matched by description on the A side), source IPs, roles, AS and address family."""
    ip_a = nb.ipam.ip_addresses.get(address=a_ip, namespace=ns.id); ip_b = nb.ipam.ip_addresses.get(address=b_ip, namespace=ns.id)
    ep_a = next((e for e in bgp.peer_endpoints.filter(routing_instance=ri[a_name].id) if e.description == a_desc), None)
    if ep_a is None:
        peering = bgp.peerings.create(status=active.id); created.append(f"bgp-peering:{label}")
        ep_a = bgp.peer_endpoints.create(peering=peering.id, routing_instance=ri[a_name].id, source_ip=ip_a.id, autonomous_system=asn[N[a_name]["asn"]].id, role=brole[a_role].id, description=a_desc, enabled=True)
        ep_b = bgp.peer_endpoints.create(peering=peering.id, routing_instance=ri[b_name].id, source_ip=ip_b.id, autonomous_system=asn[N[b_name]["asn"]].id, role=brole[b_role].id, description=b_desc, enabled=True)
    else:
        ep_b = ep_a.peer
        ensure(ep_a, source_ip=ip_a.id, autonomous_system=asn[N[a_name]["asn"]].id, role=brole[a_role].id)
        ep_b = bgp.peer_endpoints.get(id=ep_b.id); ensure(ep_b, source_ip=ip_b.id, autonomous_system=asn[N[b_name]["asn"]].id, role=brole[b_role].id, description=b_desc)
    for ep, who in ((ep_a, a_name), (ep_b, b_name)):
        if bgp.peer_endpoint_address_families.get(peer_endpoint=ep.id, afi_safi=afi) is None:
            bgp.peer_endpoint_address_families.create(peer_endpoint=ep.id, afi_safi=afi, extra_attributes={"capability_extended_nexthop": True} if afi == "vpnv4_unicast" else {}); created.append(f"bgp-endpoint-af:{who} {label}")

for pe in [n for n in inv["nodes"] if n["role"] == "pe"]:
    for r in SVC["rrs"]:
        ensure_peering(pe["name"], f"{pe['loopback6']}/128", "rr-client", f"VPNv4 to {r} (route reflector)", r, f"{N[r]['loopback6']}/128", "rr", f"VPNv4 client {pe['name']}", "vpnv4_unicast", f"{pe['name']}<->{r} vpnv4")
    for pt in pe["ports"]:
        if pt["peer"] and N[pt["peer"]]["role"] in ("ce", "ext-ce"):
            ce = N[pt["peer"]]; t = pt["tenant"]; ce_ip = next(x["ip"] for x in ce["ports"] if x["peer"] == pe["name"] and x["tenant"] == t)
            ext = " - SRv6 core attachment" if ce["role"] == "ext-ce" else ""
            ensure_peering(pe["name"], pt["ip"], "pe", f"eBGP {ce['name']} ({t})", ce["name"], ce_ip, "ce", f"eBGP {pe['name']} ({t}){ext}", "ipv4_unicast", f"{pe['name']}<->{ce['name']} {t}")

# stale peerings / prefixes: a detached external CE (no longer in lab.conf) leaves its eBGP peering on the PE, its attachment
# prefix and its address on the other lab's port — remove what this seed created, hand the port back as unwired
wanted_peers = {(pe["name"], N[pt["peer"]]["name"]) for pe in inv["nodes"] if pe["role"] == "pe" for pt in pe["ports"] if pt["peer"]}
for pe in [n for n in inv["nodes"] if n["role"] == "pe"]:
    for ep in list(bgp.peer_endpoints.filter(routing_instance=ri[pe["name"]].id)):
        far = ep.peer and bgp.peer_endpoints.get(id=ep.peer.id); far_dev = far and far.routing_instance and nb.plugins.bgp.routing_instances.get(id=far.routing_instance.id).device.name
        if far_dev and far_dev not in N and (pe["name"], far_dev) not in wanted_peers:
            bgp.peerings.get(id=ep.peering.id).delete(); created.append(f"removed stale peering {pe['name']}<->{far_dev}")
lab_links = {l["prefix"] for l in inv["links"]}
for pf in [x for t in tenants for x in nb.ipam.prefixes.filter(tenant=tenants[t].id, role="attachment-circuit")]:
    if str(pf.prefix) not in lab_links and str(getattr(pf.type, "value", pf.type)) != "container":
        for ip in nb.ipam.ip_addresses.filter(parent=pf.id):
            for x in nb.ipam.ip_address_to_interface.filter(ip_address=ip.id):
                itf = nb.dcim.interfaces.get(id=x.interface.id)
                if itf.device.name not in N: ensure(itf, description="unwired", enabled=False)   # the other lab's port, handed back
                x.delete()
            ip.delete()
        pf.delete(); created.append(f"removed stale attachment prefix {pf.prefix}")

# ---- config context ---------------------------------------------------------------------------------------------------
CTX = {"domain_name": "lab.local", "oob": {"network": OOB["network"], "gateway": OOB["gateway"], "nms": "10.3.0.10"},
       "isis": {"area": SVC["isis_area"], "level": "level-2", "metric_style": "wide", "network": "point-to-point", "bfd": True},
       "srv6": {**SR, "locator_name": "main", "sid_interface": "dum0", "behavior_usid": SR["format"].startswith("usid")},
       "core_mtu": 9000, "route_reflectors": SVC["rrs"], "tenants": {t: {"table": v["table"], "rt": v["rt"]} for t, v in SVC["tenants"].items()}}
ctx = nb.extras.config_contexts.get(name=SITE)
if ctx is None: nb.extras.config_contexts.create(name=SITE, description="SRv6 core lab constants (IS-IS, SRv6 structure, BFD, MTU, OOB, tenant tables)", locations=[site.id], data=CTX); created.append("config-context:srv6-core")
elif current(ctx.data) != CTX: patch(f"extras/config-contexts/{ctx.id}", data=CTX, locations=[site.id]); created.append("config-context updated")

# ---- saved GraphQL query (what nautobot/render.py reads) ---------------------------------------------------------------
QUERY = (LAB / "nautobot" / "srv6-core-model.graphql").read_text()
q = nb.extras.graphql_queries.get(name="srv6-core-model")
if q is None: nb.extras.graphql_queries.create(name="srv6-core-model", query=QUERY); created.append("graphql-query:srv6-core-model")
elif q.query.rstrip() != QUERY.rstrip(): q.update({"query": QUERY}); created.append("graphql-query updated")

print(f"seed complete: {len(created)} changes" + (":\n  " + "\n  ".join(created[:60]) + ("\n  ..." if len(created) > 60 else "") if created else " (already in sync)"))
