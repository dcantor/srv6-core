#!/usr/bin/env python3
"""How a packet to a prefix actually crosses the testbed, hop by hop.

A looking glass that only lists prefixes answers "who has it"; the question after that is always "and how does my
traffic get there". For an L3VPN over SRv6 the answer comes from three different places, which is why it has to be
assembled rather than read:

  the BGP data      the collector already knows who originated the prefix (the AS path's last AS = the CE), which PE
                    exported it (originator-id / the SRv6 SID's locator), under which RD and route target, and which
                    PEs imported it into which VRF
  the IGP           between the ingress PE and the egress PE the packet is an IPv6 packet addressed to the egress
                    PE's End.DT46 SID, so it follows the IS-IS shortest path — computed here over the lab's links
                    (every core link has the same metric), equal-cost paths included
  the router        what the ingress PE has actually installed can differ from the shortest path: an explicit-path
                    steering policy (tools/steer.py) replaces the next hop with a uSID carrier of its own. So the
                    first hop is read live from the PE's route when the caller asks for it, and the segment list it
                    carries wins over the computed path.

The result is a list of hops, each with the node it is on, what happens there (eBGP hand-off, VPN import, SRv6
encapsulation, plain IPv6 forwarding, decapsulation) and the interface or SID involved — enough for the page to draw
the path over the topology and to list it router by router."""
import heapq, ipaddress, re


def _core_adjacency(topology, nodes):
    """Node -> [(neighbour, interface, peer interface)] over the links that carry the underlay (PE and P only)."""
    adj = {}
    for l in topology["links"]:
        ra, rb = (nodes.get(l["a"], {}).get("role"), nodes.get(l["b"], {}).get("role"))
        if ra not in ("pe", "p") or rb not in ("pe", "p"): continue
        adj.setdefault(l["a"], []).append((l["b"], l["a_port"], l["b_port"]))
        adj.setdefault(l["b"], []).append((l["a"], l["b_port"], l["a_port"]))
    return adj


def igp_paths(topology, nodes, src, dst, limit=4):
    """Every equal-cost shortest path from src to dst as a list of node names (all core links have the same metric)."""
    if src == dst: return [[src]]
    adj = _core_adjacency(topology, nodes)
    if src not in adj or dst not in adj: return []
    dist = {src: 0}; heap = [(0, src)]
    while heap:
        d, u = heapq.heappop(heap)
        if d > dist.get(u, 1 << 30): continue
        for v, _, _ in adj.get(u, []):
            if d + 1 < dist.get(v, 1 << 30): dist[v] = d + 1; heapq.heappush(heap, (d + 1, v))
    if dst not in dist: return []
    out = []                                   # walk back from dst along nodes that are exactly one hop closer
    def walk(node, acc):
        if len(out) >= limit: return
        if node == src: out.append([src] + list(reversed(acc))); return
        for v, _, _ in sorted(adj.get(node, [])):
            if dist.get(v, 1 << 30) == dist[node] - 1: walk(v, acc + [node])
    walk(dst, [])
    return out


def link_between(topology, a, b):
    for l in topology["links"]:
        if l["a"] == a and l["b"] == b: return {"port": l["a_port"], "peer_port": l["b_port"], "prefix": l["prefix"], "ip": l["a_ip"], "peer_ip": l["b_ip"]}
        if l["b"] == a and l["a"] == b: return {"port": l["b_port"], "peer_port": l["a_port"], "prefix": l["prefix"], "ip": l["b_ip"], "peer_ip": l["a_ip"]}
    return None


def attachment(topology, nodes, pe, vrf, kind=("ce", "fw", "ext-ce")):
    """The CE (or firewall) attached to `pe` in `vrf`, with the circuit between them."""
    for l in topology["links"]:
        for near, far, port, peer_port, ip, peer_ip in ((l["a"], l["b"], l["a_port"], l["b_port"], l["a_ip"], l["b_ip"]),
                                                        (l["b"], l["a"], l["b_port"], l["a_port"], l["b_ip"], l["a_ip"])):
            if near == pe and l.get("tenant") == vrf and nodes.get(far, {}).get("role") in kind:
                return {"node": far, "port": port, "peer_port": peer_port, "prefix": l["prefix"], "ip": ip, "peer_ip": peer_ip}
    return None


