#!/usr/bin/env python3
"""Presenter mode: the lab explained live, act by act. Each act prints what to say, then runs real commands on the lab one
at a time (Enter = next command, `s` = skip act, `q` = quit) and shows the output. Acts:
  1 underlay   IS-IS, locators, the SIDs in the kernel            (~4 min)
  2 vpn        BGP VPNv4 with SIDs, the encapsulating VRF route   (~4 min)
  3 walk       packet walk: ping + tcpdump on p2, traceroute       (~4 min)
  4 steer      uSID steering: the address rewritten hop by hop     (~5 min)
  5 failover   BFD cut of p2-pe3 with a live ping, then repair     (~4 min)
  6 ops        Nautobot, portal, Grafana, logs, flows              (~3 min)
   demo_live.py [act ...] [--auto SECONDS]      e.g. demo_live.py 1 2 3 | demo_live.py --auto 2 (no pauses; for recording)"""
import json, subprocess, sys, time
from pathlib import Path
import paramiko
from netmiko import ConnectHandler

LAB = Path(__file__).resolve().parents[1]
INV = json.loads(subprocess.run([str(LAB / "lab.sh"), "inventory"], capture_output=True, text=True, check=True).stdout)
N = {n["name"]: n for n in INV["nodes"]}; H = {n["name"]: n for n in INV["nodes"] if n["role"] == "host"}
AUTO = float(sys.argv[sys.argv.index("--auto") + 1]) if "--auto" in sys.argv else None
ACTS = [a for a in sys.argv[1:] if a.isdigit()] or ["1", "2", "3", "4", "5", "6"]
B, D, G, Y, R, C = "\033[1m", "\033[2m", "\033[32m", "\033[33m", "\033[0m", "\033[36m"
_ssh = {}


def vyos(node, cmd):
    if node not in _ssh: _ssh[node] = ConnectHandler(device_type="vyos", host=N[node]["mgmt_ip"], username="vyos", password="vyos", global_delay_factor=2)
    return _ssh[node].send_command(cmd, read_timeout=90)


def shell(node, cmd):
    c = paramiko.SSHClient(); c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    c.connect(N[node]["mgmt_ip"], username="vyos" if N[node]["role"] != "host" else "lab", password="vyos" if N[node]["role"] != "host" else "lab", timeout=20, look_for_keys=False, allow_agent=False)
    _, o, e = c.exec_command(cmd, timeout=120); out = o.read().decode() + e.read().decode(); c.close(); return out


def local(cmd): return subprocess.run(cmd, shell=True, capture_output=True, text=True, cwd=LAB).stdout


def say(text): print(f"\n{Y}{text}{R}")
def pause(prompt="⏎ next"):
    if AUTO: time.sleep(AUTO); return True
    a = input(f"{D}{prompt}  (s = skip act, q = quit){R} ").strip().lower()
    if a == "q": sys.exit(0)
    return a != "s"


def step(title, where, cmd, run, note=None):
    """Announce a command, run it on Enter, print the output (and a note on what to point at)."""
    print(f"\n{B}{C}{where}$ {cmd}{R}")
    if not pause(): return False
    out = run(); print(out.rstrip()[:3500])
    if note: print(f"{G}→ {note}{R}")
    return True


def act(num, title, blurb, steps):
    print(f"\n{'=' * 78}\n{B}ACT {num} — {title}{R}\n{'=' * 78}"); say(blurb)
    if not pause("⏎ start the act"): return
    for s in steps:
        if step(*s) is False: return


def sid(pe, tenant): return shell(pe, f"ip -6 route show | grep 'End.DT4 vrftable {tenant}' | cut -d' ' -f1").strip().splitlines()[0]


def capture(src, dst):
    c = paramiko.SSHClient(); c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    c.connect(N["p2"]["mgmt_ip"], username="vyos", password="vyos", timeout=20, look_for_keys=False, allow_agent=False)
    _, o, _ = c.exec_command("sudo timeout 12 tcpdump -ni eth5 -c 3 -vv 'ip6 and dst net fd00:c:3::/48' 2>/dev/null"); time.sleep(2)
    shell(src["name"], f"ping -c 4 -i 0.5 {lan(dst)} >/dev/null"); out = o.read().decode(); c.close(); return out


