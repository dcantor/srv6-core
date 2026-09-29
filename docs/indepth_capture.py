#!/usr/bin/env python3
"""Capture the extra outputs quoted in docs/srv6-in-depth.md (the ones docs/walkthrough_capture.py does not take) from
the live lab into docs/walkthrough/*.txt. Read-only: show commands, a one-packet hex capture, pings and traceroutes.

    python3 docs/indepth_capture.py"""
import json, subprocess, sys, time
from pathlib import Path

LAB = Path(__file__).resolve().parents[1]; OUT = LAB / "docs" / "walkthrough"; OUT.mkdir(exist_ok=True)
sys.path.insert(0, str(LAB / "tests" / "resources")); import LabLib   # noqa: E402
L = LabLib.LabLib()
INV = json.loads(subprocess.run([str(LAB / "lab.sh"), "inventory"], capture_output=True, text=True, check=True).stdout)
HOST = {n["name"]: n for n in INV["nodes"] if n["role"] == "host"}
MGMT = {n["name"]: n["mgmt_ip"] for n in INV["nodes"]}
lan = lambda h: HOST[h]["ports"][0]["ip"].split("/")[0]


def save(name, text):
    (OUT / f"{name}.txt").write_text(text.rstrip() + "\n"); print(f"{name:34s} {len(text.splitlines()):4d} lines")


op = lambda node, cmd: L.run_vyos_command(MGMT[node], cmd)
sh = lambda node, cmd: L.vyos_shell(MGMT[node], cmd)
hc = lambda host, cmd: L.host_command(MGMT[host], cmd)

# IS-IS: pe1's own LSP, with the SRv6 TLVs
save("pe1-isis-lsp", sh("pe1", "vtysh -c 'show isis database detail pe1.00-00'"))
# BGP: the VPN route and the SID structure
save("pe1-bgp-vpn-prefix-json", sh("pe1", "vtysh -c 'show bgp ipv4 vpn 172.20.3.0/24 json' | python3 -c \"import json,sys; d=json.load(sys.stdin); "
                                        "[print(json.dumps({k: p.get(k) for k in ('remoteSid','remoteSidStructure','remoteLabel','nexthops')}, indent=1)) "
                                        "for rd in d.get('172.20.3.0/24', d).values() if isinstance(rd, dict) for p in rd.get('paths', [])[:1]]\" 2>&1 | head -40"))
# the kernel's SRv6 settings on a PE
save("pe1-seg6-sysctl", sh("pe1", "for i in all eth1 eth2 eth3; do echo \"net.ipv6.conf.$i.seg6_enabled = $(cat /proc/sys/net/ipv6/conf/$i/seg6_enabled)\"; done; "
                               "sudo ip sr tunsrc show"))
save("pe1-vrf-leak", sh("pe1", "ip -6 route show vrf tenant-a | grep -E 'fd00:c:' | head -8"))
# one SRv6 packet in hex, on p2 towards pe3
h = L.start_background(MGMT["p2"], "sudo timeout 10 tcpdump -ni eth5 -c 1 -xx -v 'ip6 and dst net fd00:c:3::/48 and ip6 proto 43' 2>/dev/null")
time.sleep(2); hc("dc1-h1", f"ping -c 2 -i 0.5 -s 16 {lan('dc3-h1')} >/dev/null"); save("p2-tcpdump-hex", L.finish_background(h))
# OAM: a locator answers ping; traceroute through the core to it
save("pe1-ping-locator", sh("pe1", "ping -6 -c 3 -i 0.3 fd00:c:3:: 2>&1 | tail -4"))
save("pe1-traceroute-locator", sh("pe1", "traceroute -6 -n -q 1 -w 1 fd00:c:3:: 2>&1 | head -6"))
save("pe1-traceroute-steered-free", sh("pe1", "traceroute -6 -n -q 1 -w 1 fd00:a::3 2>&1 | head -6"))
# MSDs as IS-IS advertises them
save("pe1-isis-srv6-node", op("pe1", "show isis segment-routing srv6 node"))
