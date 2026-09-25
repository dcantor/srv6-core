"""Render the VyOS day-0 configuration of every PE / P / CE from an inventory (the JSON shape of `lab.sh inventory`:
service {core_as, rrs, tenants{name: {table, rt}}}, oob {gateway}, nodes [{name, role, dc, mgmt_ip, loopback6, router_id,
locator, isis_net, asn, pe, rd {tenant: rd}, ports [{name, ip, peer, peer_port, prefix, tenant}]}]). Two producers feed it:
`lab.sh inventory` (tools/gen_configs.py) and Nautobot (nautobot/render.py); their output must be identical."""
import ipaddress


NMS_IP, VM_PORT, VL_SYSLOG_PORT, SFLOW_PORT = "10.3.0.10", 8428, 5514, 6343  # the NMS on the OOB network: VictoriaMetrics (InfluxDB API) and VictoriaLogs (syslog)
TELEGRAF_TOKEN = "srv6core-lab-telegraf".ljust(86, "_") + "=="   # VyOS insists on an InfluxDB-shaped token (86 chars + ==); VictoriaMetrics ignores it
# The routers' own HTTPS API (VyOS `service https api`): what the BGP looking glass calls to read their tables — the RIB,
# the per-VRF BGP tables and the IS-IS adjacencies — instead of scraping a terminal. Reachable only from the OOB
# addresses of the looking glass and the lab host (`allow-client`); a lab key, like the vyos/vyos logins.
API_KEY = "srv6core-lab-looking-glass"
API_CLIENTS = ("10.3.0.70", "10.3.0.1")          # the looking glass and the host (lab.sh / the test suites)


def render_all(inv):
    """{node name: config text} for every VyOS node in the inventory."""
    R = _Renderer(inv); return {n["name"]: R.render(n) for n in inv["nodes"] if n["role"] in ("pe", "p", "ce", "fw")}


