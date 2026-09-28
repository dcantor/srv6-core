#!/usr/bin/env python3
"""Packet capture on a link: tcpdump on one of the link's ends, streamed as it decodes, and kept as a pcap to download.

A link in the model (`topology.links`: a / a_port, b / b_port) is captured at a router end it names, over SSH:

    sudo sh -c 'echo $$ > /tmp/lgcap.<id>.pid; exec timeout <s> tcpdump -i <port> -c <n> -s <snap> -U -w - <filter>' \\
        | tee /tmp/lgcap.<id> | tcpdump -nn -tttt -vv -l -r -

The first tcpdump writes the raw packets; `tee` keeps them as the pcap; the second decodes them line by line as they
arrive, so the page shows packets while the capture runs. Stop sends SIGINT to the capture (its pid is in the pid file),
the pipeline drains, and the pcap comes back (base64) before the files are removed. Nothing is left on the router, and
nothing but the model's own link interfaces can be named.

A path capture starts one such capture on every link of a prefix's path at once, and can send five pings along it from
the first router (in the tenant's VRF), so one packet can be followed hop by hop: `follow` keeps pings on the access
links and the SRv6-encapsulated packets in the core.

Bounds: 1–1000 packets, 1–60 seconds, a 64–1600 byte snapshot; one capture at a time per router port; the newest KEEP
captures are kept in memory. A filter is a pcap (BPF) expression from a restricted alphabet, passed quoted.
`ping_peer` pings the far end of the link (five pings, from the port's VRF if it has one) while the capture runs."""
import base64, ipaddress, re, shlex, threading, time, uuid

KEEP = 60
FILTER_OK = re.compile(r"^[A-Za-z0-9 .:/\[\]()!&|=<>*+-]{0,200}$")
NAME_OK = re.compile(r"^[A-Za-z0-9_-]{1,40}$")
SRV6 = "ip6 and (ip6[6] == 4 or ip6[6] == 41 or ip6[6] == 43)"   # tenant traffic encapsulated in IPv6 (uSID or SRH)
PRESETS = {                                   # the filters the page offers by name
    "all": "",
    "data": "(ip or ip6) and not (udp port 3784 or udp port 4784) and not tcp port 179 "
            "and not (icmp6 and ip6[40] >= 133 and ip6[40] <= 137)",   # no BFD, BGP or neighbour discovery; IS-IS is not IP
    "srv6": SRV6,
    "icmp": "icmp or icmp6",
    "bgp": "tcp port 179",
    "bfd": "udp port 3784 or udp port 4784",
    "isis": "isis",
}
_TS = re.compile(r"^\d{4}-\d\d-\d\d \d\d:\d\d:\d\d\.\d+ ")
_MARK = "=====LG-PCAP-BASE64====="