def lan_of(topology, nodes, hosts, ce, vrf, prefix):
    """The site LAN of `ce` in `vrf` (the one the prefix belongs to, when it is a LAN) and the host on it."""
    for l in topology["links"]:
        for near, far, port, ip, peer_ip in ((l["a"], l["b"], l["a_port"], l["a_ip"], l["b_ip"]), (l["b"], l["a"], l["b_port"], l["b_ip"], l["a_ip"])):
            if near == ce and l.get("tenant") == vrf and far in hosts:
                if prefix and l["prefix"] != prefix and not _covers(l["prefix"], prefix): continue
                return {"host": far, "port": port, "prefix": l["prefix"], "ip": ip, "host_ip": peer_ip}
    return None


def _covers(outer, inner):
    try: return ipaddress.ip_network(inner).subnet_of(ipaddress.ip_network(outer))
    except (ValueError, TypeError): return False


SEG_RE = re.compile(r"segs \d+ \[([^\]]+)\]")


def parse_route(text):
    """What the router has actually installed, read from the kernel (`ip route show vrf <vrf> <prefix>`), which is the
    only view that shows the SRv6 encapsulation: the segment list, the outgoing interface and who installed it
    (`proto bgp` for the VPN route, `proto static`/`proto zebra` for a steering policy's own route)."""
    if not text: return None
    segs, dev, proto = [], None, None
    m = SEG_RE.search(text)
    if m: segs = [s.strip() for s in m.group(1).split(",") if s.strip()]
    m = re.search(r"\bdev\s+(\S+)", text)
    if m: dev = m.group(1)
    m = re.search(r"\bproto\s+(\S+)", text)
    if m: proto = m.group(1)
    return {"segments": segs, "dev": dev, "proto": proto, "raw": text.strip().splitlines()[0] if text.strip() else ""}


def segment_nodes(segments, cfg):
    """The routers a segment list names, in order.

    With uSID (the lab's `usid-f3216`) a whole explicit path is *one* 128-bit address: the 32-bit block followed by a
    sequence of 16-bit micro-SIDs, one per node, ending in the last node's function (its End.DT46). So a single
    segment like fd00:c:11:13:3:e001:: is "p1, then p3, then pe3's tenant SID" and has to be unpacked chunk by chunk;
    an uncompressed list is simply one address per node."""
    srv6 = (cfg.get("service") or {}).get("srv6") or {}
    block_len, node_len = int(srv6.get("block_len", 32)), int(srv6.get("node_len", 16))
    by_locator = {}
    for name, n in (cfg.get("nodes") or {}).items():
        if n.get("locator"): by_locator[ipaddress.ip_network(n["locator"])] = name
    out = []
    for seg in segments:
        try: addr = int(ipaddress.ip_address(seg.split("/")[0]))
        except ValueError: continue
        node = next((nm for net, nm in by_locator.items() if ipaddress.ip_address(addr) in net), None)
        if node and node not in out: out.append(node)
        if not srv6.get("format", "").startswith("usid"): continue
        block = addr >> (128 - block_len) << (128 - block_len)          # the carrier's chunks after the block
        for i in range(block_len, 128, node_len):
            chunk = (addr >> (128 - i - node_len)) & ((1 << node_len) - 1)
            if not chunk: break
            cand = ipaddress.ip_address(block | (chunk << (128 - block_len - node_len)))
            nm = next((n for net, n in by_locator.items() if cand in net), None)
            if nm is None: break                                        # not a node: the last chunk is a function (uDT46)
            if nm not in out: out.append(nm)
    return out


