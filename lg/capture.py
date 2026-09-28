#!/usr/bin/env python3
"""Packet capture on a link: tcpdump on one of the link's ends, bounded, decoded, and kept for download as a pcap.

A link in the model (`topology.links`: a / a_port, b / b_port) is captured at the router end it names — over SSH, as
the live query does: `sudo tcpdump -i <port> -c <packets> -s <snaplen> -w -` under `timeout <seconds>`, written to a
temporary file on the router, decoded there (`tcpdump -nn -tttt -vv -r`), sent back with the file itself (base64), and
the file removed. Nothing is left on the router and nothing but the model's own interfaces can be named.

`ping_peer` also pings the far end of the link (five pings, from the port's VRF if it has one) while the capture runs, so a
quiet link still shows something. Bounds: 1–1000 packets, 1–60 seconds, a 64–1600 byte snapshot; one capture at a time per router; the newest KEEP
captures are kept in memory for download. A filter is a pcap (BPF) expression from a restricted alphabet, passed
quoted."""
import base64, re, shlex, threading, time, uuid

KEEP = 30
FILTER_OK = re.compile(r"^[A-Za-z0-9 .:/\[\]()!&|=<>*+-]{0,200}$")
PRESETS = {                                   # the filters the page offers by name
    "all": "",
    "srv6": "ip6 and (ip6[6] == 4 or ip6[6] == 41 or ip6[6] == 43)",   # tenant traffic encapsulated in IPv6 (uSID or SRH)
    "isis": "isis",
    "bgp": "tcp port 179",
    "bfd": "udp port 3784 or udp port 4784",
    "icmp": "icmp or icmp6",
    "data": "(ip or ip6) and not (udp port 3784 or udp port 4784) and not tcp port 179 "
            "and not (icmp6 and ip6[40] >= 133 and ip6[40] <= 137)",       # no BFD, BGP or neighbour discovery; IS-IS is not IP
}
_TS = re.compile(r"^\d{4}-\d\d-\d\d \d\d:\d\d:\d\d\.\d+ ")
_MARK = "=====LG-PCAP-BASE64====="


class Captures:
    def __init__(self, cfg, devices, run):
        self.cfg, self.devices, self.run = cfg, devices, run      # run(dev, command, timeout) -> text
        self.links = (cfg.get("topology") or {}).get("links") or []
        self.store, self.busy, self.lock = {}, set(), threading.Lock()

    def ends(self, link):
        """The link's ends a capture can run on: a router the looking glass reads (a host or the collector cannot)."""
        out = []
        for side in ("a", "b"):
            n, port = link[side], link[f"{side}_port"]
            if n in self.devices and port:
                out.append({"device": n, "interface": port})
        return out

    def find_link(self, device, interface):
        return next((l for l in self.links if (l["a"] == device and l["a_port"] == interface)
                     or (l["b"] == device and l["b_port"] == interface)), None)

    def start(self, body):
        device, interface = body.get("device", ""), body.get("interface", "")
        link = self.find_link(device, interface)
        if device not in self.devices or not link:
            raise ValueError(f"{device} {interface} is not an end of a link in the model")
        ping = bool(body.get("ping_peer"))
        packets = _bound(body.get("packets", 100), 1, 1000, "packets")
        seconds = _bound(body.get("seconds", 10), 1, 60, "seconds")
        snaplen = _bound(body.get("snaplen", 256), 64, 1600, "snaplen")
        preset = body.get("preset") or ""
        flt = PRESETS[preset] if preset in PRESETS else (body.get("filter") or "").strip()
        if preset not in PRESETS and preset:
            raise ValueError(f"unknown preset {preset!r}: {', '.join(PRESETS)}")
        if not FILTER_OK.match(flt):
            raise ValueError("the filter may use letters, digits, spaces and . : / [ ] ( ) ! & | = < > * + - only")
        with self.lock:
            if device in self.busy:
                raise RuntimeError(f"a capture is already running on {device}")
            self.busy.add(device)
        try:
            t0 = time.time()
            peer_ip = ((link.get("b_ip") if link["a"] == device else link.get("a_ip")) or "").split("/")[0]
            # the port may sit in a tenant's VRF (a PE's or a CE's access side): ping from inside it
            q = shlex.quote(interface)
            gen = (f"(v=$(ip -o link show dev {q} | grep -o 'master [^ ]*' | cut -d' ' -f2); sleep 1; "
                   f"sudo ${{v:+ip vrf exec $v}} ping -c 5 -i 0.5 -W 1 {shlex.quote(peer_ip)} >/dev/null 2>&1) & ") if ping and peer_ip else ""
            cmd = (f"{gen}f=$(mktemp /tmp/lgcap.XXXXXX); "
                   f"sudo timeout {seconds} tcpdump -i {shlex.quote(interface)} -nn -s {snaplen} -c {packets} -U -w - "
                   f"{shlex.quote(flt) if flt else ''} 2>/dev/null > $f; "
                   f"tcpdump -nn -tttt -vv -r $f 2>/dev/null; echo {_MARK}; base64 -w0 $f; echo; rm -f $f; wait")
            out = self.run(self.devices[device], cmd, seconds + 45)
            text, _, b64 = out.partition(_MARK)
            pcap = base64.b64decode(b64.strip() or b"")
            pkts = _packets(text)
            cid = uuid.uuid4().hex[:12]
            other = link["b"] if link["a"] == device else link["a"]
            rec = {"id": cid, "device": device, "interface": interface, "peer": other,
                   "peer_interface": link["b_port"] if link["a"] == device else link["a_port"],
                   "prefix": link.get("prefix"), "tenant": link.get("tenant"), "filter": flt, "preset": preset or None,
                   "packets_requested": packets, "seconds": seconds, "snaplen": snaplen, "ping_peer": bool(gen), "peer_ip": peer_ip,
                   "started": t0, "took": round(time.time() - t0, 2), "count": len(pkts), "bytes": len(pcap),
                   "packets": pkts}
            with self.lock:
                self.store[cid] = {"meta": rec, "pcap": pcap}
                for old in sorted(self.store, key=lambda k: self.store[k]["meta"]["started"])[:-KEEP]:
                    del self.store[old]
            return rec
        finally:
            with self.lock:
                self.busy.discard(device)

    def list(self):
        with self.lock:
            return [{k: v for k, v in s["meta"].items() if k != "packets"}
                    for s in sorted(self.store.values(), key=lambda s: -s["meta"]["started"])]

    def get(self, cid):
        with self.lock:
            return self.store.get(cid)

    def filename(self, meta):
        stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(meta["started"]))
        return f"{self.cfg.get('lab', 'lab')}-{meta['device']}-{meta['interface']}-{stamp}.pcap"


