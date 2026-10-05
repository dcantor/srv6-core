#!/usr/bin/env python3
"""BMP (RFC 7854, Loc-RIB per RFC 9069): the route reflectors stream their tables to the looking glass.

Each reflector connects to us (`bmp connect`, FRR's `-M bmp` module) and sends, per peer, every route as it arrives:

  loc-rib      the reflector's own best paths — what it reflects to the PEs, so the core's VPN table, as before
  pre-policy   its Adj-RIB-In from each PE before any policy — what each PE actually *sent*, which no BGP session to the
               reflector can show (a client only ever gets the reflector's best path)

plus Peer Up / Peer Down for each of the reflector's sessions, with the OPEN messages and the reason. Nothing is
polled: the tables here are the reflectors' own, kept current message by message, and the collector reconciles
snapshots of them into the store like any other view.

Only what this lab needs is decoded: VPNv4 / VPNv6 (SAFI 128) and plain unicast in MP_REACH / MP_UNREACH, the usual
path attributes, route targets, and the BGP Prefix-SID's SRv6 L3 Service TLV (RFC 9252) — the SID, its endpoint
behaviour and its structure, including transposition (the function bits travel in the label field and are put back
into the SID here).
"""
import ipaddress, socket, struct, threading, time

# message types (RFC 7854 4.1)
ROUTE_MONITORING, STATS, PEER_DOWN, PEER_UP, INITIATION, TERMINATION, MIRRORING = range(7)
PEER_TYPES = {0: "global", 1: "rd", 2: "local", 3: "loc-rib"}
ORIGINS = {0: "IGP", 1: "EGP", 2: "incomplete"}
AFI = {1: "ipv4", 2: "ipv6"}
SAFI = {1: "unicast", 128: "vpn"}
PEER_DOWN_REASONS = {1: "local system closed, NOTIFICATION sent", 2: "local system closed, no NOTIFICATION",
                     3: "remote system closed, NOTIFICATION received", 4: "remote system closed, no NOTIFICATION",
                     5: "peer de-configured", 6: "Loc-RIB no longer monitored"}
BEHAVIOURS = {0x0012: "End.DT6", 0x0013: "End.DT4", 0x0014: "End.DT46", 0x003E: "uDT6", 0x003F: "uDT4", 0x0040: "uDT46",
              0x0010: "End.DX6", 0x0011: "End.DX4", 0x003C: "uDX6", 0x003D: "uDX4", 0xFFFF: "opaque"}


class BMPError(ValueError):
    pass


# ---- decoding --------------------------------------------------------------------------------------------------
def _rd(b):
    """An 8-byte route distinguisher in FRR's text form."""
    t = struct.unpack("!H", b[:2])[0]
    if t == 0: return "%d:%d" % struct.unpack("!HI", b[2:8])
    if t == 1: return "%s:%d" % (ipaddress.IPv4Address(b[2:6]), struct.unpack("!H", b[6:8])[0])
    if t == 2: return "%d:%d" % struct.unpack("!IH", b[2:8])
    return b.hex()


def _ext_community(b):
    """One 8-byte extended community in FRR's text form (RT:…, SoO:…), raw hex for what this lab does not use."""
    t, st = b[0] & 0x3F, b[1]
    name = {0x02: "RT", 0x03: "SoO"}.get(st)
    if name and t == 0x00: return "%s:%d:%d" % (name, *struct.unpack("!HI", b[2:8]))
    if name and t == 0x01: return "%s:%s:%d" % (name, ipaddress.IPv4Address(b[2:6]), struct.unpack("!H", b[6:8])[0])
    if name and t == 0x02: return "%s:%d:%d" % (name, *struct.unpack("!IH", b[2:8]))
    return "0x" + b.hex()


def _prefix_sid(b):
    """BGP Prefix-SID (attribute 40): the SRv6 L3 Service TLV (5) -> {sid, behavior, structure}."""
    out, i = {}, 0
    while i + 3 <= len(b):
        t, ln = b[i], struct.unpack("!H", b[i + 1:i + 3])[0]; v = b[i + 3:i + 3 + ln]; i += 3 + ln
        if t != 5: continue
        j = 1                                                   # one reserved byte, then sub-TLVs
        while j + 3 <= len(v):
            st, sl = v[j], struct.unpack("!H", v[j + 1:j + 3])[0]; sv = v[j + 3:j + 3 + sl]; j += 3 + sl
            if st != 1 or len(sv) < 21: continue                # SRv6 SID Information
            out["sid"] = str(ipaddress.IPv6Address(sv[1:17]))
            beh = struct.unpack("!H", sv[18:20])[0]
            out["behavior"] = BEHAVIOURS.get(beh, f"0x{beh:04x}")
            k = 21
            while k + 3 <= len(sv):
                sst, ssl = sv[k], struct.unpack("!H", sv[k + 1:k + 3])[0]; ssv = sv[k + 3:k + 3 + ssl]; k += 3 + ssl
                if sst == 1 and len(ssv) >= 6:                  # SID Structure
                    out["structure"] = dict(zip(("locatorBlockLen", "locatorNodeLen", "functionLen", "argumentLen",
                                                 "transpositionLen", "transpositionOffset"), ssv[:6]))
    return out


