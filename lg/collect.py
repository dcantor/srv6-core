#!/usr/bin/env python3
"""Where the looking glass gets its data.

Two sources, because one view cannot answer every question:

  the collector's own RIB   FRR on this VM holds an iBGP session to every route reflector, so `show bgp ipv4|ipv6 vpn`
                            is the core's whole L3VPN table with the attributes the PEs originated — RD, route targets,
                            SRv6 SID and label, originator, cluster list. Read locally over vtysh, so it can be read
                            often (no SSH, no router CPU).
  the routers themselves    what each router actually holds: its RIB per VRF (which protocol won, which next hop and
                            interface, the SRv6 encapsulation), its per-VRF BGP tables (best-path selection on that PE,
                            the CE's own eBGP view) and its IS-IS adjacencies. Read from the router's **own HTTPS API**
                            (`service https api`, JSON out) where it can express the question, and over SSH / vtysh
                            where it cannot — VyOS's op-mode has no `json` for `show bgp vrf <x> ipv4 unicast`.

Every row carries **how it was obtained** — `rr-session`, `router-api` or `router-ssh` — because the same prefix seen
two ways is two answers, and a looking glass that hides which one it is showing is a looking glass you cannot trust.

Both are normalised into the same shape and handed to Store.reconcile(), which is what turns a sequence of snapshots
into a history: announce / change (with the fields that changed) / withdraw.

FRR's JSON has two quirks worth knowing. `show bgp ... vpn json` carries the true next hop (the PE's loopback) but no
attributes; `show bgp ... vpn detail json` carries every attribute but prints the next hop as 0.0.0.0 whenever it is an
IPv6 one behind extended next-hop encoding. So the two are read together and merged per path. And a prefix can hold
several paths from the same peer (multipath, or one import per RD), so a path is identified by its next hop as well.
"""
import ipaddress, json, re, subprocess, threading, time

VTYSH = ["vtysh", "-c"]


def _json(text):
    """vtysh sometimes prefixes its JSON with a warning line; take the document."""
    text = text.strip()
    if not text: return {}
    i = min([x for x in (text.find("{"), text.find("[")) if x >= 0], default=-1)
    if i < 0: raise ValueError(f"no JSON in vtysh output: {text[:200]}")
    return json.loads(text[i:])


class Resolver:
    """lab.conf's view of the core, used to turn addresses, RDs and SIDs into names the UI can show."""

    def __init__(self, cfg):
        self.cfg = cfg
        self.rd_map = cfg.get("rd_map", {})
        self.nodes = cfg.get("nodes", {})
        self.by_ip = {}
        for ip, name in (cfg.get("loopbacks") or {}).items(): self.by_ip[ip] = name
        for name, n in self.nodes.items():
            for k in ("mgmt_ip", "router_id"):
                if n.get(k): self.by_ip.setdefault(n[k], name)
        for p in cfg.get("collector", {}).get("peers", []): self.by_ip.setdefault(p["ip"], p["name"])
        self.locators = [(ipaddress.ip_network(pfx), name) for pfx, name in (cfg.get("locators") or {}).items()]
        self.router_ids = {n["router_id"]: name for name, n in self.nodes.items() if n.get("router_id")}

    def node_of_ip(self, ip):
        if not ip: return None
        return self.by_ip.get(str(ip).split("%")[0])

    def node_of_sid(self, sid):
        if not sid: return None
        try: a = ipaddress.ip_address(sid.split("/")[0])
        except ValueError: return None
        return next((name for net, name in self.locators if a in net), None)

    def vrf_of_rd(self, rd):
        return (self.rd_map.get(rd) or {}).get("vrf")

    def pe_of_rd(self, rd):
        return (self.rd_map.get(rd) or {}).get("pe")