def _bound(v, lo, hi, name):
    try:
        v = int(v)
    except (TypeError, ValueError):
        raise ValueError(f"{name} must be a whole number")
    if not lo <= v <= hi:
        raise ValueError(f"{name} must be between {lo} and {hi}")
    return v


def _packets(text):
    """tcpdump -tttt -vv output → [{no, time, summary, proto, src, dst, detail}]: a packet starts with its timestamp."""
    pkts, cur = [], None
    for line in text.splitlines():
        if _TS.match(line):
            cur = {"no": len(pkts) + 1, "time": line[:26], "summary": line[27:].strip(), "detail": []}
            s = cur["summary"]
            m = re.search(r"(\S+) > (\S+): ", s)
            cur["src"], cur["dst"] = (m.group(1), m.group(2)) if m else ("", "")
            cur["proto"] = _proto(s)
            pkts.append(cur)
        elif cur is not None and line.strip():
            cur["detail"].append(line.rstrip())
    for p in pkts:
        lines = [p["summary"]] + p["detail"]
        m = next((m for m in (re.search(r"(\S+) > (\S+): (.*)$", l.strip()) for l in lines[:3]) if m), None)
        if m:                                          # IPv4 under -vv (and an SRv6 packet's inner header): the second line
            if not p["src"]:
                p["src"], p["dst"] = m.group(1), m.group(2)
            p["info"] = m.group(3)[:160]
        else:
            p["info"] = p["summary"][:160]
        inner = next((m for m in (re.search(r"(\S+) > (\S+): (.*)$", l.strip()) for l in p["detail"][:2]) if m), None)
        if p["proto"].startswith("SRv6") and inner:    # the tenant's own packet, inside the encapsulation
            p["info"] = f"inside: {inner.group(1)} > {inner.group(2)}: {inner.group(3)}"[:160]
        p["detail"] = "\n".join(p["detail"])
    return pkts


def _proto(s):
    """What a packet is, outer header first: SRv6 (an IPv6 packet carrying a routing header, IPv4 or IPv6) names what it carries."""
    inner = lambda: next((n for p, n in ((r"ICMP6|icmp6", "ICMPv6"), (r"ICMP", "ICMP"), (r"\.179[ :]", "BGP"), (r"UDP|udp", "UDP"),
                                          (r"Flags \[", "TCP")) if re.search(p, s.split(")", 1)[-1])), "data")
    if re.search(r"next-header Routing \(43\)|RT6 \(", s):
        return f"SRv6 (SRH) → {inner()}"
    if re.search(r"next-header IPIP|next-header IPv4 \(4\)", s):
        return f"SRv6 → IPv4 {inner()}"
    if re.search(r"next-header IPv6 \(41\)", s):
        return f"SRv6 → IPv6 {inner()}"
    for pat, name in ((r"\.3784[ :]|\.4784[ :]|BFD", "BFD"), (r"\.179[ :]|BGP", "BGP"), (r"ISIS|IS-IS", "IS-IS"),
                      (r"router advertisement|router solicitation|neighbor (solicitation|advertisement)", "ICMPv6 ND"),
                      (r"ICMP6|icmp6", "ICMPv6"), (r"ICMP", "ICMP"), (r"LLDP", "LLDP"), (r"ARP", "ARP"),
                      (r"UDP", "UDP"), (r"Flags \[", "TCP"), (r"IP6", "IPv6"), (r"^IP ", "IPv4")):
        if re.search(pat, s):
            return name
    return "other"