def transpose(sid, label24, structure):
    """Put the bits that travelled in the label field back into the SID (RFC 9252 4: the transposed bits fill the
    label field from its most significant bit)."""
    tl, off = (structure or {}).get("transpositionLen", 0), (structure or {}).get("transpositionOffset", 0)
    if not sid or not tl: return sid
    bits = (label24 >> (24 - tl)) & ((1 << tl) - 1)
    return str(ipaddress.IPv6Address(int(ipaddress.IPv6Address(sid)) | (bits << (128 - off - tl))))


def _nlri(b, afi, safi, withdraw=False):
    """Prefixes of one NLRI field: [(rd, prefix, label24)] — one label for VPN (an SRv6 VPN carries exactly one: the
    transposed function, or the MPLS-style label FRR still fills in)."""
    out, i, alen = [], 0, 4 if afi == 1 else 16
    while i < len(b):
        bits = b[i]; i += 1; label = rd = None
        if safi == 128:
            label = int.from_bytes(b[i:i + 3], "big"); i += 3; bits -= 24
            rd = _rd(b[i:i + 8]); i += 8; bits -= 64
        n = (bits + 7) // 8
        raw = b[i:i + n] + bytes(alen - n); i += n
        net = ipaddress.ip_network((raw, bits), strict=False) if afi in (1, 2) else None
        out.append((rd, str(net), label))
    return out


def _nexthop(b, safi):
    """MP_REACH next hop. A VPN next hop is preceded by an 8-byte (zero) RD; a VPNv4 route may carry an IPv6 next hop
    (RFC 8950, extended next hop) — global, or global plus link-local."""
    if safi == 128: b = b[8:] if len(b) in (12, 24) else (b[8:24] if len(b) == 48 else b)
    if len(b) >= 16: return str(ipaddress.IPv6Address(b[:16]))
    if len(b) >= 4: return str(ipaddress.IPv4Address(b[:4]))
    return None


def parse_update(b, as4=True):
    """A BGP UPDATE (with its 19-byte header) -> {"attrs": {...}, "announce": [(afi, safi, rd, prefix, label)],
    "withdraw": [...], "eor": (afi, safi) | None}."""
    if len(b) < 23 or b[18] != 2: raise BMPError("not an UPDATE")
    i = 19
    wl = struct.unpack("!H", b[i:i + 2])[0]; i += 2
    withdraw = [(1, 1, rd, p, l) for rd, p, l in _nlri(b[i:i + wl], 1, 1)]; i += wl
    al = struct.unpack("!H", b[i:i + 2])[0]; i += 2; end = i + al
    attrs, announce, eor = {}, [], None
    while i < end:
        flags, t = b[i], b[i + 1]
        if flags & 0x10: ln = struct.unpack("!H", b[i + 2:i + 4])[0]; i += 4
        else: ln = b[i + 2]; i += 3
        v = b[i:i + ln]; i += ln
        if t == 1: attrs["origin"] = ORIGINS.get(v[0], str(v[0]))
        elif t == 2:
            segs, j, w = [], 0, 4 if as4 else 2
            while j + 2 <= len(v):
                st, cnt = v[j], v[j + 1]; j += 2
                asns = [int.from_bytes(v[j + w * k:j + w * (k + 1)], "big") for k in range(cnt)]; j += w * cnt
                s = " ".join(map(str, asns)); segs.append("{%s}" % s if st == 1 else s)
            attrs["as_path"] = " ".join(x for x in segs if x)
        elif t == 3: attrs["nexthop"] = str(ipaddress.IPv4Address(v[:4]))
        elif t == 4: attrs["med"] = struct.unpack("!I", v[:4])[0]
        elif t == 5: attrs["local_pref"] = struct.unpack("!I", v[:4])[0]
        elif t == 6: attrs["atomic_aggregate"] = True
        elif t == 8: attrs["communities"] = " ".join("%d:%d" % struct.unpack("!HH", v[k:k + 4]) for k in range(0, len(v), 4))
        elif t == 9: attrs["originator_id"] = str(ipaddress.IPv4Address(v[:4]))
        elif t == 10: attrs["cluster_list"] = [str(ipaddress.IPv4Address(v[k:k + 4])) for k in range(0, len(v), 4)]
        elif t == 16: attrs["ext_communities"] = " ".join(_ext_community(v[k:k + 8]) for k in range(0, len(v), 8))
        elif t == 32: attrs["large_communities"] = " ".join("%d:%d:%d" % struct.unpack("!III", v[k:k + 12]) for k in range(0, len(v), 12))
        elif t == 40: attrs["_prefix_sid"] = _prefix_sid(v)
        elif t == 14:
            afi, safi, nl = struct.unpack("!HBB", v[:4])
            attrs["nexthop"] = _nexthop(v[4:4 + nl], safi); attrs["nexthop_afi"] = "ipv6" if nl >= 16 else "ipv4"
            announce += [(afi, safi, rd, p, l) for rd, p, l in _nlri(v[5 + nl:], afi, safi)]
        elif t == 15:
            afi, safi = struct.unpack("!HB", v[:3])
            if len(v) == 3: eor = (afi, safi)
            withdraw += [(afi, safi, rd, p, l) for rd, p, l in _nlri(v[3:], afi, safi, withdraw=True)]
    announce += [(1, 1, None, p, None) for _, p, _ in _nlri(b[end:], 1, 1)]
    if not announce and not withdraw and al == 0: eor = (1, 1)
    return {"attrs": attrs, "announce": announce, "withdraw": withdraw, "eor": eor}


