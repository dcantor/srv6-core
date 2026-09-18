"""Render the VyOS day-0 configuration of every PE / P / CE from an inventory (the JSON shape of `lab.sh inventory`:
service {core_as, rrs, tenants{name: {table, rt}}}, oob {gateway}, nodes [{name, role, dc, mgmt_ip, loopback6, router_id,
locator, isis_net, asn, pe, rd {tenant: rd}, ports [{name, ip, peer, peer_port, prefix, tenant}]}]). Two producers feed it:
`lab.sh inventory` (tools/gen_configs.py) and Nautobot (nautobot/render.py); their output must be identical."""
import ipaddress


NMS_IP, VM_PORT, VL_SYSLOG_PORT, SFLOW_PORT = "10.3.0.10", 8428, 5514, 6343  # the NMS on the OOB network: VictoriaMetrics (InfluxDB API) and VictoriaLogs (syslog)
TELEGRAF_TOKEN = "srv6core-lab-telegraf".ljust(86, "_") + "=="   # VyOS insists on an InfluxDB-shaped token (86 chars + ==); VictoriaMetrics ignores it


def render_all(inv):
    """{node name: config text} for every VyOS node in the inventory."""
    R = _Renderer(inv); return {n["name"]: R.render(n) for n in inv["nodes"] if n["role"] in ("pe", "p", "ce")}