def normalise(p, rd=None, resolver=None, nexthop=None):
    """One FRR path (detail JSON) -> the attribute set the looking glass stores, plus the fields it indexes on."""
    a = {}
    asp = p.get("aspath")
    a["as_path"] = (asp.get("string") if isinstance(asp, dict) else p.get("path")) or ""
    if a["as_path"] in ("Local", "local"): a["as_path"] = ""
    a["origin"] = p.get("origin")
    a["local_pref"] = p.get("locPrf")
    a["med"] = p.get("metric")
    a["weight"] = p.get("weight")
    a["valid"] = bool(p.get("valid"))
    bp = p.get("bestpath")
    a["best"] = bool(bp.get("overall")) if isinstance(bp, dict) else bool(bp)
    a["selection_reason"] = (bp or {}).get("selectionReason") if isinstance(bp, dict) else p.get("selectionReason")
    for src, dst in (("community", "communities"), ("extendedCommunity", "ext_communities"), ("largeCommunity", "large_communities")):
        v = p.get(src)
        if isinstance(v, dict): v = v.get("string")
        if v: a[dst] = v
    a["originator_id"] = p.get("originatorId")
    cl = p.get("clusterList")
    if cl: a["cluster_list"] = cl.get("list") if isinstance(cl, dict) else cl
    if p.get("remoteLabel") is not None: a["label"] = p["remoteLabel"]
    for src, dst in (("remoteSid", "sid"), ("remoteTransposedSid", "transposed_sid"), ("importedFrom", "imported_from"),
                     ("pathFrom", "path_from"), ("atomicAggregate", "atomic_aggregate"), ("aggregatorAs", "aggregator_as")):
        if p.get(src) not in (None, ""): a[dst] = p[src]
    if p.get("remoteSidStructure"): a["sid_structure"] = p["remoteSidStructure"]
    if p.get("local"): a["local"] = True
    if p.get("sourced"): a["sourced"] = True
    if p.get("multipath"): a["multipath"] = True
    peer = p.get("peer") or {}
    peer_id = peer.get("peerId") if isinstance(peer, dict) else None
    peer_id = peer_id or p.get("peerId")
    if peer_id in ("(unspec)", "::", "0.0.0.0", None): peer_id = ""
    if isinstance(peer, dict):
        a["peer_router_id"] = peer.get("routerId"); a["peer_hostname"] = peer.get("hostname"); a["peer_type"] = peer.get("type")
    # the next hop: the merged one when the caller has it (see the module docstring), otherwise whatever detail printed
    nh = nexthop
    if nh is None:
        for x in p.get("nexthops") or []:
            if x.get("ip") and x["ip"] not in ("0.0.0.0", "::"): nh = x["ip"]; break
    a["nexthop"] = nh
    nhs = p.get("nexthops") or []
    if nhs and nhs[0].get("afi"): a["nexthop_afi"] = nhs[0]["afi"]
    if nhs and nhs[0].get("hostname"): a["nexthop_hostname"] = nhs[0]["hostname"]
    if p.get("lastUpdate", {}).get("epoch"): a["last_update"] = p["lastUpdate"]["epoch"]
    a = {k: v for k, v in a.items() if v is not None}

    origin_as = None
    m = re.findall(r"\d+", a.get("as_path") or "")
    if m: origin_as = int(m[-1])
    origin_node = None
    if resolver:
        origin_node = (resolver.router_ids.get(a.get("originator_id")) or resolver.node_of_sid(a.get("sid"))
                       or resolver.node_of_ip(a.get("nexthop")) or (resolver.pe_of_rd(a["imported_from"]) if a.get("imported_from") else None)
                       or resolver.pe_of_rd(rd))
    return {"attrs": a, "peer": peer_id, "peer_name": resolver.node_of_ip(peer_id) if resolver else None,
            "origin_node": origin_node, "origin_as": origin_as, "nexthop": a.get("nexthop"), "best": a.get("best")}


