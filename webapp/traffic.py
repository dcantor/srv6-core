"""Traffic on the map: how much crosses every core link in each direction, what it is, and where steered traffic went.

Two sources, each for what it is good at:

  load         the interface counters of the sending end (node-exporter on every router -> Prometheus): exact bits/s
  composition  the P routers' sFlow samples (hsflowd, 1 in 16 packets on every core port -> goflow2 -> VictoriaLogs):
               what share of a link's traffic is which tenant to which PE, steered or not, or IS-IS / BFD / BGP

A sample is taken on one interface in one direction (`out_if` leaving the P router, `in_if` arriving), so every
direction of every link has exactly one observer: the P router sending it, or — on a PE's link towards a P router —
the P router receiving it. The SRv6 traffic is read from the Segment Routing Header that Linux's encapsulation always
adds: its segment list is the one the ingress PE built, unchanged at every hop, so it names the egress PE and the
tenant (the End.DT46 SID, as the looking glass decoded it) and, for a steered packet, every waypoint — a uSID carrier
is unpacked into the routers it names. Traffic entering the core on a PE's link is the tenant traffic matrix: ingress
PE (the link it entered on) to egress PE (the SID). Shares are of sampled bytes; bits/s are the counters' times the
share, so a category is an estimate and the total is not."""
import ipaddress, json, os, re, time, urllib.parse, urllib.request

PROMETHEUS = os.environ.get("PROMETHEUS_URL", "http://10.0.0.10:9090")
VLOGS = os.environ.get("VICTORIALOGS_URL", "http://10.0.0.10:9428")
LG = os.environ.get("LOOKING_GLASS_URL", "http://10.3.0.70:8080")
UNKNOWN_IF = {"0", "1073741823"}                # sFlow's "not this side" / "unknown" interface
WINDOWS = {"1m": 60, "5m": 300, "15m": 900, "1h": 3600}


def _get(url, timeout=20):
    with urllib.request.urlopen(url, timeout=timeout) as r: return r.read().decode()


def prom(query):
    d = json.loads(_get(f"{PROMETHEUS}/api/v1/query?" + urllib.parse.urlencode({"query": query})))
    return d["data"]["result"]


def logsql(query):
    body = _get(f"{VLOGS}/select/logsql/query?" + urllib.parse.urlencode({"query": query}), timeout=30)
    return [json.loads(l) for l in body.splitlines() if l.strip()]


