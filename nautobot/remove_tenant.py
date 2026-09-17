#!/usr/bin/env python3
"""Remove a tenant from Nautobot: its PE<->CE peerings and VRF address families, VRF device assignments, the hosts (devices)
of the tenant, every address and prefix of the tenant, the VRF, its route target and the Tenant object. The PE / CE
interfaces stay (seed.py re-describes them as unwired).      NAUTOBOT_TOKEN=... remove_tenant.py <name>"""
import argparse, os, sys
import pynautobot, requests

p = argparse.ArgumentParser(); p.add_argument("name"); p.add_argument("--url", default=os.environ.get("NAUTOBOT_URL", "http://10.0.0.10:8080")); p.add_argument("--token", default=os.environ.get("NAUTOBOT_TOKEN"))
a = p.parse_args(); nb = pynautobot.api(a.url, token=a.token); H = {"Authorization": f"Token {a.token}", "Accept": "application/json"}
t = a.name; removed = []
tenant = nb.tenancy.tenants.get(name=t); vrf = nb.ipam.vrfs.get(name=t)
bgp = nb.plugins.bgp
if vrf:
    for af in bgp.address_families.filter(vrf=vrf.id): af.delete(); removed.append("bgp address family")
    for va in nb.ipam.vrf_device_assignments.filter(vrf=vrf.id): va.delete(); removed.append("vrf device assignment")
for ep in bgp.peer_endpoints.all():
    if ep.description and ep.description.endswith(f"({t})"):
        peering = ep.peering
        try: bgp.peerings.get(id=peering.id).delete(); removed.append("bgp peering")
        except Exception: pass   # already gone with its other endpoint
if tenant:
    host_names = {ip.interfaces[0].device.name for ip in nb.ipam.ip_addresses.filter(tenant=tenant.id) if getattr(ip, "interfaces", None)}   # hosts: their LAN address carries the tenant
    r = requests.post(f"{a.url}/api/graphql/", json={"query": '{ ip_addresses(tenant: ["%s"]) { interfaces { device { name role { name } } } } }' % t}, headers=H, timeout=60).json()
    host_names |= {i["device"]["name"] for ip in r["data"]["ip_addresses"] for i in ip["interfaces"] if i["device"]["role"]["name"] == "host"}
    hosts = [d for d in nb.dcim.devices.filter(role="host") if d.name in host_names]
    for d in hosts: d.delete(); removed.append(f"device {d.name}")
    for ip in nb.ipam.ip_addresses.filter(tenant=tenant.id): ip.delete(); removed.append("ip address")
    for pf in sorted(nb.ipam.prefixes.filter(tenant=tenant.id), key=lambda x: -int(str(x.prefix).split("/")[1])): pf.delete(); removed.append(f"prefix {pf.prefix}")
if vrf: vrf.delete(); removed.append("vrf")
rt = nb.ipam.route_targets.filter(tenant=tenant.id) if tenant else []
for x in rt: x.delete(); removed.append("route target")
if tenant: tenant.delete(); removed.append("tenant")
print(f"removed {t}: {len(removed)} objects" + (" — " + ", ".join(sorted(set(removed))) if removed else " (nothing found)"))