def _rd_tables(doc):
    """Both shapes FRR uses for a VPN table: {routes: {routeDistinguishers: {rd: {...}}}} and {rd: {...}} at the top."""
    if isinstance(doc.get("routes"), dict) and "routeDistinguishers" in doc["routes"]: return doc["routes"]["routeDistinguishers"]
    return {k: v for k, v in doc.items() if re.match(r"^[\w.]+:\d+$", k) and isinstance(v, dict)}


def _prefix_tables(doc):
    r = doc.get("routes", doc)
    return {k: v for k, v in r.items() if "/" in k and isinstance(v, list)}


def _peer_of(p):
    """The peer an FRR path came from, in either shape (`peerId` in a table, `peer.peerId` in a detail view)."""
    peer = p.get("peer")
    pid = (peer.get("peerId") if isinstance(peer, dict) else None) or p.get("peerId")
    return "" if pid in ("(unspec)", "::", "0.0.0.0", None) else pid


def _paths(entry):
    """A prefix's paths: a list, or the single-prefix detail shape {prefix, paths: [...]}."""
    if isinstance(entry, dict): return entry.get("paths") or []
    return entry or []


class RouterAPI:
    """The router's own HTTPS API (VyOS `service https api`): POST /show with an op-mode path, JSON back.

    Self-signed certificates on the OOB network, so verification is off — the API is reachable only from the looking
    glass and the lab host (`allow-client`), and the key is a lab key like the vyos/vyos logins."""

    def __init__(self, host, key, timeout=45):
        self.host, self.key, self.timeout = host, key, timeout
        self.url = f"https://{host}"

    def show(self, path, parse=True):
        import requests, urllib3
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
        r = requests.post(f"{self.url}/show", data={"key": self.key, "data": json.dumps({"op": "show", "path": list(path)})},
                          verify=False, timeout=self.timeout)
        r.raise_for_status()
        body = r.json()
        if not body.get("success"): raise RuntimeError(f"{self.host}: show {' '.join(path)}: {str(body.get('error')).strip()}")
        data = body.get("data") or ""
        return _json(data) if parse else data


def rib_entries(doc, afi, vrf, node=None):
    """FRR's `show ip route ... json` -> the looking glass's shape: one row per route, the winning paths marked."""
    out = []
    for prefix, routes in (doc or {}).items():
        if not isinstance(routes, list): continue
        seen = {}
        for r in routes:
            nhs = [n for n in (r.get("nexthops") or []) if n.get("active") or n.get("fib") or True]
            nh = next((n.get("ip") or n.get("interfaceName") for n in nhs), None)
            a = {"protocol": r.get("protocol"), "distance": r.get("distance"), "metric": r.get("metric"),
                 "selected": bool(r.get("selected")), "installed": bool(r.get("installed")),
                 "nexthops": [{"ip": n.get("ip"), "interface": n.get("interfaceName"), "vrf": n.get("vrf"),
                               "active": bool(n.get("active")), "recursive": bool(n.get("recursive")),
                               "seg6": n.get("seg6"), "seg6local": n.get("seg6local"), "labels": n.get("labels")}
                              for n in nhs],
                 "uptime": r.get("uptime"), "table": r.get("table"), "internal_status": r.get("internalStatus")}
            segs = [s for n in a["nexthops"] for s in ([n["seg6"]] if isinstance(n.get("seg6"), str) else (n.get("seg6") or []))]
            if segs: a["sid"] = segs[0]
            disc = f"{r.get('protocol')}|{nh or '-'}"
            seen[disc] = seen.get(disc, 0) + 1
            if seen[disc] > 1: disc = f"{disc}#{seen[disc]}"
            out.append({"afi": afi, "safi": "rib", "vrf": vrf, "rd": None, "prefix": prefix, "disc": disc, "peer": "",
                        "peer_name": None, "origin_node": node, "origin_as": None, "nexthop": nh,
                        "best": bool(r.get("selected")), "attrs": {k: v for k, v in a.items() if v not in (None, [], "")}})
    return out


