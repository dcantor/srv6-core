#!/usr/bin/env python3
"""Capture the command outputs quoted in docs/srv6-walkthrough.md from the live lab into docs/walkthrough/*.txt, so
the document shows real output and can be refreshed after a change (python3 docs/walkthrough_capture.py)."""
import json, subprocess, sys, time
from pathlib import Path

LAB = Path(__file__).resolve().parents[1]; OUT = LAB / "docs" / "walkthrough"; OUT.mkdir(exist_ok=True)
sys.path.insert(0, str(LAB / "tests" / "resources")); import LabLib   # noqa: E402
L = LabLib.LabLib()
INV = json.loads(subprocess.run([str(LAB / "lab.sh"), "inventory"], capture_output=True, text=True, check=True).stdout)
HOST = {n["name"]: n for n in INV["nodes"] if n["role"] == "host"}
MGMT = {n["name"]: n["mgmt_ip"] for n in INV["nodes"]}   # LabLib keywords take addresses
lan = lambda h: HOST[h]["ports"][0]["ip"].split("/")[0]


def save(name, text):
    (OUT / f"{name}.txt").write_text(text.rstrip() + "\n"); print(f"{name:34s} {len(text.splitlines()):4d} lines")


op = lambda node, cmd: L.run_vyos_command(MGMT[node], cmd)
sh = lambda node, cmd: L.vyos_shell(MGMT[node], cmd)
hc = lambda host, cmd: L.host_command(MGMT[host], cmd)
bg = lambda node, cmd: L.start_background(MGMT[node], cmd)

