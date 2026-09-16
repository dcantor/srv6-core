#!/usr/bin/env python3
"""Render the VyOS day-0 configuration of every PE / P / CE from lab.conf (via `lab.sh inventory`) into
nodes/<node>/vyos_config.txt. Idempotent; run it after changing lab.conf. The files are what
`lab.sh bootstrap` pushes over the serial console on first boot."""
import ipaddress, json, subprocess, sys
from pathlib import Path

LAB_DIR = Path(__file__).resolve().parents[1]
inv = json.loads(subprocess.run([str(LAB_DIR / "lab.sh"), "inventory"], capture_output=True, text=True, check=True).stdout)
SVC = inv["service"]; NODES = {n["name"]: n for n in inv["nodes"]}
PES = [n for n in inv["nodes"] if n["role"] == "pe"]
RR = NODES[SVC["rr"]]


def identity(n):
    return [f"# {n['name']}: {n['role'].upper()} in {n['dc']} — day-0 pushed over the serial console by lab.sh bootstrap; rendered by tools/gen_configs.py",
            f"set system host-name {n['name']}", "set system domain-name lab.local",
            "set system login user vyos authentication plaintext-password vyos", "set service ssh port 22", "set service lldp interface all",
            f"set interfaces ethernet eth0 address {n['mgmt_ip']}/24", "set interfaces ethernet eth0 description 'OOB management'",
            f"set protocols static route 10.0.0.0/8 next-hop {inv['oob']['gateway']}"]


def core_ports(n):
    return [p for p in n["ports"] if p["peer"] and NODES[p["peer"]]["role"] in ("pe", "p")]


def underlay(n):
    """Loopback, core links (jumbo), the SRv6 locator on dum0, IS-IS level-2 with SRv6, seg6 enabled on the core links."""
    out = ["# underlay: IPv6-only core, IS-IS level-2 point-to-point, jumbo frames leave headroom for the SRv6 encapsulation",
           f"set interfaces loopback lo address {n['loopback6']}/128"]
    for p in core_ports(n):
        out += [f"set interfaces ethernet {p['name']} address {p['ip']}", f"set interfaces ethernet {p['name']} description 'core: {p['peer']} {p['peer_port']}'",
                f"set interfaces ethernet {p['name']} mtu 9000"]
    loc = ipaddress.ip_network(n["locator"])
    out += ["# SRv6: the locator; dum0 carries the local SIDs (FRR installs them there) and, advertised passively by IS-IS, keeps the",
            "# locator reachable even when the IS-IS SRv6 sub-TLVs are not understood by a neighbour",
            f"set interfaces dummy dum0 address {loc.network_address + 1}/{loc.prefixlen}", f"set interfaces dummy dum0 description 'SRv6 locator {n['locator']} (local SIDs)'",
            f"set protocols segment-routing srv6 locator main prefix {n['locator']}", "set protocols segment-routing srv6 locator main block-len 40",
            "set protocols segment-routing srv6 locator main node-len 24", "set protocols segment-routing srv6 locator main func-bits 16",
            f"set protocols segment-routing srv6 encapsulation source-address {n['loopback6']}"]
    out += [f"set protocols segment-routing interface {p['name']} srv6" for p in core_ports(n)]
    out += ["set system sysctl parameter net.ipv6.conf.all.seg6_enabled value 1",
            f"set protocols isis net {n['isis_net']}", "set protocols isis level level-2", "set protocols isis metric-style wide", "set protocols isis log-adjacency-changes"]
    out += [f"set protocols isis interface {p['name']} network point-to-point" for p in core_ports(n)]
    out += ["set protocols isis interface lo passive", "set protocols isis interface dum0 passive",
            "set protocols isis segment-routing srv6 locator main", "set protocols isis segment-routing srv6 interface dum0"]
    return out