class _Renderer:
    def __init__(self, inv):
        self.inv = inv; self.SVC = self.inv["service"]; self.NODES = {n["name"]: n for n in self.inv["nodes"]}
        self.PES = [n for n in self.inv["nodes"] if n["role"] == "pe"]; self.RRS = [self.NODES[r] for r in self.SVC["rrs"]]

    def render(self, n):
        return "\n".join({"pe": self.pe, "p": self.p, "ce": self.ce, "fw": self.fw}[n["role"]](n)) + "\n"

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
                f"set service monitoring telegraf global-tag dc value {n['dc']}",
                "# the router's own HTTPS API (/show, /retrieve, /ping, /traceroute): what the BGP looking glass reads the",
                "# RIB, the per-VRF BGP tables and the IS-IS adjacencies through — a supported interface rather than a",
                "# scraped terminal, and every answer already JSON. Only the looking glass and the host may call it.",
                "set service https api rest", f"set service https api keys id lg key {API_KEY}"] + [
                f"set service https allow-client address {c}" for c in API_CLIENTS] + self.sflow(n)

    def sflow(self, n):
        """sFlow (hsflowd) from the P routers' ports to the collector on the NMS (goflow2 -> VictoriaLogs): the outer IPv6 flows
        show which uSIDs / paths carry the traffic, and every path crosses a P. Not on the PEs: hsflowd samples through pcap
        (every packet copied to user space), which costs a 1-vCPU PE ~15 % of its forwarding capacity on top of the
        encapsulation work. Lab traffic is small: sample 1 in 16 packets."""
        core_ports = [p["name"] for p in n["ports"] if p["peer"] and self.NODES[p["peer"]]["role"] in ("pe", "p")]
        if n["role"] != "p" or not core_ports: return []
        return ["# sFlow from the core-facing ports to the NMS collector (goflow2 -> VictoriaLogs): the outer IPv6 flows = SRv6 paths in use",
                f"set system sflow agent-address {n['mgmt_ip']}", f"set system sflow server {NMS_IP} port {SFLOW_PORT}",
                "set system sflow sampling-rate 16", "set system sflow polling 20"] + [f"set system sflow interface {p}" for p in core_ports]


    def core_ports(self, n):
        return [p for p in n["ports"] if p["peer"] and self.NODES[p["peer"]]["role"] in ("pe", "p")]

    def collector_ports(self, n):
        """Ports facing a BGP looking glass. Not core ports: the collector runs no IGP and carries no traffic, so the link
        stays out of IS-IS / SRv6 and only exists to carry the iBGP session that feeds the looking glass."""
        return [p for p in n["ports"] if p["peer"] and self.NODES[p["peer"]]["role"] == "lg"]

    @staticmethod
    def far_end(port):
        """The address at the other end of a point-to-point link (the ends are the first two hosts of the prefix)."""
        me = ipaddress.ip_interface(port["ip"]); net = me.network.network_address
        return str(net + 2 if me.ip == net + 1 else net + 1)


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
        inet = self.SVC.get("internet") or {}
        for ce_port in [p for p in n["ports"] if p["peer"] and self.NODES[p["peer"]]["role"] in ("ce", "ext-ce", "fw")]:
            vrf = ce_port["tenant"]; t = self.SVC["tenants"][vrf]; ce = self.NODES[ce_port["peer"]]; is_fw = ce["role"] == "fw"
            me = ipaddress.ip_interface(ce_port["ip"]); net = me.network.network_address
            ce_ip = str(net + 2 if me.ip == net + 1 else net + 1); rd = n["rd"][vrf]   # the other host of the /30 (an external CE is the first end)
            ext = " (external CE, another lab's router)" if ce["role"] == "ext-ce" else (" (the internet breakout firewall: default route only, IPv4)" if is_fw else "")
            ce_ip6 = None
            if ce_port.get("ip6"):
                me6 = ipaddress.ip_interface(ce_port["ip6"]); net6 = me6.network.network_address; ce_ip6 = str(net6 + 2 if me6.ip == net6 + 1 else net6 + 1)
            out += [f"# tenant VRF {vrf} (table {t['table']}, RT {t['rt']}, RD {rd}): attachment circuit {ce_port['name']} to {ce['name']} {ce_port['peer_port']}{ext}" + ("" if is_fw else ", dual-stack"),
                    f"set vrf name {vrf} table {t['table']}", f"set interfaces ethernet {ce_port['name']} vrf {vrf}", f"set interfaces ethernet {ce_port['name']} address {ce_port['ip']}"] + (
                    [f"set interfaces ethernet {ce_port['name']} address {ce_port['ip6']}"] if ce_ip6 else []) + [
                    f"set interfaces ethernet {ce_port['name']} description '{vrf}: {ce['name']} {ce_port['peer_port']}'",
                    f"# Linux scopes the SRv6 encapsulation's outer lookup to the ingress VRF for forwarded packets: leak every remote locator into",
                    f"# the VRF table via the attached P router(s) on the IGP shortest path (recursive through IS-IS, so a dead P drops out), and the",
                    f"# whole block via every attached P as the fallback"] + [
                    f"set vrf name {vrf} protocols static route6 {self.NODES[d]['locator']} next-hop {self.NODES[x]['loopback6']} vrf default"
                    for d, hops in sorted(first_hops.items()) if self.NODES[d].get("locator") for x in hops] + [
                    f"set vrf name {vrf} protocols static route6 {block} next-hop {self.NODES[x]['loopback6']} vrf default" for x in attached_ps]
            if is_fw:
                out += ["set policy prefix-list DEFAULT-ONLY rule 10 action permit", "set policy prefix-list DEFAULT-ONLY rule 10 prefix 0.0.0.0/0"]
            out += [f"# eBGP to the {'firewall' if is_fw else 'CE'}, one session per address family; one SRv6 End.DT46 SID per VRF carries both (sid vpn per-vrf export auto)",
                    f"set vrf name {vrf} protocols bgp system-as {self.SVC['core_as']}", f"set vrf name {vrf} protocols bgp parameters router-id {n['router_id']}",
                    f"set vrf name {vrf} protocols bgp parameters log-neighbor-changes",
                    f"set vrf name {vrf} protocols bgp sid vpn per-vrf export auto",
                    f"set vrf name {vrf} protocols bgp neighbor {ce_ip} remote-as {ce['asn']}", f"set vrf name {vrf} protocols bgp neighbor {ce_ip} description '{ce['name']} ({vrf}{', internet' if is_fw else ''})'",
                    f"set vrf name {vrf} protocols bgp neighbor {ce_ip} address-family ipv4-unicast" + (" prefix-list import DEFAULT-ONLY" if is_fw else "")] + ([
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
        for c in self.collector_ports(n):
            lg = self.NODES[c["peer"]]; lg_ip = self.far_end(c)
            out += [f"# {c['name']}: the BGP looking glass ({lg['name']}) hangs off this reflector on its own link — no IS-IS, no SRv6, no traffic",
                    f"set interfaces ethernet {c['name']} address {c['ip']}", f"set interfaces ethernet {c['name']} description 'looking glass: {lg['name']} {c['peer_port']}'"]
            if n["name"] not in self.SVC["rrs"]: continue   # only a reflector has the whole table to hand over
            out += [f"# ...and takes the whole VPN table as a reflector client. `capability extended-nexthop` is what keeps the PE's",
                    f"# loopback as the next hop: without it FRR rewrites the IPv6 next hop of a VPNv4 route to its own address and the",
                    f"# looking glass would show every prefix as coming from the reflector. The collector announces nothing back.",
                    f"set protocols bgp neighbor {lg_ip} remote-as {self.SVC['core_as']}",
                    f"set protocols bgp neighbor {lg_ip} description '{lg['name']} (BGP looking glass: route collector)'",
                    f"set protocols bgp neighbor {lg_ip} capability extended-nexthop"]
            for af in ("ipv4-vpn", "ipv6-vpn"):
                out += [f"set protocols bgp neighbor {lg_ip} address-family {af} route-reflector-client"]
        return out


    def fw(self, n):
        """The internet breakout firewall, VRF-lite: one attachment circuit per tenant, each in that tenant's VRF with an eBGP session
        to the PE that announces nothing but a default route; the last port is the libvirt NAT network (DHCP, the host's uplink) in the
        default VRF. The tenant VRFs import only the default route from the default VRF (BGP `import vrf`, route-map DEFAULT-ONLY), the
        default VRF imports the tenants' routes for the return traffic; source NAT on the uplink; the forward policy allows tenant ->
        internet and nothing else — the tenants never reach each other through the box (nor does anything new come in)."""
        acs = [p for p in n["ports"] if p["peer"]]; netp = next(p for p in n["ports"] if p.get("network"))
        tenant_space = "172.16.0.0/12"   # every tenant circuit and LAN of the lab lives here
        out = self.identity(n) + [
            f"# {netp['name']}: libvirt network '{netp['network']}' (DHCP) = the host's NAT uplink, default VRF. Source NAT: everything from the",
            f"# tenants leaves with this box's address on that network",
            f"set interfaces ethernet {netp['name']} address dhcp", f"set interfaces ethernet {netp['name']} description 'internet: libvirt {netp['network']} (host NAT)'",
            f"set nat source rule 100 outbound-interface name {netp['name']}", f"set nat source rule 100 source address {tenant_space}", "set nat source rule 100 translation address masquerade",
            "# the default VRF's BGP instance (no neighbours) only exists to leak: DHCP's default route (a static in FRR) out to the tenant VRFs,",
            "# the tenants' routes (their circuits and LANs, learnt from the PE) in for the return traffic",
            "set policy prefix-list DEFAULT-ONLY rule 10 action permit", "set policy prefix-list DEFAULT-ONLY rule 10 prefix 0.0.0.0/0",
            "set policy route-map DEFAULT-ONLY rule 10 action permit", "set policy route-map DEFAULT-ONLY rule 10 match ip address prefix-list DEFAULT-ONLY",
            f"set protocols bgp system-as {n['asn']}", f"set protocols bgp parameters router-id {n['mgmt_ip']}", "set protocols bgp parameters log-neighbor-changes",
            "set protocols bgp address-family ipv4-unicast redistribute static route-map DEFAULT-ONLY"] + [
            f"set protocols bgp address-family ipv4-unicast import vrf {ac['tenant']}" for ac in acs]
        for ac in acs:
            vrf = ac["tenant"]; pe_ip = str(ipaddress.ip_interface(ac["ip"]).network.network_address + 1); v = f"vrf name {vrf} "
            out += [f"# {vrf}: {ac['name']} = attachment circuit to {ac['peer']} {ac['peer_port']} in VRF {vrf}; eBGP announcing a default route and nothing else",
                    f"set vrf name {vrf} table {self.SVC['tenants'][vrf]['table']}", f"set interfaces ethernet {ac['name']} vrf {vrf}",
                    f"set interfaces ethernet {ac['name']} address {ac['ip']}", f"set interfaces ethernet {ac['name']} description '{ac['peer']} {ac['peer_port']} ({vrf})'",
                    f"set {v}protocols bgp system-as {n['asn']}", f"set {v}protocols bgp parameters router-id {ac['ip'].split('/')[0]}", f"set {v}protocols bgp parameters log-neighbor-changes",
                    f"set {v}protocols bgp neighbor {pe_ip} remote-as {self.SVC['core_as']}", f"set {v}protocols bgp neighbor {pe_ip} description '{ac['peer']} ({vrf})'",
                    f"set {v}protocols bgp neighbor {pe_ip} address-family ipv4-unicast default-originate",
                    f"set {v}protocols bgp neighbor {pe_ip} address-family ipv4-unicast prefix-list export DEFAULT-ONLY",
                    f"set {v}protocols bgp address-family ipv4-unicast import vrf default", f"set {v}protocols bgp address-family ipv4-unicast route-map vrf import DEFAULT-ONLY"]
        out += ["# stateful firewall: each tenant may go out to the internet, nothing else is forwarded (so no tenant -> tenant), nothing new",
                "# comes in from the internet side; management only from the OOB network",
                "set firewall ipv4 forward filter default-action drop",
                "set firewall ipv4 forward filter rule 5 action accept", "set firewall ipv4 forward filter rule 5 state established", "set firewall ipv4 forward filter rule 5 state related", "set firewall ipv4 forward filter rule 5 description 'established / related'"]
        out += ["set firewall ipv4 forward filter rule 8 action drop", f"set firewall ipv4 forward filter rule 8 destination address {tenant_space}", "set firewall ipv4 forward filter rule 8 log",
                "set firewall ipv4 forward filter rule 8 description 'tenant -> tenant: never through the breakout (a tenant VRF only ever sends the other tenants addresses here)'"]
        for i, ac in enumerate(acs):   # in the forward hook a packet received in a VRF carries the VRF device as its input interface, not ethN
            r = 10 + i
            out += [f"set firewall ipv4 forward filter rule {r} action accept", f"set firewall ipv4 forward filter rule {r} inbound-interface name {ac['tenant']}", f"set firewall ipv4 forward filter rule {r} outbound-interface name {netp['name']}",
                    f"set firewall ipv4 forward filter rule {r} source address {tenant_space}", f"set firewall ipv4 forward filter rule {r} description '{ac['tenant']} -> internet'"]
        out += ["set firewall ipv4 forward filter rule 900 action drop", "set firewall ipv4 forward filter rule 900 log", "set firewall ipv4 forward filter rule 900 description 'log everything else (tenant -> tenant included)'",
                "set firewall ipv4 input filter default-action drop",
                "set firewall ipv4 input filter rule 1 action accept", "set firewall ipv4 input filter rule 1 inbound-interface name lo", "set firewall ipv4 input filter rule 1 description 'loopback (the resolver, FRR)'",
                "set firewall ipv4 input filter rule 5 action accept", "set firewall ipv4 input filter rule 5 state established", "set firewall ipv4 input filter rule 5 state related",
                "set firewall ipv4 input filter rule 10 action accept", "set firewall ipv4 input filter rule 10 inbound-interface name eth0", "set firewall ipv4 input filter rule 10 description 'OOB management'"]
        for i, ac in enumerate(acs):
            r = 20 + i
            out += [f"set firewall ipv4 input filter rule {r} action accept", f"set firewall ipv4 input filter rule {r} inbound-interface name {ac['name']}", f"set firewall ipv4 input filter rule {r} protocol tcp", f"set firewall ipv4 input filter rule {r} destination port 179", f"set firewall ipv4 input filter rule {r} description 'BGP from the PE ({ac['tenant']})'"]
        out += ["set firewall ipv4 input filter rule 30 action accept", "set firewall ipv4 input filter rule 30 protocol icmp", "set firewall ipv4 input filter rule 30 description 'ping'",
                f"set firewall ipv4 input filter rule 40 action accept", f"set firewall ipv4 input filter rule 40 inbound-interface name {netp['name']}", "set firewall ipv4 input filter rule 40 protocol udp", "set firewall ipv4 input filter rule 40 source port 67", "set firewall ipv4 input filter rule 40 description 'DHCP from the host'"]
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


# ---- the BGP looking glass ----------------------------------------------------------------------------------------
# The collector is not a VyOS node: it runs FRR directly, so its configuration is FRR's own syntax rather than `set`
# lines. It is rendered from the same inventory as everything else, by the same two producers (lab.conf and Nautobot),
# so the looking glass stays a modelled part of the lab and not a hand-kept VM.

def lg_nodes(inv):
    return [n for n in inv["nodes"] if n["role"] == "lg"]


def render_lg(inv, name=None):
    """frr.conf of a looking-glass collector: one iBGP session per reflector, VPNv4 + VPNv6, announcing nothing."""
    N = {n["name"]: n for n in inv["nodes"]}; svc = inv["service"]
    n = N[name] if name else lg_nodes(inv)[0]
    peers = []
    for p in n["ports"]:
        if not p["peer"] or N[p["peer"]]["role"] != "p": continue
        me = ipaddress.ip_interface(p["ip"]); net = me.network.network_address
        peers.append((N[p["peer"]], str(net + 2 if me.ip == net + 1 else net + 1), p["name"]))
    out = [f"! {n['name']}: the lab's BGP looking glass — a passive route collector, rendered by tools/render.py",
           "! It peers with every route reflector over its own point-to-point link and receives the whole VPN table with the",
           "! attributes the PEs originated (RD, route targets, SRv6 SID and label, originator, cluster list). It runs no IGP,",
           "! installs nothing in the kernel and announces nothing: COLLECTOR-NO-EXPORT denies everything outbound.",
           "frr defaults traditional", f"hostname {n['name']}", "log file /var/log/frr/frr.log informational",
           "log syslog informational", "service integrated-vtysh-config", "!",
           f"router bgp {svc['core_as']}", f" bgp router-id {n['router_id']}", " bgp log-neighbor-changes",
           " no bgp default ipv4-unicast", " no bgp network import-check", " bgp graceful-restart"]
    for peer, ip, port in peers:
        rr = " (route reflector)" if peer["name"] in svc["rrs"] else ""
        out += [f" neighbor {ip} remote-as {svc['core_as']}", f" neighbor {ip} description {peer['name']}{rr} via {port}",
                f" neighbor {ip} capability extended-nexthop", f" neighbor {ip} timers 10 30"]
    for af in ("ipv4 vpn", "ipv6 vpn"):
        out.append(f" address-family {af}")
        for _, ip, _ in peers:
            # soft-reconfiguration keeps the Adj-RIB-In, so the looking glass can show what each reflector *sent* as well
            # as what won the best-path — without asking for a route refresh every time somebody opens the page
            out += [f"  neighbor {ip} activate", f"  neighbor {ip} soft-reconfiguration inbound",
                    f"  neighbor {ip} route-map COLLECTOR-NO-EXPORT out"]
        out.append(" exit-address-family")
    out += ["exit", "!", "route-map COLLECTOR-NO-EXPORT deny 10", "exit", "!"]
    return "\n".join(out) + "\n"


def lg_app_config(inv, name=None, port=8080, poll_local=20, poll_devices=120, history_days=30,
                  ssh=("vyos", "vyos"), nms=NMS_IP):
    """What the looking-glass service needs to make sense of what it collects: the reflectors it peers with, which RD and
    which locator belong to which PE and tenant, and the devices whose per-VRF tables it polls over SSH (the VPN table in
    the core cannot show a tenant's view *after* import — that only exists on the PE and the CE)."""
    N = {n["name"]: n for n in inv["nodes"]}; svc = inv["service"]
    n = N[name] if name else lg_nodes(inv)[0]
    peers = []
    for p in n["ports"]:
        if not p["peer"] or N[p["peer"]]["role"] != "p": continue
        me = ipaddress.ip_interface(p["ip"]); net = me.network.network_address
        peer = N[p["peer"]]
        peers.append({"name": peer["name"], "ip": str(net + 2 if me.ip == net + 1 else net + 1), "local": str(me.ip),
                      "interface": p["name"], "mgmt_ip": peer["mgmt_ip"], "rr": peer["name"] in svc["rrs"]})
    rd_map = {rd: {"vrf": t, "pe": x["name"], "dc": x["dc"]} for x in inv["nodes"] if x["role"] == "pe" for t, rd in (x.get("rd") or {}).items()}
    # router-id only for the nodes that actually run BGP: it is what resolves an originator-id to a name, and a node
    # without a BGP instance (p2) has none in Nautobot — carrying lab.conf's would make the two producers disagree
    nodes = {x["name"]: {"role": x["role"], "dc": x["dc"], "mgmt_ip": x["mgmt_ip"], "asn": x.get("asn"),
                         "loopback6": x.get("loopback6"), "locator": x.get("locator"),
                         "router_id": x.get("router_id") if x.get("asn") else None}
             for x in inv["nodes"] if x["role"] != "host"}
    # the wiring, so the looking glass can draw the testbed and work out which routers a prefix's traffic crosses:
    # the hosts and their LANs belong in it too (they are the ends of the path). Sorted, because the two producers
    # build the inventory in different orders.
    hosts = {x["name"]: {"role": "host", "dc": x["dc"], "mgmt_ip": x["mgmt_ip"],
                         "ip": (x["ports"][0]["ip"] or "").split("/")[0] or None,
                         "ip6": (x["ports"][0].get("ip6") or "").split("/")[0] or None,
                         "tenant": x["ports"][0].get("tenant")}
             for x in inv["nodes"] if x["role"] == "host" and x["ports"]}
    links = sorted(({"a": l["a"], "a_port": l["a_port"], "a_ip": l["a_ip"], "b": l["b"], "b_port": l["b_port"],
                     "b_ip": l["b_ip"], "prefix": l["prefix"], "tenant": l.get("tenant")} for l in inv["links"]),
                   key=lambda l: (l["a"], l["a_port"], l["b"], l["b_port"]))
    devices = []
    for x in inv["nodes"]:
        if x["role"] not in ("pe", "p", "ce", "fw"): continue
        vrfs = sorted({p["tenant"] for p in x["ports"] if p.get("tenant")})
        families = [("ipv4", "unicast")] + ([("ipv6", "unicast")] if any(p.get("ip6") for p in x["ports"]) else [])
        # what to ask each router for, and therefore how: the RIB and the IS-IS adjacencies come from its own HTTPS
        # API (JSON), the VPN table too (the reflector's own view, to hold against what the session delivered), and
        # the per-VRF BGP tables over SSH because VyOS's op-mode has no `json` for them
        collect = {"rib": True, "isis": x["role"] in ("pe", "p"),
                   "vpn": x["role"] == "pe" or x["name"] in svc["rrs"], "bgp_vrf": bool(vrfs)}
        devices.append({"name": x["name"], "role": x["role"], "dc": x["dc"], "mgmt_ip": x["mgmt_ip"], "vrfs": vrfs,
                        "families": [list(f) for f in families], "collect": collect})
    devices.sort(key=lambda d: d["name"])      # two producers feed this (lab.conf and Nautobot): the order must not depend on them
    return {"lab": inv["lab"], "node": n["name"], "listen": {"host": "0.0.0.0", "port": port},
            "db": "/var/lib/lgd/lg.db", "history_days": history_days,
            "collector": {"asn": svc["core_as"], "router_id": n["router_id"], "peers": peers,
                          "families": [["ipv4", "vpn"], ["ipv6", "vpn"]]},
            "service": {"core_as": svc["core_as"], "rrs": svc["rrs"], "srv6": svc["srv6"],
                        "tenants": svc["tenants"], "isis_area": svc.get("isis_area")},
            "rd_map": rd_map, "nodes": nodes, "devices": devices, "topology": {"links": links, "hosts": hosts},
            "loopbacks": {x["loopback6"]: x["name"] for x in inv["nodes"] if x.get("loopback6")},
            "locators": {x["locator"]: x["name"] for x in inv["nodes"] if x.get("locator")},
            "poll": {"local": poll_local, "devices": poll_devices},
            "ssh": {"username": ssh[0], "password": ssh[1]},
            # the routers' own HTTPS API: where the RIBs, the VPN tables and the IS-IS adjacencies come from. The
            # per-VRF BGP tables still come over SSH — VyOS's op-mode has no `json` for them — and every row the
            # looking glass stores says which of the two it came from.
            "api": {"key": API_KEY, "scheme": "https"}, "nms": nms}