class _Renderer:
    def __init__(self, inv):
        self.inv = inv; self.SVC = self.inv["service"]; self.NODES = {n["name"]: n for n in self.inv["nodes"]}
        self.PES = [n for n in self.inv["nodes"] if n["role"] == "pe"]; self.RRS = [self.NODES[r] for r in self.SVC["rrs"]]

    def render(self, n):
        return "\n".join({"pe": self.pe, "p": self.p, "ce": self.ce}[n["role"]](n)) + "\n"

    def identity(self, n):
        return [f"# {n['name']}: {n['role'].upper()} in {n['dc']} — day-0 pushed over the serial console by lab.sh bootstrap; rendered by tools/render.py",
                f"set system host-name {n['name']}", "set system domain-name lab.local",
                "set system login user vyos authentication plaintext-password vyos", "set service ssh port 22", "set service lldp interface all",
                f"set interfaces ethernet eth0 address {n['mgmt_ip']}/24", "set interfaces ethernet eth0 description 'OOB management'",
                f"set protocols static route 10.0.0.0/8 next-hop {self.inv['oob']['gateway']}",
                "# monitoring: Prometheus exporters on the OOB address (node-exporter :9100, frr-exporter :9342), scraped from the NMS",
                f"set service monitoring prometheus node-exporter listen-address {n['mgmt_ip']}",
                f"set service monitoring prometheus frr-exporter listen-address {n['mgmt_ip']}",
                "# telemetry pushed by the node itself: VyOS Telegraf -> VictoriaMetrics on the NMS over the InfluxDB v2 write API (host",
                "# metrics, VyOS service state, kernel nstat counters), every series tagged lab / role / dc; syslog -> VictoriaLogs (UDP 5514)",
                f"set service monitoring telegraf influxdb url http://{NMS_IP}", f"set service monitoring telegraf influxdb port {VM_PORT}",
                f"set service monitoring telegraf influxdb bucket {self.inv['lab']}", "set service monitoring telegraf influxdb authentication organization lab",
                f"set service monitoring telegraf influxdb authentication token {TELEGRAF_TOKEN}",
                f"set system syslog remote {NMS_IP} port {VL_SYSLOG_PORT}", f"set system syslog remote {NMS_IP} protocol udp",
                f"set system syslog remote {NMS_IP} facility all level info",
                f"set service monitoring telegraf global-tag lab value {self.inv['lab']}", f"set service monitoring telegraf global-tag role value {n['role']}",
                f"set service monitoring telegraf global-tag dc value {n['dc']}"] + self.sflow(n)

    def sflow(self, n):
        """sFlow (hsflowd) from the core-facing ports of PEs and Ps to the collector on the NMS (goflow2 -> VictoriaLogs):
        the outer IPv6 flows show which uSIDs / paths carry the traffic. Lab traffic is small: sample 1 in 16 packets."""
        core_ports = [p["name"] for p in n["ports"] if p["peer"] and self.NODES[p["peer"]]["role"] in ("pe", "p")]
        if n["role"] not in ("pe", "p") or not core_ports: return []
        return ["# sFlow from the core-facing ports to the NMS collector (goflow2 -> VictoriaLogs): the outer IPv6 flows = SRv6 paths in use",
                f"set system sflow agent-address {n['mgmt_ip']}", f"set system sflow server {NMS_IP} port {SFLOW_PORT}",
                "set system sflow sampling-rate 16", "set system sflow polling 20"] + [f"set system sflow interface {p}" for p in core_ports]


    def core_ports(self, n):
        return [p for p in n["ports"] if p["peer"] and self.NODES[p["peer"]]["role"] in ("pe", "p")]


    def underlay(self, n):
        """Loopback, core links (jumbo), the SRv6 locator on dum0, IS-IS level-2 with SRv6, seg6 enabled on the core links."""
        out = ["# underlay: IPv6-only core, IS-IS level-2 point-to-point, jumbo frames leave headroom for the SRv6 encapsulation",
               f"set interfaces loopback lo address {n['loopback6']}/128"]
        for p in self.core_ports(n):
            out += [f"set interfaces ethernet {p['name']} address {p['ip']}", f"set interfaces ethernet {p['name']} description 'core: {p['peer']} {p['peer_port']}'",
                    f"set interfaces ethernet {p['name']} mtu 9000"]
        loc = ipaddress.ip_network(n["locator"]); sr = self.SVC["srv6"]; usid = sr["format"].startswith("usid")
        out += [f"# SRv6: the locator ({sr['format']}: block {sr['block_len']} / node {sr['node_len']} / function {sr['func_bits']} bits); dum0 is where FRR installs",
                "# the local SIDs — addressed with a /128 so the connected route never outranks the locator's own End SID in zebra",
                f"set interfaces dummy dum0 address {loc.network_address + 1}/128", f"set interfaces dummy dum0 description 'SRv6 locator {n['locator']} (local SIDs)'",
                f"set protocols segment-routing srv6 locator main prefix {n['locator']}", f"set protocols segment-routing srv6 locator main block-len {sr['block_len']}",
                f"set protocols segment-routing srv6 locator main node-len {sr['node_len']}", f"set protocols segment-routing srv6 locator main func-bits {sr['func_bits']}",
                f"set protocols segment-routing srv6 locator main format {sr['format']}"] + (["set protocols segment-routing srv6 locator main behavior-usid"] if usid else []) + [
                f"set protocols segment-routing srv6 encapsulation source-address {n['loopback6']}"]
        out += [f"set protocols segment-routing interface {p['name']} srv6" for p in self.core_ports(n)]
        out += ["set system sysctl parameter net.ipv6.conf.all.seg6_enabled value 1",
                f"set protocols isis net {n['isis_net']}", "set protocols isis level level-2", "set protocols isis metric-style wide", "set protocols isis log-adjacency-changes"]
        out += [f"set protocols isis interface {p['name']} network point-to-point" for p in self.core_ports(n)]
        # BFD on every core adjacency: the UDP-tunnel links never lose carrier, so a dead neighbour is only seen through the
        # protocol; BFD (300 ms x 3) turns the 30 s IS-IS hold time into ~1 s of loss on a failure
        out += [f"set protocols isis interface {p['name']} bfd" for p in self.core_ports(n)]
        out += ["set protocols isis interface lo passive", "set protocols isis interface dum0 passive",
                "set protocols isis segment-routing srv6 locator main", "set protocols isis segment-routing srv6 interface dum0"]
        return out


    def shortest_first_hops(self, src):
        """IGP view from `src`: for every other core node, the attached P routers that lie on a shortest path (all links cost 10).
        Used to leak the remote locators into the tenant VRFs with the same next hops IS-IS would pick."""
        import heapq
        adj = {}
        for x in self.inv["nodes"]:
            if x["role"] in ("pe", "p"): adj[x["name"]] = [p["peer"] for p in self.core_ports(x)]
        dist = {src: 0}; first = {src: set()}; heap = [(0, src)]
        while heap:
            d, u = heapq.heappop(heap)
            if d > dist.get(u, 1e9): continue
            for v in adj[u]:
                nd = d + 10; fh = {v} if u == src else first[u]
                if nd < dist.get(v, 1e9): dist[v] = nd; first[v] = set(fh); heapq.heappush(heap, (nd, v))
                elif nd == dist[v]: first[v] |= fh
        return {node: sorted(f) for node, f in first.items() if node != src}


    def pe(self, n):
        """One VRF per tenant: attachment circuit to the CE, eBGP to it, VPNv4 export/import with its own End.DT4 SID."""
        attached_ps = sorted({p["peer"] for p in self.core_ports(n) if self.NODES[p["peer"]]["role"] == "p"})
        block = ipaddress.ip_network(self.SVC["srv6"]["block"])   # the SRv6 block all locators are carved from
        first_hops = self.shortest_first_hops(n["name"])
        out = self.identity(n) + self.underlay(n) + [
            f"# BGP: VPNv4 to the route reflectors {', '.join(r['name'] for r in self.RRS)} over the IPv6 loopbacks (extended next hop), SRv6 SIDs from locator main",
            f"set protocols bgp system-as {self.SVC['core_as']}", f"set protocols bgp parameters router-id {n['router_id']}", "set protocols bgp parameters log-neighbor-changes",
            "set protocols bgp srv6 locator main"]
        for r in self.RRS:
            out += [f"set protocols bgp neighbor {r['loopback6']} remote-as {self.SVC['core_as']}", f"set protocols bgp neighbor {r['loopback6']} description '{r['name']} route reflector'",
                    f"set protocols bgp neighbor {r['loopback6']} update-source {n['loopback6']}", f"set protocols bgp neighbor {r['loopback6']} capability extended-nexthop",
                    f"set protocols bgp neighbor {r['loopback6']} address-family ipv4-vpn", f"set protocols bgp neighbor {r['loopback6']} address-family ipv6-vpn"]
        for ce_port in [p for p in n["ports"] if p["peer"] and self.NODES[p["peer"]]["role"] in ("ce", "ext-ce")]:
            vrf = ce_port["tenant"]; t = self.SVC["tenants"][vrf]; ce = self.NODES[ce_port["peer"]]
            me = ipaddress.ip_interface(ce_port["ip"]); net = me.network.network_address
            ce_ip = str(net + 2 if me.ip == net + 1 else net + 1); rd = n["rd"][vrf]   # the other host of the /30 (an external CE is the first end)
            ext = " (external CE, another lab's router)" if ce["role"] == "ext-ce" else ""
            ce_ip6 = None
            if ce_port.get("ip6"):
                me6 = ipaddress.ip_interface(ce_port["ip6"]); net6 = me6.network.network_address; ce_ip6 = str(net6 + 2 if me6.ip == net6 + 1 else net6 + 1)
            out += [f"# tenant VRF {vrf} (table {t['table']}, RT {t['rt']}, RD {rd}): attachment circuit {ce_port['name']} to {ce['name']} {ce_port['peer_port']}{ext}, dual-stack",
                    f"set vrf name {vrf} table {t['table']}", f"set interfaces ethernet {ce_port['name']} vrf {vrf}", f"set interfaces ethernet {ce_port['name']} address {ce_port['ip']}"] + (
                    [f"set interfaces ethernet {ce_port['name']} address {ce_port['ip6']}"] if ce_port.get("ip6") else []) + [
                    f"set interfaces ethernet {ce_port['name']} description '{vrf}: {ce['name']} {ce_port['peer_port']}'",
                    f"# Linux scopes the SRv6 encapsulation's outer lookup to the ingress VRF for forwarded packets: leak every remote locator into",
                    f"# the VRF table via the attached P router(s) on the IGP shortest path (recursive through IS-IS, so a dead P drops out), and the",
                    f"# whole block via every attached P as the fallback"] + [
                    f"set vrf name {vrf} protocols static route6 {self.NODES[d]['locator']} next-hop {self.NODES[x]['loopback6']} vrf default"
                    for d, hops in sorted(first_hops.items()) if self.NODES[d].get("locator") for x in hops] + [
                    f"set vrf name {vrf} protocols static route6 {block} next-hop {self.NODES[x]['loopback6']} vrf default" for x in attached_ps] + [
                    f"# eBGP to the CE, one session per address family; one SRv6 End.DT46 SID per VRF carries both (sid vpn per-vrf export auto)",
                    f"set vrf name {vrf} protocols bgp system-as {self.SVC['core_as']}", f"set vrf name {vrf} protocols bgp parameters router-id {n['router_id']}",
                    f"set vrf name {vrf} protocols bgp parameters log-neighbor-changes",
                    f"set vrf name {vrf} protocols bgp sid vpn per-vrf export auto",
                    f"set vrf name {vrf} protocols bgp neighbor {ce_ip} remote-as {ce['asn']}", f"set vrf name {vrf} protocols bgp neighbor {ce_ip} description '{ce['name']} ({vrf})'",
                    f"set vrf name {vrf} protocols bgp neighbor {ce_ip} address-family ipv4-unicast"] + ([
                    f"set vrf name {vrf} protocols bgp neighbor {ce_ip6} remote-as {ce['asn']}", f"set vrf name {vrf} protocols bgp neighbor {ce_ip6} description '{ce['name']} ({vrf}, IPv6)'",
                    f"set vrf name {vrf} protocols bgp neighbor {ce_ip6} address-family ipv6-unicast"] if ce_ip6 else []) + [
                    l for af in (["ipv4-unicast"] + (["ipv6-unicast"] if ce_ip6 else [])) for l in (
                    f"set vrf name {vrf} protocols bgp address-family {af} redistribute connected",
                    f"set vrf name {vrf} protocols bgp address-family {af} rd vpn export {rd}",
                    f"set vrf name {vrf} protocols bgp address-family {af} route-target vpn both {t['rt']}",
                    f"set vrf name {vrf} protocols bgp address-family {af} import vpn", f"set vrf name {vrf} protocols bgp address-family {af} export vpn")]
        seen, dedup = set(), []   # a VRF with two attachment circuits repeats its block: keep the first occurrence of each set line
        for l in out:
            if l.startswith("set") and l in seen: continue
            seen.add(l); dedup.append(l)
        return dedup


    def p(self, n):
        out = self.identity(n) + self.underlay(n)
        if n["name"] in self.SVC["rrs"]:
            out += [f"# VPNv4 + VPNv6 route reflector for the PEs (no VRFs here; p routers only forward IPv6); the PEs peer with every reflector",
                    f"set protocols bgp system-as {self.SVC['core_as']}", f"set protocols bgp parameters router-id {n['router_id']}", f"set protocols bgp parameters cluster-id {n['router_id']}",
                    "set protocols bgp parameters log-neighbor-changes",
                    f"set protocols bgp peer-group RR-CLIENTS remote-as {self.SVC['core_as']}", f"set protocols bgp peer-group RR-CLIENTS update-source {n['loopback6']}",
                    "set protocols bgp peer-group RR-CLIENTS capability extended-nexthop", "set protocols bgp peer-group RR-CLIENTS address-family ipv4-vpn route-reflector-client",
                    "set protocols bgp peer-group RR-CLIENTS address-family ipv6-vpn route-reflector-client"]
            for x in self.PES:
                out += [f"set protocols bgp neighbor {x['loopback6']} peer-group RR-CLIENTS", f"set protocols bgp neighbor {x['loopback6']} description '{x['name']}'"]
        return out


    def ce(self, n):
        """Per tenant: its own VRF on the CE (`vrf name <tenant>`) holding the attachment circuit to the PE and the site LAN, with an
        eBGP session announcing the LAN — the CE's default VRF carries only management, so the tenants never meet on the CE either."""
        out = self.identity(n) + ["# VyOS/FRR insist on a default-VRF BGP instance while VRF instances exist: an empty one (no neighbours, no networks)",
                             f"set protocols bgp system-as {n['asn']}"]
        tenants = sorted({p["tenant"] for p in n["ports"] if p.get("tenant")})
        for i, vrf in enumerate(tenants):
            pe_port = next(p for p in n["ports"] if p.get("tenant") == vrf and self.NODES[p["peer"]]["role"] == "pe")
            lan = next(p for p in n["ports"] if p.get("tenant") == vrf and self.NODES[p["peer"]]["role"] == "host")
            pe_ip = str(ipaddress.ip_interface(pe_port["ip"]).network.network_address + 1); lan_net = ipaddress.ip_interface(lan["ip"]).network
            pe_ip6 = str(ipaddress.ip_interface(pe_port["ip6"]).network.network_address + 1) if pe_port.get("ip6") else None
            lan_net6 = ipaddress.ip_interface(lan["ip6"]).network if lan.get("ip6") else None
            v = f"vrf name {vrf} "   # prefix for everything that lives in the tenant VRF
            out += [f"# {vrf}: VRF {vrf} on the CE holds the attachment circuit to {pe_port['peer']} and the {n['dc']} LAN (dual-stack: one eBGP session per family)",
                    f"set vrf name {vrf} table {self.SVC['tenants'][vrf]['table']}", f"set interfaces ethernet {pe_port['name']} vrf {vrf}", f"set interfaces ethernet {lan['name']} vrf {vrf}",f"set interfaces ethernet {pe_port['name']} address {pe_port['ip']}"] + (
                    [f"set interfaces ethernet {pe_port['name']} address {pe_port['ip6']}"] if pe_port.get("ip6") else []) + [f"set interfaces ethernet {pe_port['name']} description '{pe_port['peer']} {pe_port['peer_port']} ({vrf})'",
                    f"set interfaces ethernet {lan['name']} address {lan['ip']}"] + ([f"set interfaces ethernet {lan['name']} address {lan['ip6']}"] if lan.get("ip6") else []) + [f"set interfaces ethernet {lan['name']} description '{n['dc']} LAN {vrf}: {lan['peer']}'",
                    f"set {v}protocols bgp system-as {n['asn']}", f"set {v}protocols bgp parameters router-id {lan_net.network_address + 1}", f"set {v}protocols bgp parameters log-neighbor-changes",
                    f"set {v}protocols bgp neighbor {pe_ip} remote-as {self.SVC['core_as']}", f"set {v}protocols bgp neighbor {pe_ip} description '{pe_port['peer']} ({vrf})'",
                    f"set {v}protocols bgp neighbor {pe_ip} address-family ipv4-unicast", f"set {v}protocols bgp address-family ipv4-unicast network {lan_net}"] + ([
                    f"set {v}protocols bgp neighbor {pe_ip6} remote-as {self.SVC['core_as']}", f"set {v}protocols bgp neighbor {pe_ip6} description '{pe_port['peer']} ({vrf}, IPv6)'",
                    f"set {v}protocols bgp neighbor {pe_ip6} address-family ipv6-unicast", f"set {v}protocols bgp address-family ipv6-unicast network {lan_net6}"] if pe_ip6 else [])
        return out