def pe(n):
    ce_port = next(p for p in n["ports"] if p["peer"] and NODES[p["peer"]]["role"] == "ce"); ce = NODES[ce_port["peer"]]
    ce_ip = str(ipaddress.ip_interface(ce_port["ip"]).network.network_address + 2); vrf = SVC["vrf"]; idx = n["idx"]
    return identity(n) + underlay(n) + [
        f"# customer VRF {vrf}: attachment circuit to {ce['name']}",
        f"set vrf name {vrf} table {SVC['table']}", f"set interfaces ethernet {ce_port['name']} vrf {vrf}", f"set interfaces ethernet {ce_port['name']} address {ce_port['ip']}",
        f"set interfaces ethernet {ce_port['name']} description '{vrf}: {ce['name']} {ce_port['peer_port']}'",
        f"# BGP: VPNv4 to the route reflector {RR['name']} over the IPv6 loopbacks (extended next hop), SRv6 SIDs from locator main",
        f"set protocols bgp system-as {SVC['core_as']}", f"set protocols bgp parameters router-id {n['router_id']}", "set protocols bgp parameters log-neighbor-changes",
        "set protocols bgp srv6 locator main",
        f"set protocols bgp neighbor {RR['loopback6']} remote-as {SVC['core_as']}", f"set protocols bgp neighbor {RR['loopback6']} description '{RR['name']} route reflector'",
        f"set protocols bgp neighbor {RR['loopback6']} update-source {n['loopback6']}", f"set protocols bgp neighbor {RR['loopback6']} capability extended-nexthop",
        f"set protocols bgp neighbor {RR['loopback6']} address-family ipv4-vpn",
        f"# BGP in the VRF: eBGP to the CE; export/import with an SRv6 End.DT4 SID (sid vpn export auto)",
        f"set vrf name {vrf} protocols bgp system-as {SVC['core_as']}", f"set vrf name {vrf} protocols bgp parameters router-id {n['router_id']}",
        f"set vrf name {vrf} protocols bgp neighbor {ce_ip} remote-as {ce['asn']}", f"set vrf name {vrf} protocols bgp neighbor {ce_ip} description '{ce['name']}'",
        f"set vrf name {vrf} protocols bgp neighbor {ce_ip} address-family ipv4-unicast",
        f"set vrf name {vrf} protocols bgp address-family ipv4-unicast redistribute connected",
        f"set vrf name {vrf} protocols bgp address-family ipv4-unicast rd vpn export {SVC['core_as']}:{idx}",
        f"set vrf name {vrf} protocols bgp address-family ipv4-unicast route-target vpn both {SVC['rt']}",
        f"set vrf name {vrf} protocols bgp address-family ipv4-unicast sid vpn export auto",
        f"set vrf name {vrf} protocols bgp address-family ipv4-unicast import vpn", f"set vrf name {vrf} protocols bgp address-family ipv4-unicast export vpn"]


def p(n):
    out = identity(n) + underlay(n)
    if n["name"] == RR["name"]:
        out += [f"# VPNv4 route reflector for the PEs (no VRFs here; p routers only forward IPv6)",
                f"set protocols bgp system-as {SVC['core_as']}", f"set protocols bgp parameters router-id {n['router_id']}", f"set protocols bgp parameters cluster-id {n['router_id']}",
                "set protocols bgp parameters log-neighbor-changes",
                f"set protocols bgp peer-group RR-CLIENTS remote-as {SVC['core_as']}", f"set protocols bgp peer-group RR-CLIENTS update-source {n['loopback6']}",
                "set protocols bgp peer-group RR-CLIENTS capability extended-nexthop", "set protocols bgp peer-group RR-CLIENTS address-family ipv4-vpn route-reflector-client"]
        for x in PES:
            out += [f"set protocols bgp neighbor {x['loopback6']} peer-group RR-CLIENTS", f"set protocols bgp neighbor {x['loopback6']} description '{x['name']}'"]
    return out


def ce(n):
    pe_port = next(p for p in n["ports"] if p["peer"] and NODES[p["peer"]]["role"] == "pe"); lan = next(p for p in n["ports"] if p["peer"] and NODES[p["peer"]]["role"] == "host")
    pe_ip = str(ipaddress.ip_interface(pe_port["ip"]).network.network_address + 1); lan_net = ipaddress.ip_interface(lan["ip"]).network
    return identity(n) + [
        f"# attachment circuit to {pe_port['peer']} (VRF {SVC['vrf']} on the PE side) and the {n['dc']} LAN",
        f"set interfaces ethernet {pe_port['name']} address {pe_port['ip']}", f"set interfaces ethernet {pe_port['name']} description '{pe_port['peer']} {pe_port['peer_port']} ({SVC['vrf']})'",
        f"set interfaces ethernet {lan['name']} address {lan['ip']}", f"set interfaces ethernet {lan['name']} description '{n['dc']} LAN: {lan['peer']}'",
        f"# eBGP to the PE: announce the LAN, learn the other data centres",
        f"set protocols bgp system-as {n['asn']}", f"set protocols bgp parameters router-id {n['router_id']}",
        f"set protocols bgp neighbor {pe_ip} remote-as {SVC['core_as']}", f"set protocols bgp neighbor {pe_ip} description '{pe_port['peer']}'",
        f"set protocols bgp neighbor {pe_ip} address-family ipv4-unicast", f"set protocols bgp address-family ipv4-unicast network {lan_net}"]


RENDER = {"pe": pe, "p": p, "ce": ce}
changed = 0
for n in inv["nodes"]:
    if n["role"] not in RENDER: continue
    path = LAB_DIR / "nodes" / n["name"] / "vyos_config.txt"; path.parent.mkdir(parents=True, exist_ok=True)
    text = "\n".join(RENDER[n["role"]](n)) + "\n"
    if not path.exists() or path.read_text() != text: path.write_text(text); changed += 1; print(f"wrote {path.relative_to(LAB_DIR)} ({text.count(chr(10))} lines)")
print(f"{changed} file(s) changed")
