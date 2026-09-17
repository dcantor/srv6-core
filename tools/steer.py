#!/usr/bin/env python3
"""Explicit-path SRv6 steering: pin a tenant prefix on a source PE to a segment list through chosen P routers instead of
the IGP shortest path. The policy is a static route in the tenant VRF whose SID list is the End SID of every listed
P router followed by the End.DT4 SID of the PE that owns the prefix (read live, since BGP allocates it).

   steer.py add  <src-pe> <tenant> <prefix> <p> [<p> ...]     e.g. steer.py add pe1 tenant-b 172.21.3.0/24 p1 p3
                 with uSID locators (lab.conf SRV6_FORMAT=usid-*) the whole path is packed into ONE segment — the carrier
                 <block>:<uN p1>:<uN p3>:<uN pe3>:<uDT4 fn>:: — add --uncompressed to install the classic list instead
   steer.py del  <src-pe> <tenant> <prefix>
   steer.py show [<pe> ...]                                    the policies present on the PEs (kernel view)
   steer.py sid  <pe> <tenant>                                 print the End.DT4 SID of a tenant VRF on a PE"""
import ipaddress, json, os, re, subprocess, sys
from pathlib import Path
from netmiko import ConnectHandler

LAB_DIR = Path(__file__).resolve().parents[1]
inv = json.loads(subprocess.run([str(LAB_DIR / "lab.sh"), "inventory"], capture_output=True, text=True, check=True).stdout)
N = {n["name"]: n for n in inv["nodes"]}
CREDS = dict(username=os.environ.get("VYOS_USERNAME", "vyos"), password=os.environ.get("VYOS_PASSWORD", "vyos"))


def conn(node): return ConnectHandler(device_type="vyos", host=N[node]["mgmt_ip"], **CREDS)


def dt4_sid(pe, tenant):
    """The End.DT4 SID BGP allocated for a tenant VRF on a PE (from `show bgp segment-routing srv6`)."""
    c = conn(pe); out = c.send_command("show bgp segment-routing srv6"); c.disconnect()
    m = re.search(rf"- name: {re.escape(tenant)}\n\s+vpn_policy\[AFI_IP\]\.tovpn_sid: ([0-9a-f:]+)", out)
    if not m: sys.exit(f"{pe}: no End.DT4 SID for {tenant}")
    return m[1]


def owner_pe(tenant, prefix):
    """The PE behind which a tenant prefix lives (the CE announcing that LAN)."""
    net = ipaddress.ip_network(prefix)
    for n in inv["nodes"]:
        if n["role"] != "ce": continue
        for p in n["ports"]:
            if p["peer"] and p.get("tenant") == tenant and N[p["peer"]]["role"] == "host" and ipaddress.ip_network(p["prefix"]) == net: return n["pe"]
    sys.exit(f"no CE announces {prefix} in {tenant}")


def core_iface(src_pe, first_p):
    port = next((p for p in N[src_pe]["ports"] if p["peer"] == first_p), None)
    if not port: sys.exit(f"{src_pe} is not attached to {first_p}; its P routers are {[p['peer'] for p in N[src_pe]['ports'] if p['peer'] and N[p['peer']]['role'] == 'p']}")
    return port["name"]


def usid_carrier(sr, ps, dt4_sid):
    """Pack the path into one 128-bit segment (usid-f3216): the 32-bit block, then one 16-bit uN per P router, then the
    destination PE's uN and its uDT4 function, zero-padded. Each uN owner shifts the address left by 16 bits (NEXT-C-SID)."""
    hx = lambda a: ipaddress.IPv6Address(a).exploded.split(":")
    block = hx(dt4_sid)[:2]                       # fd00:000c
    nodes = [hx(ipaddress.ip_network(N[p]["locator"]).network_address)[2] for p in ps]
    dst = hx(dt4_sid)[2:4]                        # <uN pe>:<function>
    slots = nodes + dst
    if len(slots) > 6: sys.exit(f"path too long for one carrier ({len(slots)} micro-SIDs, 6 fit in a 128-bit segment with a 32-bit block)")
    return str(ipaddress.IPv6Address(":".join(block + slots + ["0"] * (6 - len(slots)))))


def path(src_pe, tenant, prefix):
    return f"vrf name {tenant} protocols static route {prefix}"


cmd = sys.argv[1] if len(sys.argv) > 1 else "help"
if cmd == "add":
    src, tenant, prefix, ps = sys.argv[2], sys.argv[3], sys.argv[4], sys.argv[5:]
    uncompressed = "--uncompressed" in ps; ps = [p for p in ps if not p.startswith("--")]
    for p in ps:
        if N.get(p, {}).get("role") != "p": sys.exit(f"{p} is not a P router")
    dst = owner_pe(tenant, prefix); sid = dt4_sid(dst, tenant); iface = core_iface(src, ps[0])
    segs = [str(ipaddress.ip_network(N[p]["locator"]).network_address) for p in ps] + [sid]
    sr = inv["service"]["srv6"]
    if sr["format"].startswith("usid") and not uncompressed:
        segs = [usid_carrier(sr, ps, sid)]
    c = conn(src)
    out = c.send_config_set([f"set {path(src, tenant, prefix)} interface {iface} vrf default", f"set {path(src, tenant, prefix)} interface {iface} segments {'/'.join(segs)}", "commit", "save"],
                            exit_config_mode=True, cmd_verify=False, read_timeout=120)
    if re.search(r"Invalid|failed", out): sys.exit(out[-500:])
    print(f"{src}: {tenant} {prefix} -> {' -> '.join(ps)} -> {dst}  segments {' / '.join(segs)}  (out {iface})" + ("  [uSID: one compressed segment]" if len(segs) == 1 and len(ps) >= 1 and inv["service"]["srv6"]["format"].startswith("usid") and not uncompressed else ""))
    print(c.send_command(f"sudo ip -c=never route show vrf {tenant} {prefix}")); c.disconnect()
elif cmd == "del":
    src, tenant, prefix = sys.argv[2], sys.argv[3], sys.argv[4]
    c = conn(src); out = c.send_config_set([f"delete {path(src, tenant, prefix)}", "commit", "save"], exit_config_mode=True, cmd_verify=False, read_timeout=120)
    if re.search(r"Invalid|failed", out): sys.exit(out[-500:])
    print(f"{src}: {tenant} {prefix} back on the IGP shortest path"); print(c.send_command(f"sudo ip -c=never route show vrf {tenant} {prefix}")); c.disconnect()
elif cmd == "show":
    for pe in (sys.argv[2:] or [n["name"] for n in inv["nodes"] if n["role"] == "pe"]):
        c = conn(pe); lines = []
        for t in inv["service"]["tenants"]:
            for l in c.send_command(f"sudo ip -c=never route show vrf {t}").splitlines():
                if "seg6" in l and "proto static" in l: lines.append(f"  {t}: {l.strip()}")
        c.disconnect(); print(f"{pe}:" + ("\n" + "\n".join(lines) if lines else " no steering policies"))
elif cmd == "sid":
    print(dt4_sid(sys.argv[2], sys.argv[3]))
else:
    sys.exit(__doc__)