class Captures:
    def __init__(self, cfg, devices):
        self.cfg, self.devices = cfg, devices
        self.links = (cfg.get("topology") or {}).get("links") or []
        self.store, self.busy, self.groups, self.lock = {}, set(), {}, threading.Lock()

    # ---- the model ------------------------------------------------------------------------------------------------
    def ends(self, link):
        """The link's ends a capture can run on: a router the looking glass reads (a host or the collector cannot)."""
        return [{"device": link[s], "interface": link[f"{s}_port"]} for s in ("a", "b")
                if link[s] in self.devices and link[f"{s}_port"]]

    def find_link(self, device, interface):
        return next((l for l in self.links if (l["a"] == device and l["a_port"] == interface)
                     or (l["b"] == device and l["b_port"] == interface)), None)

    # ---- one capture ----------------------------------------------------------------------------------------------
    def start(self, body, wait=True):
        """Validate and start a capture; with `wait`, return when it has finished (the whole record), otherwise at once."""
        rec = self._prepare(body)
        t = threading.Thread(target=self._run, args=(rec,), daemon=True)
        t.start()
        if wait:
            t.join(rec["seconds"] + 90)
            return self.view(rec["id"])
        return self.view(rec["id"], since=0)

    def _prepare(self, body, group=None):
        device, interface = body.get("device", ""), body.get("interface", "")
        link = self.find_link(device, interface)
        if device not in self.devices or not link:
            raise ValueError(f"{device} {interface} is not an end of a link in the model")
        packets = _bound(body.get("packets", 100), 1, 1000, "packets")
        seconds = _bound(body.get("seconds", 10), 1, 60, "seconds")
        snaplen = _bound(body.get("snaplen", 256), 64, 1600, "snaplen")
        preset = body.get("preset") or ""
        if preset and preset not in PRESETS:
            raise ValueError(f"unknown preset {preset!r}: {', '.join(PRESETS)}")
        flt = PRESETS[preset] if preset else (body.get("filter") or "").strip()
        if not FILTER_OK.match(flt):
            raise ValueError("the filter may use letters, digits, spaces and . : / [ ] ( ) ! & | = < > * + - only")
        with self.lock:
            if (device, interface) in self.busy:
                raise RuntimeError(f"a capture is already running on {device} {interface}")
            self.busy.add((device, interface))
        other_a = link["a"] == device
        rec = {"id": uuid.uuid4().hex[:12], "group": group, "device": device, "interface": interface,
               "peer": link["b"] if other_a else link["a"], "peer_interface": link["b_port"] if other_a else link["a_port"],
               "peer_ip": ((link.get("b_ip") if other_a else link.get("a_ip")) or "").split("/")[0],
               "prefix": link.get("prefix"), "tenant": link.get("tenant"), "filter": flt, "preset": preset or None,
               "packets_requested": packets, "seconds": seconds, "snaplen": snaplen, "ping_peer": bool(body.get("ping_peer")),
               "status": "starting", "started": time.time(), "took": None, "count": 0, "bytes": 0, "error": None,
               "packets": []}
        with self.lock:
            self.store[rec["id"]] = {"meta": rec, "pcap": b"", "stop": False}
            for old in sorted(self.store, key=lambda k: self.store[k]["meta"]["started"])[:-KEEP]:
                if self.store[old]["meta"]["status"] not in ("starting", "running"):
                    del self.store[old]
        return rec

    def _command(self, rec):
        cid, q = rec["id"], shlex.quote
        f, pidf = f"/tmp/lgcap.{cid}", f"/tmp/lgcap.{cid}.pid"
        inner = (f"echo $$ > {pidf}; exec timeout {rec['seconds']} tcpdump -i {q(rec['interface'])} -nn -s {rec['snaplen']} "
                 f"-c {rec['packets_requested']} -U -w - {q(rec['filter']) if rec['filter'] else ''}")
        gen = ""
        if rec["ping_peer"] and rec["peer_ip"]:        # the port may sit in a tenant's VRF: ping from inside it
            gen = (f"(v=$(ip -o link show dev {q(rec['interface'])} | grep -o 'master [^ ]*' | cut -d' ' -f2); sleep 1.5; "
                   f"sudo ${{v:+ip vrf exec $v}} ping -c 5 -i 0.5 -W 1 {q(rec['peer_ip'])} >/dev/null 2>&1) & ")
        return (f"{gen}sudo sh -c {q(inner)} 2>/dev/null | tee {f} | tcpdump -nn -tttt -vv -l -r - 2>/dev/null; "
                f"echo {_MARK}; base64 -w0 {f}; echo; sudo rm -f {f} {pidf}; wait")

    def _run(self, rec):
        buf, pcap_part, in_pcap, last = [], [], False, [0.0]

        def on_line(line):
            nonlocal in_pcap
            if in_pcap:
                pcap_part.append(line); return
            if line.strip() == _MARK:
                in_pcap = True; return
            buf.append(line)
            if _TS.match(line) and time.time() - last[0] > 0.3:   # re-decode what has arrived, at most ~3 times a second
                rec["packets"] = _packets("\n".join(buf)); rec["count"] = len(rec["packets"]); last[0] = time.time()

        try:
            rec["status"] = "running"
            _ssh_stream(self.cfg, self.devices[rec["device"]], self._command(rec), rec["seconds"] + 60, on_line)
            pcap = base64.b64decode("".join(pcap_part).strip() or b"")
            rec["packets"] = _packets("\n".join(buf)); rec["count"] = len(rec["packets"])
            with self.lock:
                self.store[rec["id"]]["pcap"] = pcap
                stopped = self.store[rec["id"]]["stop"]
            rec["bytes"] = len(pcap)
            rec["status"] = "stopped" if stopped else "done"
        except Exception as e:                                        # noqa: BLE001 — SSH or the router: report it
            rec["status"], rec["error"] = "error", f"{e.__class__.__name__}: {e}"
        finally:
            rec["took"] = round(time.time() - rec["started"], 2)
            with self.lock:
                self.busy.discard((rec["device"], rec["interface"]))

    def stop(self, cid):
        with self.lock:
            s = self.store.get(cid)
            if not s:
                raise KeyError(cid)
            if s["meta"]["status"] not in ("starting", "running"):
                return s["meta"]["status"]
            s["stop"] = True
        rec = s["meta"]
        _ssh_run(self.cfg, self.devices[rec["device"]],
                 f"p=$(cat /tmp/lgcap.{rec['id']}.pid 2>/dev/null) && sudo kill -INT $p", 20)
        return "stopping"

    def view(self, cid, since=None):
        """A capture's record; with `since`, only the packets from that index on (the last one may still be growing)."""
        with self.lock:
            s = self.store.get(cid)
        if not s:
            return None
        rec = dict(s["meta"])
        if since is not None:
            rec["since"] = int(since); rec["packets"] = rec["packets"][int(since):]
        return rec

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

    # ---- a whole path ---------------------------------------------------------------------------------------------
    def start_path(self, body):
        """A capture on every link of a path at once: body.points = [{device, interface, preset?}], common bounds, and
        body.ping = {device, vrf, target} for five pings along it, sent 2 s after the captures start."""
        points = body.get("points") or []
        if not 1 <= len(points) <= 12:
            raise ValueError("a path capture takes 1 to 12 capture points")
        if len({(p.get("device"), p.get("interface")) for p in points}) != len(points):
            raise ValueError("one capture per router port: the same port twice")
        ping = body.get("ping") or None
        if ping:
            if ping.get("device") not in self.devices:
                raise ValueError(f"cannot ping from {ping.get('device')!r}: not a router the looking glass reads")
            if ping.get("vrf") and not NAME_OK.match(ping["vrf"]):
                raise ValueError("a VRF is a name: letters, digits, - and _")
            try:
                ipaddress.ip_address(ping.get("target", ""))
            except ValueError:
                raise ValueError(f"the ping target {ping.get('target')!r} is not an address")
        common = {k: body[k] for k in ("packets", "seconds", "snaplen") if k in body}
        recs = []
        try:
            for p in points:
                recs.append(self._prepare({**common, "device": p.get("device"), "interface": p.get("interface"),
                                           "preset": p.get("preset") or body.get("preset") or "data",
                                           "filter": p.get("filter")}, group=True))
        except Exception:
            for r in recs:                                        # release what was taken before the bad point
                with self.lock:
                    self.busy.discard((r["device"], r["interface"])); self.store.pop(r["id"], None)
            raise
        gid = uuid.uuid4().hex[:12]
        for r in recs:
            r["group"] = gid
            threading.Thread(target=self._run, args=(r,), daemon=True).start()
        g = {"id": gid, "started": time.time(), "captures": [r["id"] for r in recs], "ping": ping, "ping_result": None}
        with self.lock:
            self.groups[gid] = g
        if ping:
            threading.Thread(target=self._ping, args=(g,), daemon=True).start()
        return g

    def _ping(self, g):
        p, q = g["ping"], shlex.quote
        vrf = f"ip vrf exec {q(p['vrf'])} " if p.get("vrf") else ""
        try:
            out = _ssh_run(self.cfg, self.devices[p["device"]],
                           f"sleep 2; sudo {vrf}ping -c 5 -i 0.5 -W 1 {q(p['target'])} 2>&1 | tail -3", 40)
            g["ping_result"] = out.strip()
        except Exception as e:                                        # noqa: BLE001
            g["ping_result"] = f"{e.__class__.__name__}: {e}"

    def group(self, gid):
        with self.lock:
            return self.groups.get(gid)