def from_rib(row):
    """A stored RIB row (pulled from the router's own API) in the shape the path builder wants: the SRv6 segments it
    encapsulates into, the interface it forwards out of, and who installed the route."""
    if not row: return None
    a = row.get("attrs") or {}
    segs, dev = [], None
    for nh in a.get("nexthops") or []:
        s = nh.get("seg6")
        s = s.get("segs") if isinstance(s, dict) else s
        for one in ([s] if isinstance(s, str) else (s or [])):
            if one and one not in segs: segs.append(one)
        if nh.get("interface") and not dev: dev = nh["interface"]
    return {"segments": segs, "dev": dev, "proto": a.get("protocol"), "via": row.get("via") or "router-api",
            "age": row.get("last_seen"), "raw": f"{row['prefix']} proto {a.get('protocol')} metric {a.get('metric')}"
                                                 + (f" segs [{', '.join(segs)}]" if segs else "") + (f" dev {dev}" if dev else "")}


def build(prefix, vrf, paths, cfg, vantage=None, route_text=None, route=None):
    """The hop list for `prefix` in `vrf`, seen from `vantage` (a PE or a CE). `paths` are the collector's paths for
    the prefix; `route_text` (optional) is what the vantage router's RIB says, which is what makes a steered path
    visible. Returns {hops, notes, egress, origin, ...}."""
    nodes, topo, hosts = cfg["nodes"], cfg["topology"], cfg["topology"]["hosts"]
    rd_map = cfg["rd_map"]
    core = [p for p in paths if p["safi"] == "vpn"] or paths
    if not core: return {"error": f"the looking glass holds no path for {prefix}"}
    best = next((p for p in core if p.get("best")), core[0])
    vrf = vrf or best.get("vrf")
    egress = best.get("origin_node") or (rd_map.get(best.get("rd") or "", {}) or {}).get("pe")
    sid = (best.get("attrs") or {}).get("sid")
    rd, rt = best.get("rd"), (best.get("attrs") or {}).get("ext_communities")
    origin_as = best.get("origin_as")

    pes = [name for name, n in nodes.items() if n["role"] == "pe"]
    if vantage not in nodes: vantage = None
    if not vantage:                                   # a vantage point that makes the path interesting: not the egress
        vantage = next((p for p in sorted(pes) if p != egress), egress)
    hops, notes = [], []

    def hop(node, what, detail=None, **extra):
        hops.append({"node": node, "role": nodes.get(node, {}).get("role") or ("host" if node in hosts else "?"),
                     "dc": (nodes.get(node) or hosts.get(node) or {}).get("dc"), "what": what, "detail": detail, **extra})

    # --- the ingress side: where the traffic comes from ---------------------------------------------------------
    v_role = nodes.get(vantage, {}).get("role")
    if v_role == "pe":
        ac = attachment(topo, nodes, vantage, vrf)
        if ac:
            lan = lan_of(topo, nodes, hosts, ac["node"], vrf, None)
            if lan: hop(lan["host"], "sends to " + prefix, f"{lan['host_ip'].split('/')[0]} on {lan['prefix']}", interface=None)
            hop(ac["node"], f"the {vrf} CE at {nodes.get(ac['node'], {}).get('dc', '')}",
                f"eBGP to {vantage} over {ac['prefix']}", interface=ac["peer_port"])
        hop(vantage, "ingress PE: imports the route into VRF " + str(vrf),
            f"route target {rt}, next hop {best.get('nexthop')}" + (f", encapsulates to {sid}" if sid else ""),
            interface=ac["port"] if ac else None, ingress=True)
    elif v_role in ("ce", "fw"):
        pe = next((l for l in topo["links"] if vantage in (l["a"], l["b"]) and l.get("tenant") == vrf
                   and nodes.get(l["a"] if l["b"] == vantage else l["b"], {}).get("role") == "pe"), None)
        pe_name = (pe["a"] if pe and pe["b"] == vantage else (pe["b"] if pe else None))
        src_lan = lan_of(topo, nodes, hosts, vantage, vrf, None)
        if src_lan: hop(src_lan["host"], "sends to " + prefix, f"{src_lan['host_ip'].split('/')[0]} on {src_lan['prefix']}")
        hop(vantage, f"the {vrf} CE at {nodes.get(vantage, {}).get('dc', '')}", f"eBGP to {pe_name}")
        if pe_name:
            hop(pe_name, "ingress PE: imports the route into VRF " + str(vrf),
                f"route target {rt}" + (f", encapsulates to {sid}" if sid else ""), ingress=True)
            vantage = pe_name
    else:
        hop(vantage, "vantage point", None, ingress=True)

    # --- the core: the SRv6 packet's path to the egress PE ------------------------------------------------------
    live = route or parse_route(route_text)
    seg_nodes = segment_nodes((live or {}).get("segments") or [], cfg)
    # a steering policy is a static route of its own, and its segment list names more than just the egress PE
    steered = bool(live and (live.get("proto") == "static" or len(seg_nodes) > 1))
    first_hop = None                                  # the neighbour on the interface the router actually forwards out of
    if live and live.get("dev"):
        for l in topo["links"]:
            if l["a"] == vantage and l["a_port"] == live["dev"]: first_hop = l["b"]
            elif l["b"] == vantage and l["b_port"] == live["dev"]: first_hop = l["a"]
    path_nodes = []
    if egress and egress != vantage:
        if steered:                                   # the segment list names the routers to cross, in order
            path_nodes = [n for n in seg_nodes if n != vantage]
            if egress not in path_nodes: path_nodes.append(egress)
            notes.append(f"{vantage} has an explicit-path steering policy for this prefix: the path below is the segment "
                         f"list it installed ({', '.join(live['segments'])}), not the IGP shortest path")
        else:
            options = igp_paths(topo, nodes, vantage, egress)
            chosen = next((o for o in options if first_hop and len(o) > 1 and o[1] == first_hop), options[0] if options else None)
            if chosen:
                path_nodes = chosen[1:-1] + [egress]
                if first_hop and chosen[1] == first_hop:
                    notes.append(f"the first hop is read from {vantage}'s own route: it forwards out of {live['dev']} to {first_hop}")
                if len(options) > 1:
                    notes.append("equal-cost paths through the core: " + " / ".join(" → ".join(o[1:-1]) for o in options)
                                 + " — the packet takes one of them per flow")
        for i, node in enumerate(path_nodes):
            prev = hops[-1]["node"] if hops else vantage
            l = link_between(topo, prev, node)
            if node == egress:
                hop(node, "egress PE: decapsulates (End.DT46) and looks the packet up in VRF " + str(vrf),
                    f"SID {sid}" if sid else None, interface=l["peer_port"] if l else None, egress=True)
            else:
                hop(node, "P router: forwards the encapsulated packet",
                    f"IPv6 {l['prefix']}" if l else None, interface=l["peer_port"] if l else None)
    elif egress == vantage:
        notes.append("the prefix is local to this PE: it is the egress, nothing is encapsulated")
        hops[-1]["egress"] = True

    # --- the egress side: PE to CE to the LAN -------------------------------------------------------------------
    if egress:
        ac = attachment(topo, nodes, egress, vrf)
        if ac:
            hop(ac["node"], f"the {vrf} CE that owns the prefix" + (f" (AS {origin_as})" if origin_as else ""),
                f"eBGP to {egress} over {ac['prefix']}", interface=ac["peer_port"])
            lan = lan_of(topo, nodes, hosts, ac["node"], vrf, prefix)
            if lan: hop(lan["host"], "the destination is on " + lan["prefix"], lan["host_ip"].split("/")[0], interface=None)
    return {"prefix": prefix, "vrf": vrf, "rd": rd, "route_target": rt, "sid": sid, "egress": egress,
            "origin_as": origin_as, "vantage": vantage, "steered": steered, "hops": hops, "notes": notes,
            "live": live, "nexthop": best.get("nexthop"),
            # where each half of the answer came from: the control plane from the collector's own session with the
            # reflectors, the forwarding decision from the router itself
            "sources": {"control_plane": best.get("via") or "rr-session", "forwarding": (live or {}).get("via")}}