class Decoder:
    """Addresses of the lab's SRv6 block -> the routers they name and the final function, and an End.DT46 SID -> (PE, tenant)."""

    def __init__(self, inv, sid_table):
        svc = inv["service"]; sr = svc["srv6"]
        self.block = ipaddress.ip_network(sr["block"]); self.usid = sr["format"].startswith("usid")
        self.blen, self.nlen, self.flen = int(sr["block_len"]), int(sr["node_len"]), int(sr["func_bits"])
        self.locators = [(ipaddress.ip_network(n["locator"]), n["name"]) for n in inv["nodes"] if n.get("locator")]
        self.node_id = {}                                   # the uSID of each router: the bits after the block in its locator
        for net, name in self.locators:
            self.node_id[(int(net.network_address) >> (128 - self.blen - self.nlen)) & ((1 << self.nlen) - 1)] = name
        self.sid_table = sid_table

    def nodes(self, addr):
        """(routers named, in order; the final SID: locator of the last router + the function)."""
        try: a = ipaddress.IPv6Address(addr)
        except ValueError: return [], None
        if a not in self.block: return [], None
        if not self.usid:
            name = next((n for net, n in self.locators if a in net), None)
            return ([name] if name else []), str(a)
        rest = int(a) & ((1 << (128 - self.blen)) - 1); words = []
        for i in range((128 - self.blen) // self.nlen):
            words.append((rest >> (128 - self.blen - self.nlen * (i + 1))) & ((1 << self.nlen) - 1))
        while words and words[-1] == 0: words.pop()
        named = []
        for w in words:
            if w in self.node_id: named.append(self.node_id[w])
            else: break
        func = words[len(named)] if len(words) > len(named) else 0
        if not named: return [], None
        last = next(net for net, n in self.locators if n == named[-1])
        final = ipaddress.IPv6Address(int(last.network_address) | (func << (128 - self.blen - self.nlen - self.flen)))
        return named, str(final)

    def classify(self, segs):
        """The SRH's segment list -> {egress, tenant, sid, waypoints} (waypoints: the P routers a steered packet is sent through)."""
        named, final = [], None
        for s in segs:
            n, f = self.nodes(s)
            named += n; final = f or final
        if not named: return None
        pe, tenant = self.sid_table.get(final, (named[-1], None))
        if not tenant and final: tenant = f"SID {final} (no longer current)"   # a PE's SIDs are renumbered when its BGP is re-committed
        return {"egress": pe, "tenant": tenant, "sid": final, "waypoints": [n for n in named[:-1]]}


def sid_table():
    """End.DT46 SID -> (PE, tenant), from the looking glass (it decodes the transposed SIDs out of BMP)."""
    try:
        d = json.loads(_get(f"{LG}/api/prefixes?source=collector&safi=vpn&limit=5000", timeout=15))
    except Exception:                                          # noqa: BLE001 — without it the tenant is just unnamed
        return {}
    out = {}
    for p in d.get("paths", []):
        sid = (p.get("attrs") or {}).get("transposed_sid")
        if sid and p.get("origin_node") and p.get("vrf"): out[sid] = (p["origin_node"], p["vrf"])
    return out


def _kind(r, dec):
    proto, port = r.get("proto") or "", r.get("dst_port") or ""
    segs = json.loads(r.get("ipv6_routing_header_addresses") or "[]")
    if segs:
        c = dec.classify(segs)
        if c: return ("srv6", c)
    if proto in ("HOPOPT", "") and not r.get("etype"): return ("IS-IS", None)
    if proto == "UDP" and port in ("3784", "4784"): return ("BFD", None)
    if proto == "TCP" and (port == "179" or r.get("src_port") == "179"): return ("BGP", None)
    if proto == "IPv6-ICMP": return ("ICMPv6 / ND", None)
    return ("other", None)


def collect(inv, window="5m", policies=()):
    secs = WINDOWS.get(window, 300); t0 = time.time()
    role = {n["name"]: n["role"] for n in inv["nodes"]}
    mgmt = {n["mgmt_ip"]: n["name"] for n in inv["nodes"]}
    ps = [n for n in inv["nodes"] if n["role"] == "p"]
    links = [l for l in inv["links"] if role.get(l["a"]) in ("p", "pe") and role.get(l["b"]) in ("p", "pe")]
    port_peer = {}                                          # (node, port) -> (peer, peer port)
    for l in links: port_peer[(l["a"], l["a_port"])] = (l["b"], l["b_port"]); port_peer[(l["b"], l["b_port"])] = (l["a"], l["a_port"])
    # load: every core port's transmit rate, i.e. one direction of its link, measured at the sender
    rng = "2m" if secs <= 120 else window
    tx = {(r["metric"]["node"], r["metric"]["device"]): float(r["value"][1]) * 8
          for r in prom(f'rate(node_network_transmit_bytes_total{{lab="{inv["lab"]}",role=~"p|pe",device=~"eth[0-9]+"}}[{rng}])')}
    ifname = {(r["metric"]["node"], r["value"][1]): r["metric"]["device"]
              for r in prom(f'node_network_iface_id{{lab="{inv["lab"]}",role="p"}}')}
    dec = Decoder(inv, sid_table())
    rows = logsql(f'_time:{window} sampler_address:in({",".join(json.dumps(p["mgmt_ip"]) for p in ps)}) '
                  f'| stats by (sampler_address, in_if, out_if, etype, proto, dst_port, src_port, ipv6_routing_header_addresses) '
                  f'sum(bytes) bytes, count() samples, max(sampling_rate) rate')
    dirs = {}                                               # (from, to) -> {kind key: bytes}
    matrix, seen_policy, samples = {}, {}, 0
    for r in rows:
        node = mgmt.get(r.get("sampler_address")); n = int(r.get("samples") or 0); samples += n
        b = float(r.get("bytes") or 0) * float(r.get("rate") or 1)
        if r.get("out_if") not in UNKNOWN_IF: port = ifname.get((node, r["out_if"])); src_side = True
        elif r.get("in_if") not in UNKNOWN_IF: port = ifname.get((node, r["in_if"])); src_side = False
        else: continue
        peer = port_peer.get((node, port))
        if not peer: continue
        a, z = (node, peer[0]) if src_side else (peer[0], node)
        # one observer per direction: the P router that sends it, or (from a PE) the P router that receives it
        if not src_side and role.get(a) == "p": continue
        kind, c = _kind(r, dec)
        key = kind if kind != "srv6" else f"{c['tenant'] or '?'} → {c['egress']}" + (" (steered)" if c["waypoints"] else "")
        d = dirs.setdefault((a, z), {}); d[key] = d.get(key, 0) + b
        if kind == "srv6":
            if role.get(a) == "pe":                         # entering the core: the tenant traffic matrix
                m = matrix.setdefault((c["tenant"] or "?", a, c["egress"]), 0); matrix[(c["tenant"] or "?", a, c["egress"])] = m + b
            if c["waypoints"]:
                seen_policy.setdefault((c["sid"], tuple(c["waypoints"])), set()).add((a, z))
    out_links = []
    for l in links:
        lanes = []
        for a, ap, z in ((l["a"], l["a_port"], l["b"]), (l["b"], l["b_port"], l["a"])):
            bps = tx.get((a, ap)); comp = dirs.get((a, z), {}); tot = sum(comp.values())
            parts = sorted(({"what": k, "share": v / tot, "bps": (bps or 0) * v / tot} for k, v in comp.items()), key=lambda x: -x["share"]) if tot else []
            lanes.append({"from": a, "to": z, "port": ap, "bps": bps, "parts": parts})
        out_links.append({"a": l["a"], "b": l["b"], "lanes": lanes})
    tot_m = sum(matrix.values()) or 1
    mat = sorted(({"tenant": t, "ingress": i, "egress": e, "share": v / tot_m} for (t, i, e), v in matrix.items()), key=lambda x: -x["share"])
    pol_out = []
    for p in policies:
        if p.get("error") or not p.get("steered"): continue
        named, final = [], None
        for s in p.get("segments") or []:
            n, f = dec.nodes(s); named += n; final = f or final
        want = [(x, y) for x, y in zip(p["steered"], p["steered"][1:])]
        got = set()
        for (sid, wps), lks in seen_policy.items():
            if sid == final and list(wps) == named[:-1]: got |= lks
        pol_out.append({"pe": p["pe"], "tenant": p["tenant"], "prefix": p["prefix"], "path": p["steered"],
                        "seen": sorted(got), "matches": bool(got) and got <= set(want),
                        "missing": [l for l in want if l not in got]})
    return {"window": window, "seconds": secs, "samples": samples, "links": out_links, "matrix": mat, "policies": pol_out,
            "nodes": [{"name": n["name"], "role": n["role"], "dc": n.get("dc")} for n in inv["nodes"] if n["role"] in ("p", "pe")],
            "took": round(time.time() - t0, 2), "as_of": time.time()}