def isis_adjacencies(doc):
    """`show isis neighbor json` -> [{neighbor, interface, state, level}] — the core as the routers themselves see it.

    FRR puts one entry per circuit and names the neighbour in `adj` as a plain string (the hostname it learnt), with
    the circuits that have no adjacency present but empty."""
    out = []
    for area in (doc or {}).get("areas", []):
        for circuit in area.get("circuits", []):
            entries = circuit.get("adj")
            entries = entries if isinstance(entries, list) else ([entries] if entries else [])
            for adj in entries:
                if isinstance(adj, str):
                    out.append({"neighbor": adj, "interface": circuit.get("interface"), "state": circuit.get("state"),
                                "level": circuit.get("level"), "expires": circuit.get("expires-in")})
                elif isinstance(adj, dict):
                    out.append({"neighbor": adj.get("adj") or adj.get("system-id"), "interface": adj.get("interface") or circuit.get("interface"),
                                "state": adj.get("state") or adj.get("adj-state"), "level": adj.get("level"),
                                "expires": adj.get("expires-in")})
    return out


class Collector:
    """Reads both views and reconciles them into the store; one thread per cadence."""

    def __init__(self, cfg, store, log=print):
        self.cfg, self.store, self.log = cfg, store, log
        self.resolver = Resolver(cfg)
        self.peers = {}            # the collector's BGP sessions, as of the last read
        self.adjacency = {}        # node -> IS-IS adjacencies, as the routers report them
        self.stop = threading.Event()
        self.last = {}

    # ---- the collector's own table ---------------------------------------------------------------------------
    def vtysh(self, cmd, timeout=60):
        r = subprocess.run(VTYSH + [cmd], capture_output=True, text=True, timeout=timeout)
        if r.returncode != 0: raise RuntimeError(f"vtysh: {cmd}: {r.stderr.strip() or r.stdout.strip()}")
        return r.stdout

    def vtysh_json(self, cmd, timeout=60):
        return _json(self.vtysh(cmd, timeout))

    def read_vpn(self, afi):
        """(paths, ) of the whole VPN table for one AFI, merged from the table and the detail view."""
        table = self.vtysh_json(f"show bgp {afi} vpn json")
        detail = self.vtysh_json(f"show bgp {afi} vpn detail json")
        # the two views are matched per (RD, prefix, peer) rather than by position, so a different path order in one of
        # them cannot hand a path the next hop of its sibling
        nexthops, seen_nh = {}, {}
        for rd, pfxs in _rd_tables(table).items():
            for prefix, entry in _prefix_tables(pfxs).items():
                for p in _paths(entry):
                    nh = next((x.get("ip") for x in (p.get("nexthops") or []) if x.get("ip") not in (None, "0.0.0.0", "::")), None)
                    k = (rd, prefix, _peer_of(p)); seen_nh[k] = seen_nh.get(k, 0) + 1
                    nexthops[(*k, seen_nh[k])] = nh
        out = []
        for rd, pfxs in _rd_tables(detail).items():
            vrf = self.resolver.vrf_of_rd(rd)
            for prefix, entry in _prefix_tables(pfxs).items():
                seen, occ = {}, {}
                for i, p in enumerate(_paths(entry)):
                    pk = (rd, prefix, _peer_of(p)); occ[pk] = occ.get(pk, 0) + 1
                    n = normalise(p, rd=rd, resolver=self.resolver, nexthop=nexthops.get((*pk, occ[pk])))
                    # the discriminator only has to separate paths that share a peer, and it has to be the *same* one
                    # every poll: counting per peer keeps it stable when FRR hands the paths back in another order
                    disc = n["nexthop"] or "-"
                    k = (n["peer"], disc); seen[k] = seen.get(k, 0) + 1
                    if seen[k] > 1: disc = f"{disc}#{seen[k]}"
                    out.append({"afi": afi, "safi": "vpn", "vrf": vrf, "rd": rd, "prefix": prefix, "disc": disc,
                                "via": "rr-session", **n})
        return out

    def read_peers(self):
        """The collector's BGP sessions: state, uptime, what each reflector has sent."""
        js = self.vtysh_json("show bgp neighbors json")
        peers = {}
        for ip, n in js.items():
            if not isinstance(n, dict): continue
            afs = {}
            for af, d in (n.get("addressFamilyInfo") or {}).items():
                afs[af] = {"accepted": d.get("acceptedPrefixCounter"), "sent": d.get("sentPrefixCounter"),
                           "advertised": d.get("advertisedPrefixCounter")}
            peers[ip] = {"ip": ip, "name": self.resolver.node_of_ip(ip) or n.get("hostname"), "remote_as": n.get("remoteAs"),
                         "state": n.get("bgpState"), "uptime": n.get("bgpTimerUpEstablishedEpoch"),
                         "established_count": n.get("connectionsEstablished"), "dropped": n.get("connectionsDropped"),
                         "router_id": n.get("remoteRouterId"), "hostname": n.get("hostname"),
                         "messages": {"rx": (n.get("messageStats") or {}).get("totalRecv"), "tx": (n.get("messageStats") or {}).get("totalSent")},
                         "families": afs, "local": n.get("hostLocal"), "port": n.get("portForeign"),
                         "graceful_restart": (n.get("gracefulRestartInfo") or {}).get("endOfRibSend")}
        self.peers = peers
        return peers

    def collect_local(self):
        t0 = time.time(); total = 0; result = {}
        try:
            for afi, safi in [tuple(f) for f in self.cfg["collector"]["families"]]:
                paths = self.read_vpn(afi)
                r = self.store.reconcile("collector", paths, scope={"afi": [afi], "safi": [safi]})
                result[f"{afi} {safi}"] = r; total += r["total"]
                by_vrf = {}
                for p in paths: by_vrf[p["vrf"] or "-"] = by_vrf.get(p["vrf"] or "-", 0) + 1
                for vrf, n in by_vrf.items(): self.store.sample("collector", "paths", n, afi=afi, safi=safi, vrf=vrf)
            peers = self.read_peers()
            for ip, p in peers.items():
                self.store.sample("collector", "session_up", 1 if p["state"] == "Established" else 0, peer=p["name"] or ip)
                for af, d in p["families"].items():
                    if d.get("accepted") is not None: self.store.sample("collector", "accepted", d["accepted"], peer=p["name"] or ip, af=af)
            self.store.note_poll("collector", True, time.time() - t0, total, via="rr-session",
                                 detail={"peers": [{"name": p["name"] or p["ip"], "state": p["state"]} for p in peers.values()]})
        except Exception as e:                                   # noqa: BLE001 — a failed read must not stop the loop
            self.store.note_poll("collector", False, time.time() - t0, 0, f"{e.__class__.__name__}: {e}", via="rr-session")
            self.log(f"collector read failed: {e}")
            return None
        self.last["collector"] = time.time()
        return result

    # ---- a device: its RIB (API), its per-VRF BGP tables (SSH) and its IS-IS adjacencies (API) -----------------
    def api(self, dev):
        key = (self.cfg.get("api") or {}).get("key")
        return RouterAPI(dev["mgmt_ip"], key) if key else None

    def read_device_rib(self, dev):
        """The router's own routing table, per VRF and family, straight from its HTTPS API (JSON)."""
        api = self.api(dev)
        if not api: return 0, "no API key configured"
        total = 0
        vrfs = list(dev["vrfs"]) + ["default"]
        for vrf in vrfs:
            for afi, cmd in (("ipv4", "ip"), ("ipv6", "ipv6")):
                path = [cmd, "route"] + ([] if vrf == "default" else ["vrf", vrf]) + ["json"]
                try:
                    doc = api.show(path)
                except Exception as e:                           # noqa: BLE001 — a family a node does not carry
                    self.log(f"{dev['name']}: {e}"); continue
                rows = rib_entries(doc, afi, vrf, node=dev["name"])
                for r in rows: r["via"] = "router-api"
                self.store.reconcile(dev["name"], rows, scope={"vrf": [vrf], "afi": [afi], "safi": ["rib"]})
                self.store.sample(dev["name"], "rib_routes", len(rows), afi=afi, vrf=vrf)
                by_proto = {}
                for r in rows: by_proto[r["attrs"].get("protocol") or "?"] = by_proto.get(r["attrs"].get("protocol") or "?", 0) + 1
                for proto, n in by_proto.items(): self.store.sample(dev["name"], "rib_routes_by_protocol", n, afi=afi, vrf=vrf, protocol=proto)
                total += len(rows)
        return total, None

    def read_device_isis(self, dev):
        """IS-IS adjacencies, so the map can show the core as the routers see it rather than as the model describes it."""
        api = self.api(dev)
        if not api or (self.cfg["nodes"].get(dev["name"]) or {}).get("role") not in ("pe", "p"): return None
        try: adj = isis_adjacencies(api.show(["isis", "neighbor", "json"]))
        except Exception as e: self.log(f"{dev['name']}: isis: {e}"); return None      # noqa: BLE001
        self.store.sample(dev["name"], "isis_adjacencies", len([a for a in adj if (a.get("state") or "").lower().startswith("up")]))
        return adj

    def read_device_bgp(self, dev):
        """The per-VRF BGP tables. Over SSH: VyOS's op-mode API has no `json` for `show bgp vrf <x> ipv4 unicast`, and
        the text form loses the attributes — so this one view is read with vtysh, and says so (`router-ssh`)."""
        import paramiko
        ssh = self.cfg["ssh"]
        c = paramiko.SSHClient(); c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        c.connect(dev["mgmt_ip"], username=ssh["username"], password=ssh["password"], timeout=20, look_for_keys=False, allow_agent=False)
        try:
            total = 0
            for vrf in dev["vrfs"]:
                for afi, safi in [tuple(f) for f in dev["families"]]:
                    cmd = f"vtysh -c 'show bgp vrf {vrf} {afi} {safi} detail json'"
                    _, out, err = c.exec_command(cmd, timeout=90)
                    text = out.read().decode(errors="replace"); rc = out.channel.recv_exit_status()
                    if rc != 0:
                        raise RuntimeError(f"{cmd}: rc={rc} {err.read().decode(errors='replace')[:200]}")
                    doc = _json(text)
                    paths = []
                    for prefix, entry in _prefix_tables(doc).items():
                        seen = {}
                        for i, p in enumerate(_paths(entry)):
                            n = normalise(p, rd=None, resolver=self.resolver)
                            # an imported VPN route has no peer: its source RD is what tells two of them apart
                            disc = "|".join(x for x in (n["nexthop"], p.get("importedFrom")) if x) or "-"
                            k = (n["peer"], disc); seen[k] = seen.get(k, 0) + 1
                            if seen[k] > 1: disc = f"{disc}#{seen[k]}"
                            paths.append({"afi": afi, "safi": safi, "vrf": vrf, "rd": None, "prefix": prefix,
                                          "disc": disc, "via": "router-ssh", **n})
                    self.store.reconcile(dev["name"], paths, scope={"vrf": [vrf], "afi": [afi], "safi": [safi]})
                    self.store.sample(dev["name"], "paths", len(paths), afi=afi, safi=safi, vrf=vrf)
                    total += len(paths)
            return total
        finally:
            c.close()

    def read_device_vpn(self, dev):
        """The router's *own* VPN table, through its API — the same routes the collector holds from the session, but
        fetched from the box. Two answers to the same question is the point: the page can show them side by side.
        The API only offers the table form (VyOS's op-mode has no `detail json`), so these rows carry the next hop and
        the AS path but not the route targets or the SRv6 SID; the session's rows carry everything."""
        api = self.api(dev)
        if not api: return 0
        total = 0
        for afi in ("ipv4", "ipv6"):
            try: doc = api.show(["bgp", afi, "vpn", "json"])
            except Exception as e: self.log(f"{dev['name']}: vpn {afi}: {e}"); continue     # noqa: BLE001
            rows = []
            for rd, pfxs in _rd_tables(doc).items():
                vrf = self.resolver.vrf_of_rd(rd)
                for prefix, entry in _prefix_tables(pfxs).items():
                    seen = {}
                    for p in _paths(entry):
                        n = normalise(p, rd=rd, resolver=self.resolver)
                        disc = n["nexthop"] or "-"
                        k = (n["peer"], disc); seen[k] = seen.get(k, 0) + 1
                        if seen[k] > 1: disc = f"{disc}#{seen[k]}"
                        rows.append({"afi": afi, "safi": "vpn", "vrf": vrf, "rd": rd, "prefix": prefix, "disc": disc,
                                     "via": "router-api", **n})
            self.store.reconcile(dev["name"], rows, scope={"afi": [afi], "safi": ["vpn"]})
            self.store.sample(dev["name"], "paths", len(rows), afi=afi, safi="vpn", vrf="-")
            total += len(rows)
        return total

    def read_device(self, dev):
        """Everything one router has to say, and how each part of it was obtained."""
        want = dev.get("collect") or {"rib": True, "isis": False, "vpn": False, "bgp_vrf": True}
        got = {"bgp": 0, "rib": 0, "vpn": 0, "isis": None, "via": []}
        if want.get("rib"):
            got["rib"] = self.read_device_rib(dev)[0]
        if want.get("vpn"):
            got["vpn"] = self.read_device_vpn(dev)
        if want.get("isis"):
            adj = self.read_device_isis(dev)
            if adj is not None: got["isis"] = adj; self.adjacency[dev["name"]] = adj
        if got["rib"] or got["vpn"] or got["isis"] is not None: got["via"].append("router-api")
        if want.get("bgp_vrf") and dev.get("vrfs"):
            got["bgp"] = self.read_device_bgp(dev)
            if got["bgp"]: got["via"].append("router-ssh")
        return got

    def collect_devices(self, only=None):
        devs = [d for d in self.cfg["devices"] if not only or d["name"] in only]
        threads, results = [], {}

        def one(d):
            t0 = time.time()
            try:
                got = self.read_device(d)
                self.store.note_poll(d["name"], True, time.time() - t0, got["bgp"] + got["rib"] + got["vpn"],
                                     via="+".join(got["via"]) or None,
                                     detail={"bgp": got["bgp"], "rib": got["rib"], "vpn": got["vpn"], "isis": got["isis"]})
                results[d["name"]] = got
            except Exception as e:                               # noqa: BLE001 — one unreachable router is not an outage
                self.store.note_poll(d["name"], False, time.time() - t0, 0, f"{e.__class__.__name__}: {e}")
                results[d["name"]] = None
        for d in devs:
            t = threading.Thread(target=one, args=(d,), daemon=True, name=f"poll-{d['name']}"); t.start(); threads.append(t)
        for t in threads: t.join(timeout=180)
        self.last["devices"] = time.time()
        return results

    # ---- loops ------------------------------------------------------------------------------------------------
    def run(self):
        for name, fn, period in (("local", self.collect_local, self.cfg["poll"]["local"]),
                                 ("devices", self.collect_devices, self.cfg["poll"]["devices"]),
                                 ("prune", self.store.prune, 3600)):
            threading.Thread(target=self._loop, args=(name, fn, period), daemon=True, name=f"lgd-{name}").start()

    def _loop(self, name, fn, period):
        while not self.stop.is_set():
            t0 = time.time()
            try: fn()
            except Exception as e: self.log(f"{name} loop: {e.__class__.__name__}: {e}")   # noqa: BLE001
            self.stop.wait(max(1.0, period - (time.time() - t0)))