h1 = {dc: next(h for h in H.values() if h["dc"] == dc and any(p["tenant"] == "tenant-a" for p in h["ports"] if p["peer"])) for dc in ("dc1", "dc3")}
lan = lambda h: h["ports"][0]["ip"].split("/")[0]

if "1" in ACTS:
    act(1, "The underlay: IS-IS carries the locators; the SIDs live in the kernel",
        "Whiteboard: 3 P routers, 4 PEs. IS-IS L2, IPv6 only. Every node owns a /48 locator under fd00:c::/32 (uSID: block 32 bits, node 16, function 16). A SID is an instruction: End = forward, End.X = out this link, End.DT4 = decapsulate into this VRF.",
        [("isis", "pe1", "show isis neighbor", lambda: vyos("pe1", "show isis neighbor"), "two adjacencies: pe1 is dual-homed to p1 and p2"),
         ("locators", "pe1", "show ipv6 route isis | grep fd00:c:", lambda: vyos("pe1", "show ipv6 route isis | grep fd00:c:"), "every other locator is an ordinary IS-IS route; metric 10 = one hop (p1, p2), 20 = two hops"),
         ("sids", "p2", "ip -6 route show | grep seg6local", lambda: shell("p2", "ip -6 route show | grep seg6local"), "the ENTIRE SRv6 data plane of a P router: uN (End, shift-and-forward) for its /48, one uA (End.X) per link — no VRF, no BGP"),
         ("sids", "pe1", "ip -6 route show | grep seg6local", lambda: shell("pe1", "ip -6 route show | grep seg6local"), "a PE adds one End.DT4 per tenant VRF — the address remote PEs will send tenant traffic to")])

if "2" in ACTS:
    act(2, "The overlay: the BGP VPN you already know, with a SID where the label was",
        "Whiteboard: PE <-> route reflectors p1 and p3 over IPv6 loopbacks (VPNv4 with extended next hop). RD per PE per VRF, RT per tenant. The Prefix-SID attribute carries the End.DT4 SID; the function bits ride in the label field (transposition).",
        [("vpnv4", "pe1", "show bgp ipv4 vpn summary", lambda: vyos("pe1", "show bgp ipv4 vpn summary"), "both reflectors Established; 12 prefixes = 4 sites x 2 tenants + the ACs"),
         ("prefix", "pe1", f"show bgp ipv4 vpn {lan(h1['dc3']).rsplit('.', 1)[0]}.0/24", lambda: vyos("pe1", f"show bgp ipv4 vpn {lan(h1['dc3']).rsplit('.', 1)[0]}.0/24"), "Remote SID fd00:c:3:: + sid structure [32 16 16 0 16 48]; Remote label 917504 = 0xE000 << 4 -> reassembled SID fd00:c:3:e000::; two copies, one per reflector"),
         ("vrf route", "pe1", "ip route show vrf tenant-a", lambda: shell("pe1", "ip route show vrf tenant-a"), "encap seg6 ... segs 1 [ fd00:c:3:e000:: ] — no label table: 'wrap in IPv6 to this address'; the dc2 LAN has two next hops = ECMP from the IGP")])

if "3" in ACTS:
    src, dst = h1["dc1"], h1["dc3"]
    act(3, "Packet walk: dc1-h1 -> dc3-h1, captured on the P router in the middle",
        "Whiteboard: host -> CE (IPv4) -> PE encapsulates (outer IPv6 src = loopback, dst = pe3's End.DT4 SID) -> p2 just routes IPv6 -> pe3 decapsulates into VRF tenant-a -> CE -> host.",
        [("ping", src["name"], f"ping -c 3 {lan(dst)}", lambda: shell(src["name"], f"ping -c 3 {lan(dst)}"), "~2 ms across the core"),
         ("capture", "p2", f"tcpdump -ni eth5 -c 3 -vv 'ip6 and dst net fd00:c:3::/48'  (while the host pings)", lambda: capture(src, dst), "IP6 fd00:a::1 > fd00:c:3:e000:: with RT6 type 4 (the SRH), segleft 0, and the inner ICMP 172.20.1.2 > 172.20.3.2; hop limit 62 = two hops"),
         ("traceroute", src["name"], f"traceroute -n -w 1 -q 1 {lan(dst)}", lambda: shell(src["name"], f"traceroute -n -w 1 -q 1 {lan(dst)}"), "the tenant sees one opaque hop for the whole core: the inner TTL is not touched inside"),
         ("isolation", src["name"], f"ping -c 2 -W 2 {lan(next(h for h in H.values() if h['dc'] == 'dc1' and h['name'] != src['name']))}", lambda: shell(src["name"], f"ping -c 2 -W 2 {lan(next(h for h in H.values() if h['dc'] == 'dc1' and h['name'] != src['name']))}; true"), "tenant-b's host at the SAME site: 100 % loss — isolation is the absence of a route, not a firewall")])