def _tlvs(b):
    out, i = {}, 0
    while i + 4 <= len(b):
        t, ln = struct.unpack("!HH", b[i:i + 4]); out.setdefault(t, []).append(b[i + 4:i + 4 + ln]); i += 4 + ln
    return out


def parse_peer_header(b):
    pt, flags = b[0], b[1]
    v6 = bool(flags & 0x80)
    addr = str(ipaddress.IPv6Address(b[10:26])) if v6 else str(ipaddress.IPv4Address(b[22:26]))
    asn, bgp_id = struct.unpack("!I", b[26:30])[0], str(ipaddress.IPv4Address(b[30:34]))
    sec, usec = struct.unpack("!II", b[34:42])
    loc_rib = pt == 3
    return {"type": PEER_TYPES.get(pt, str(pt)), "rd": _rd(b[2:10]) if pt == 1 else None,
            "address": None if loc_rib else addr, "asn": asn, "bgp_id": bgp_id, "ts": sec + usec / 1e6,
            # pre/post-policy (L) means nothing for Loc-RIB, where the same bit says the table is filtered (RFC 9069)
            "post_policy": bool(flags & 0x40) and not loc_rib, "as2": bool(flags & 0x20) and not loc_rib,
            "adj_rib_out": bool(flags & 0x10) and not loc_rib}


def parse_message(t, body):
    """One BMP message (without its 6-byte common header) -> a dict."""
    if t == INITIATION or t == TERMINATION:
        tl = _tlvs(body)
        if t == INITIATION:
            return {"type": "initiation", "sys_descr": (tl.get(1) or [b""])[0].decode(errors="replace"),
                    "sys_name": (tl.get(2) or [b""])[0].decode(errors="replace")}
        reason = struct.unpack("!H", tl[1][0][:2])[0] if tl.get(1) else None
        return {"type": "termination", "reason": reason}
    peer = parse_peer_header(body[:42]); rest = body[42:]
    if t == ROUTE_MONITORING:
        return {"type": "route", "peer": peer, **parse_update(rest, as4=not peer["as2"])}
    if t == PEER_UP:
        v6 = ":" in (peer["address"] or "") or peer["type"] == "loc-rib"
        local = str(ipaddress.IPv6Address(rest[:16])) if rest[:12] != bytes(10) + b"\xff\xff" and v6 else str(ipaddress.IPv4Address(rest[12:16]))
        lport, rport = struct.unpack("!HH", rest[16:20])
        sent_len = struct.unpack("!H", rest[20 + 16:20 + 18])[0] if len(rest) >= 38 else 0
        rcvd = rest[20 + sent_len:]
        rcvd_len = struct.unpack("!H", rcvd[16:18])[0] if len(rcvd) >= 18 else 0
        tl = _tlvs(rcvd[rcvd_len:])
        return {"type": "peer_up", "peer": peer, "local": local, "local_port": lport, "remote_port": rport,
                "table_name": (tl.get(3) or [b""])[0].decode(errors="replace") or None}
    if t == PEER_DOWN:
        reason = body[42] if len(body) > 42 else None
        return {"type": "peer_down", "peer": peer, "reason": reason, "reason_text": PEER_DOWN_REASONS.get(reason, str(reason))}
    if t == STATS:
        n = struct.unpack("!I", rest[:4])[0]; i, stats = 4, {}
        for _ in range(n):
            st, ln = struct.unpack("!HH", rest[i:i + 4]); v = rest[i + 4:i + 4 + ln]; i += 4 + ln
            if ln in (4, 8): stats[st] = int.from_bytes(v, "big")
        return {"type": "stats", "peer": peer, "stats": stats}
    return {"type": "mirroring" if t == MIRRORING else f"type{t}", "peer": peer}