# underlay
save("pe1-isis-neighbor", op("pe1", "show isis neighbor"))
save("p2-isis-neighbor", op("p2", "show isis neighbor"))
save("pe1-locator", op("pe1", "show segment-routing srv6 locator"))
save("pe1-locator-detail", op("pe1", "show segment-routing srv6 locator main detail"))
save("p1-isis-srv6-node", op("p1", "show isis segment-routing srv6 node"))
save("pe1-ipv6-route-locators", sh("pe1", "vtysh -c 'show ipv6 route isis' | grep -E 'fd00:c:' | head -12"))
save("pe1-seg6local", sh("pe1", "ip -6 route show | grep seg6local"))
save("p2-seg6local", sh("p2", "ip -6 route show | grep seg6local"))
save("pe1-dum0", sh("pe1", "ip -6 addr show dev dum0 | grep inet6"))
save("pe1-bfd", op("pe1", "show bfd peers brief"))
# vpn
save("p1-bgp-vpn-summary", op("p1", "show bgp ipv4 vpn summary"))
save("pe1-bgp-vpn-summary", op("pe1", "show bgp ipv4 vpn summary"))
save("pe1-bgp-vrf-summary", op("pe1", "show ip bgp vrf tenant-a summary"))
save("p1-bgp-vpn", op("p1", "show bgp ipv4 vpn"))
save("pe1-bgp-vpn-prefix", op("pe1", f"show bgp ipv4 vpn {HOST['dc3-h1']['ports'][0]['prefix']}"))
save("pe1-bgp-vrf", op("pe1", "show ip bgp vrf tenant-a"))
save("pe1-route-vrf", sh("pe1", "ip route show vrf tenant-a"))
save("pe1-route-vrf-b", sh("pe1", "ip route show vrf tenant-b"))
save("pe1-vtysh-vrf-route", op("pe1", f"show ip route vrf tenant-a {HOST['dc3-h1']['ports'][0]['prefix']}"))
save("pe3-seg6local", sh("pe3", "ip -6 route show | grep 'End.DT4'"))
# dual-stack: the IPv6 side of the same VPN
save("pe1-bgp-vrf6-summary", op("pe1", "show bgp vrf tenant-a ipv6 summary"))
save("pe1-bgp-vpn6-prefix", op("pe1", f"show bgp ipv6 vpn {HOST['dc3-h1']['ports'][0]['prefix6']}"))
save("pe1-route6-vrf", sh("pe1", "ip -6 route show vrf tenant-a | grep -v 'fe80\\|ff00\\|anycast\\|::1 dev'"))
save("dc1-h1-ping6-dc3-h1", hc("dc1-h1", f"ping -6 -c 3 {HOST['dc3-h1']['ports'][0]['ip6'].split('/')[0]}"))
save("ce1-bgp", op("ce1", "show ip bgp vrf tenant-a"))
save("ce1-route", op("ce1", "show ip route vrf tenant-a bgp"))
save("p2-route-vrf", sh("p2", "ip route show vrf tenant-a 2>&1 | head -2; ip vrf show"))
save("p2-routes-v4", sh("p2", "ip route show | head -5"))
# hosts
save("dc1-h1-ip", hc("dc1-h1", "ip -br addr; ip route"))
save("dc1-h1-ping-dc3-h1", hc("dc1-h1", f"ping -c 3 {lan('dc3-h1')}"))
save("dc1-h1-ping-dc1-h2", hc("dc1-h1", f"ping -c 2 -W 2 {lan('dc1-h2')}; true"))
save("dc1-h1-traceroute", hc("dc1-h1", f"traceroute -n -w 1 -q 1 {lan('dc3-h1')} 2>&1 | head -8"))
# packet walk: capture on p2 while dc1-h1 pings dc3-h1
h = bg("p2", "sudo timeout 8 tcpdump -ni eth5 -c 4 -vv 'ip6 and dst net fd00:c:3::/48' 2>/dev/null")
time.sleep(2); hc("dc1-h1", f"ping -c 4 -i 0.5 {lan('dc3-h1')} >/dev/null"); save("p2-tcpdump-srv6", L.finish_background(h))
h = bg("p2", "sudo timeout 10 tcpdump -ni eth5 -c 2 -vv 'ip6 and dst net fd00:c:3::/48 and ip6 proto 43' 2>/dev/null")
time.sleep(2); hc("dc1-h1", f"ping -6 -c 4 -i 0.5 {HOST['dc3-h1']['ports'][0]['ip6'].split('/')[0]} >/dev/null"); save("p2-tcpdump-srv6-v6", L.finish_background(h))
h = bg("pe1", "sudo timeout 8 tcpdump -ni eth3 -c 2 'icmp' 2>/dev/null")
time.sleep(2); hc("dc1-h1", f"ping -c 2 -i 0.5 {lan('dc3-h1')} >/dev/null"); save("pe1-tcpdump-inner", L.finish_background(h))
# steering
prefix = HOST["dc3-h2"]["ports"][0]["prefix"]
save("steer-sid", L.steer("sid", "pe3", "tenant-b"))
save("steer-add", L.steer("add", "pe1", "tenant-b", prefix, "p1", "p3")); time.sleep(2)
save("steer-show", L.steer("show"))
save("pe1-route-steered", sh("pe1", f"ip route show vrf tenant-b {prefix}"))
h = bg("p1", "sudo timeout 8 tcpdump -ni eth2 -c 2 -vv 'ip6 and ip6[6]==43 or (ip6 and dst net fd00:c::/32)' 2>/dev/null")
time.sleep(2); hc("dc1-h2", f"ping -c 3 -i 0.5 {lan('dc3-h2')} >/dev/null"); save("p1-tcpdump-steered", L.finish_background(h))
h = bg("p3", "sudo timeout 8 tcpdump -ni eth3 -c 2 -vv 'ip6 and dst net fd00:c::/32' 2>/dev/null")
time.sleep(2); hc("dc1-h2", f"ping -c 3 -i 0.5 {lan('dc3-h2')} >/dev/null"); save("p3-tcpdump-steered", L.finish_background(h))
save("dc1-h2-traceroute-steered", hc("dc1-h2", f"traceroute -n -w 1 -q 1 {lan('dc3-h2')} 2>&1 | head -8"))
save("steer-add-uncompressed", L.steer("add", "pe1", "tenant-b", prefix, "p1", "p3", "--uncompressed")); time.sleep(2)
save("pe1-route-steered-uncompressed", sh("pe1", f"ip route show vrf tenant-b {prefix}"))
save("steer-del", L.steer("del", "pe1", "tenant-b", prefix))
# monitoring
save("pe1-frr-exporter", subprocess.run(["bash", "-c", "curl -s http://10.3.0.11:9342/metrics | grep -E '^frr_(bgp_peer_state|bfd_peer_state)' | head -6"], capture_output=True, text=True).stdout)
save("portal-metrics", subprocess.run(["bash", "-c", "curl -s http://127.0.0.1:8091/metrics | grep -E '^lab_(tenant_health|isis_adjacencies_up|tenant_site_bgp_up.*dc1)' | head -8"], capture_output=True, text=True).stdout)
L.close_all_connections(); print("done")