if "4" in ACTS:
    hb = {dc: next(h for h in H.values() if h["dc"] == dc and any(p["tenant"] == "tenant-b" for p in h["ports"] if p["peer"])) for dc in ("dc1", "dc3")}
    prefix = hb["dc3"]["ports"][0]["prefix"]
    def cap(node, iface):
        c = paramiko.SSHClient(); c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        c.connect(N[node]["mgmt_ip"], username="vyos", password="vyos", timeout=20, look_for_keys=False, allow_agent=False)
        _, o, _ = c.exec_command(f"sudo timeout 12 tcpdump -ni {iface} -c 2 -vv 'ip6 and dst net fd00:c::/32' 2>/dev/null"); time.sleep(2)
        shell(hb["dc1"]["name"], f"ping -c 3 -i 0.5 {lan(hb['dc3'])} >/dev/null"); out = o.read().decode(); c.close(); return out
    act(4, "uSID: an explicit path is one address, rewritten hop by hop",
        "Whiteboard: fd00:c : 11 : 13 : 3 : e001 :: = block, p1, p3, pe3, function. Each node whose ID is first after the block shifts the address left 16 bits (uN, NEXT-C-SID) and forwards. No SRH grows, no state in the core.",
        [("steer", "lab host", f"tools/steer.py add pe1 tenant-b {prefix} p1 p3", lambda: local(f"tests/.venv/bin/python tools/steer.py add pe1 tenant-b {prefix} p1 p3"), "one segment: the carrier fd00:c:11:13:3:e001::, out eth1 towards p1 (the shortest path was p2)"),
         ("capture", "p1", "tcpdump -ni eth2 -vv 'ip6 and dst net fd00:c::/32'  (link to p3)", lambda: cap("p1", "eth2"), "destination is now fd00:c:13:3:e001:: — p1 consumed its own uSID and shifted; the SRH still shows the original carrier"),
         ("capture", "p3", "tcpdump -ni eth3 -vv 'ip6 and dst net fd00:c::/32'  (link to pe3)", lambda: cap("p3", "eth3"), "fd00:c:3:e001:: — down to pe3's uDT4 SID, exactly what an unsteered packet carries; hop limit 61 = three routers"),
         ("uncompressed", "lab host", f"tools/steer.py add pe1 tenant-b {prefix} p1 p3 --uncompressed", lambda: local(f"tests/.venv/bin/python tools/steer.py add pe1 tenant-b {prefix} p1 p3 --uncompressed"), "the same path the classic way: three full SIDs in an SRH (56 bytes) — what uSID saves"),
         ("clean up", "lab host", f"tools/steer.py del pe1 tenant-b {prefix}", lambda: local(f"tests/.venv/bin/python tools/steer.py del pe1 tenant-b {prefix}"), "back on the BGP route")])