def read_messages(sock):
    """Yield (type, body) for every message on a BMP connection until it closes."""
    buf = b""
    while True:
        while len(buf) >= 6:
            ver, ln, t = struct.unpack("!BIB", buf[:6])
            if ver != 3: raise BMPError(f"BMP version {ver}")
            if len(buf) < ln: break
            yield t, buf[6:ln]; buf = buf[ln:]
        d = sock.recv(65536)
        if not d: return
        buf += d


# ---- the live tables -----------------------------------------------------------------------------------------
def route_attrs(attrs, label):
    """The decoded attributes of one route, in the shape the looking glass stores (collect.normalise's keys where the
    two overlap, so the views read the same whichever way a path arrived)."""
    a = {k: v for k, v in attrs.items() if not k.startswith("_")}
    a.setdefault("as_path", ""); a["valid"] = True
    ps = attrs.get("_prefix_sid") or {}
    if label is not None: a["label"] = label >> 4
    if ps.get("sid"):
        a["sid"] = ps["sid"]
        if ps.get("structure"):
            a["sid_structure"] = ps["structure"]
            full = transpose(ps["sid"], label or 0, ps["structure"])
            if full != ps["sid"]: a["transposed_sid"] = full
        if ps.get("behavior"): a["behavior"] = ps["behavior"]
    return a