def _ssh_client(cfg, dev):
    import paramiko
    ssh = cfg["ssh"]
    c = paramiko.SSHClient(); c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    c.connect(dev["mgmt_ip"], username=ssh["username"], password=ssh["password"], timeout=20, look_for_keys=False, allow_agent=False)
    return c


def _ssh_run(cfg, dev, cmd, timeout):
    c = _ssh_client(cfg, dev)
    try:
        _, out, err = c.exec_command(cmd, timeout=timeout)
        text = out.read().decode(errors="replace")
        out.channel.recv_exit_status()
        return text
    finally:
        c.close()


def _ssh_stream(cfg, dev, cmd, timeout, on_line):
    """Run `cmd`, handing each line of its output to on_line as it arrives."""
    c = _ssh_client(cfg, dev)
    try:
        ch = c.get_transport().open_session(); ch.settimeout(timeout); ch.exec_command(cmd)
        pending, end = b"", time.time() + timeout
        while time.time() < end:
            data = ch.recv(65536)
            if not data:
                break
            pending += data
            *lines, pending = pending.split(b"\n")
            for l in lines:
                on_line(l.decode(errors="replace"))
        if pending:
            on_line(pending.decode(errors="replace"))
        ch.recv_exit_status()
    finally:
        c.close()


def _bound(v, lo, hi, name):
    try:
        v = int(v)
    except (TypeError, ValueError):
        raise ValueError(f"{name} must be a whole number")
    if not lo <= v <= hi:
        raise ValueError(f"{name} must be between {lo} and {hi}")
    return v


def _packets(text):
    """tcpdump -tttt -vv output → [{no, time, ts, summary, proto, src, dst, info, detail}]: a packet starts with its time."""
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
        try:
            p["ts"] = time.mktime(time.strptime(p["time"][:19], "%Y-%m-%d %H:%M:%S")) + float("0" + p["time"][19:26])
        except ValueError:
            p["ts"] = None
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