if "5" in ACTS:
    src, dst = h1["dc1"], h1["dc3"]
    def cut():
        c = paramiko.SSHClient(); c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        c.connect(H[src["name"]]["mgmt_ip"], username="lab", password="lab", timeout=20, look_for_keys=False, allow_agent=False)
        _, o, _ = c.exec_command(f"ping -c 150 -i 0.2 -W 1 {lan(dst)}"); time.sleep(3)
        r = ConnectHandler(device_type="vyos", host=N["p2"]["mgmt_ip"], username="vyos", password="vyos", global_delay_factor=2); r.config_mode()
        r.send_config_set(["set firewall ipv6 input filter rule 10 inbound-interface name eth5", "set firewall ipv6 input filter rule 10 action drop", "set firewall ipv6 output filter rule 10 outbound-interface name eth5", "set firewall ipv6 output filter rule 10 action drop",
                           "set firewall ipv6 forward filter rule 10 inbound-interface name eth5", "set firewall ipv6 forward filter rule 10 action drop", "set firewall ipv6 forward filter rule 11 outbound-interface name eth5", "set firewall ipv6 forward filter rule 11 action drop"], exit_config_mode=False)
        r.commit(); t_cut = time.time(); time.sleep(6)
        nb = r.send_command("run show isis neighbor", read_timeout=60); rt = shell("pe3", f"ip route show vrf tenant-a {lan(src).rsplit('.', 1)[0]}.0/24")
        r.send_config_set(["delete firewall"], exit_config_mode=False); r.commit(); r.exit_config_mode(); r.disconnect(); t_fix = time.time()
        out = o.read().decode(); c.close()
        return (f"--- cut at +0 s (firewall drops IS-IS and BFD on p2 eth5, link stays up: a SILENT failure), repaired at +{t_fix - t_cut:.0f} s\n"
                f"--- p2 IS-IS neighbours after the cut:\n{nb}\n--- pe3's route to dc1's LAN after the cut:\n{rt}\n--- the ping that ran through it:\n" + "\n".join(out.splitlines()[-3:]))
    act(5, "Failover: a silent link failure, BFD, and how many packets the tenant lost",
        "Whiteboard: BFD every 300 ms x3 on every core adjacency. A firewall rule on p2 silently eats IS-IS and BFD on the p2-pe3 link (the interface stays up: the nasty case). pe3 must move every tenant route to p3 within a second while a host pings across at 5/s.",
        [("cut + ping", "p2 / dc1-h1", "set firewall ... eth5 drop  (with a 0.2 s ping running dc1-h1 -> dc3-h1)", cut, "IS-IS on p2 shows pe3 gone; pe3's route now goes via eth2 (p3); the ping lost a handful of packets = ~1 s of outage, the BFD detection time"),
         ("grafana", "browser", "Grafana overview: the cut and repair are an annotation region; VictoriaLogs has isisd's '%ADJCHANGE ... bfd session went down'", lambda: local("curl -s 'http://10.0.0.10:9428/select/logsql/query' --data-urlencode 'query=_time:3m app_name:isisd \"ADJCHANGE\" | uniq by (hostname, _msg) | fields hostname, _msg' | head -6"), "the routers' own words about what just happened")])

if "6" in ACTS:
    act(6, "Operating it: source of truth, provisioning, tests, telemetry",
        "Whiteboard: Nautobot models everything -> renderer -> configs (the same renderer reads lab.conf; a test proves they agree). The portal adds a tenant as a pipeline. 64 Robot cases. Metrics (pull + push), syslog, flows, alerts, annotations.",
        [("nautobot", "lab host", "./lab.sh nautobot render --check", lambda: local("./lab.sh nautobot render --check"), "Nautobot's rendering == lab.conf's for every node"),
         ("portal", "lab host", "curl portal /api/state (tenant health)", lambda: local("curl -s localhost:8091/api/state | python3 -c \"import sys,json; d=json.load(sys.stdin); [print(t['name'], t.get('health'), [(s['dc'], s['live']['bgp']) for s in t['sites']]) for t in d['tenants']]\""), "live per-site eBGP state and host reachability"),
         ("telemetry", "NMS", "VictoriaMetrics: BGP sessions Established (frr-exporter) / flows: SRv6 paths seen on p2 (sFlow)", lambda: local("curl -s 'http://10.0.0.10:8428/api/v1/query' --data-urlencode 'query=count(frr_bgp_peer_state{lab=\"srv6-core\"}==1)' | python3 -c \"import sys,json; print('BGP sessions up:', json.load(sys.stdin)['data']['result'][0]['value'][1])\"; curl -s 'http://10.0.0.10:9428/select/logsql/query' --data-urlencode 'query=_time:30m sampler_address:10.3.0.22 proto:\"IPv6-Route\" | stats by (src_addr, dst_addr) count() as samples | sort by (samples desc) | limit 5'"), "the packet walk as flows: source PE loopback -> destination SID, sampled on p2"),
         ("tests", "lab host", "./lab.sh test  (64 cases; results committed with configs and routing tables)", lambda: local("ls results | tail -3; grep -c '<test ' results/latest/output.xml 2>/dev/null || true"), "every claim in this session is a test case")])

for c in _ssh.values():
    try: c.disconnect()
    except Exception: pass
print(f"\n{G}end of the session.{R}")