class Feed:
    """Every reflector's BMP stream, decoded into tables: per reflector, its Loc-RIB and each peer's Adj-RIB-In.

    `rib[rr][view][(afi, safi, rd, prefix, peer)] = attrs` with view "loc-rib" or "pre-policy" / "post-policy".
    A reflector is *synced* once it has sent End-of-RIB for what it monitors (or `grace` seconds after it connected,
    for an implementation that sends none): until then its table is not handed to the store, so a restart of lgd —
    which drops every BMP connection — does not read as every route withdrawn and announced again."""

    def __init__(self, cfg, log=print, grace=30, keep=120):
        self.cfg, self.log, self.grace, self.keep = cfg, log, grace, keep
        bmp = cfg.get("bmp") or {}
        self.port = bmp.get("port", 11019)
        self.by_ip = {p["ip"]: p for p in cfg.get("collector", {}).get("peers", [])}   # the reflectors' link addresses
        self.lock = threading.Lock()
        self.rib, self.conns, self.peers = {}, {}, {}
        self.mentioned = {}                                    # rr -> {(view, key)}: every route announced or withdrawn since it connected
        self.version = 0                                       # bumped on every change: the collector reconciles on it
        self.stop = threading.Event()

    def rr_of(self, addr):
        p = self.by_ip.get(addr.split("%")[0])
        return p["name"] if p else addr

    # the server ------------------------------------------------------------------------------------------------
    def serve(self):
        s = socket.socket(socket.AF_INET6, socket.SOCK_STREAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1); s.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)
        s.bind(("::", self.port)); s.listen(8); s.settimeout(1)
        self.log(f"bmp: listening on port {self.port}")
        while not self.stop.is_set():
            try: c, a = s.accept()
            except socket.timeout: continue
            threading.Thread(target=self.handle, args=(c, a[0]), daemon=True).start()

    def handle(self, sock, addr):
        addr = addr.removeprefix("::ffff:"); rr = self.rr_of(addr)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
        with self.lock:
            self.rib[rr] = {}; self.mentioned[rr] = set(); self.conns[rr] = {"name": rr, "address": addr, "state": "up", "since": time.time(),
                                                 "messages": 0, "routes": 0, "last": None, "eor": [], "synced": False,
                                                 "sys_name": None, "sys_descr": None}
            self.peers[rr] = {}; self.version += 1
        self.log(f"bmp: {rr} connected from {addr}")
        why = "closed"
        try:
            for t, body in read_messages(sock):
                try: m = parse_message(t, body)
                except (BMPError, ValueError, IndexError, struct.error) as e:
                    self.log(f"bmp: {rr}: undecodable message type {t}: {e}"); continue
                self.apply(rr, m)
        except (OSError, BMPError) as e:
            why = str(e)
        finally:
            sock.close()
            with self.lock:
                c = self.conns.get(rr)
                if c and c["address"] == addr: c.update(state="down", down_since=time.time(), down_reason=why)
                self.version += 1
            self.log(f"bmp: {rr} disconnected ({why})")

    # applying a message --------------------------------------------------------------------------------------
    def _view(self, peer):
        return "loc-rib" if peer["type"] == "loc-rib" else ("post-policy" if peer["post_policy"] else "pre-policy")

    def apply(self, rr, m):
        with self.lock:
            c = self.conns[rr]; c["messages"] += 1; c["last"] = time.time()
            t = m["type"]
            if t == "initiation": c.update(sys_name=m["sys_name"], sys_descr=m["sys_descr"]); return
            if t == "termination": c["state"] = "terminated"; return
            peer = m.get("peer") or {}; pkey = (peer.get("type"), peer.get("address"))
            if t == "peer_up":
                self.peers[rr][pkey] = {**peer, "state": "up", "since": peer["ts"] or time.time(), "local": m["local"],
                                        "table_name": m["table_name"]}
                self.version += 1; return
            if t == "peer_down":
                p = self.peers[rr].setdefault(pkey, dict(peer))
                p.update(state="down", since=peer["ts"] or time.time(), reason=m["reason_text"])
                for view in list(self.rib[rr]):          # a peer that is gone has sent nothing that still stands
                    self.rib[rr][view] = {k: v for k, v in self.rib[rr][view].items() if k[4] != peer.get("address")}
                self.version += 1; return
            if t == "stats":
                self.peers[rr].setdefault(pkey, dict(peer))["stats"] = m["stats"]; return
            if t != "route": return
            view = self.rib[rr].setdefault(self._view(peer), {})
            if m["eor"]:
                c["eor"] = sorted(set(c["eor"]) | {f"{self._view(peer)} {AFI.get(m['eor'][0])} {SAFI.get(m['eor'][1])}"})
            vname = self._view(peer)
            for afi, safi, rd, prefix, _ in m["withdraw"]:
                k = (AFI.get(afi), SAFI.get(safi), rd, prefix, peer.get("address"))
                view.pop(k, None); self.mentioned[rr].add((vname, k))
            for afi, safi, rd, prefix, label in m["announce"]:
                a = route_attrs(m["attrs"], label)
                a["peer_router_id"] = peer["bgp_id"]; a["peer_asn"] = peer["asn"]
                a["last_update"] = int(peer["ts"] or time.time())   # volatile in the store: a replay is not a change
                k = (AFI.get(afi), SAFI.get(safi), rd, prefix, peer.get("address"))
                view[k] = a; self.mentioned[rr].add((vname, k))
            c["routes"] = sum(len(v) for v in self.rib[rr].values())
            if m["announce"] or m["withdraw"]: self.version += 1

    # what the collector reads -----------------------------------------------------------------------------------
    def synced(self, rr):
        c = self.conns.get(rr)
        if not c: return False
        if c["state"] != "up":                           # gone: its last table stands for `keep` seconds, then goes
            return time.time() - c.get("down_since", 0) < self.keep
        if not c["synced"] and (c["eor"] or time.time() - c["since"] >= self.grace): c["synced"] = True
        return c["synced"]

    def snapshot(self, view):
        """{rr: [(key, attrs)]} for one view, from the reflectors whose table can be trusted right now. A reflector that
        is connected but has not finished its initial dump is left out, and so is reported as `None` (not empty)."""
        with self.lock:
            out = {}
            for rr in self.conns:
                out[rr] = list(self.rib.get(rr, {}).get(view, {}).items()) if self.synced(rr) else None
            return out

    def mentioned_keys(self, rr, view):
        """Every route of one view this reflector has announced or withdrawn since it connected: a route among them that
        it does not hold now was withdrawn, and must not be filled back in from anywhere else."""
        with self.lock:
            return {k for v, k in self.mentioned.get(rr, set()) if v == view}

    def status(self):
        with self.lock:
            return {rr: {**{k: v for k, v in c.items()}, "synced": self.synced(rr),
                         "peers": [{"type": p.get("type"), "address": p.get("address"), "asn": p.get("asn"),
                                    "bgp_id": p.get("bgp_id"), "state": p.get("state"), "since": p.get("since"),
                                    "reason": p.get("reason"), "table_name": p.get("table_name")}
                                   for p in self.peers.get(rr, {}).values()],
                         "views": {v: len(t) for v, t in self.rib.get(rr, {}).items()}}
                    for rr, c in self.conns.items()}
