#!/usr/bin/env python3
"""Throughput between two tenant hosts across the SRv6 core (iperf3 on the Alpine hosts, over their tenant LAN addresses).
   iperf.py <src-host> <dst-host> [-t seconds] [-P streams] [-u -b RATE] [--json]     e.g. iperf.py dc1-h1 dc3-h1 -t 5
   iperf.py --scenarios                    baseline, steered (uSID carrier), steered (uncompressed) for dc1-h2 -> dc3-h2
Prints Mbit/s (TCP: sender/receiver, retransmits; UDP: rate, loss, jitter). The server is started on the destination for the
run and stopped afterwards."""
import argparse, json, subprocess, sys, time
from pathlib import Path
import paramiko

LAB = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(LAB / "tools"))
p = argparse.ArgumentParser(); p.add_argument("src", nargs="?"); p.add_argument("dst", nargs="?"); p.add_argument("-t", type=int, default=5); p.add_argument("-P", type=int, default=1)
p.add_argument("-u", action="store_true"); p.add_argument("-b", default="50M"); p.add_argument("--json", action="store_true"); p.add_argument("--scenarios", action="store_true")
a = p.parse_args()
inv = json.loads(subprocess.run([str(LAB / "lab.sh"), "inventory"], capture_output=True, text=True, check=True).stdout)
HOSTS = {n["name"]: n for n in inv["nodes"] if n["role"] == "host"}


def ssh(host):
    c = paramiko.SSHClient(); c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    c.connect(HOSTS[host]["mgmt_ip"], username="lab", password="lab", timeout=15, look_for_keys=False, allow_agent=False); return c


def run(src, dst, seconds=5, streams=1, udp=False, rate="50M"):
    lan = HOSTS[dst]["ports"][0]["ip"].split("/")[0]
    s = ssh(dst); s.exec_command("pkill iperf3; (iperf3 -s -1 -D >/dev/null 2>&1 &)"); time.sleep(1.5)
    c = ssh(src); cmd = f"iperf3 -c {lan} -t {seconds} -P {streams} -J" + (f" -u -b {rate}" if udp else "")
    _, out, err = c.exec_command(cmd, timeout=seconds + 30); txt = out.read().decode(); e = err.read().decode(); c.close()
    s.exec_command("pkill iperf3"); s.close()
    try: r = json.loads(txt)
    except ValueError: sys.exit(f"iperf3 failed: {e or txt[-300:]}")
    if "error" in r: sys.exit(f"iperf3: {r['error']}")
    end = r["end"]
    if udp:
        u = end["sum"]; return {"src": src, "dst": dst, "to": lan, "udp": True, "mbps": round(u["bits_per_second"] / 1e6, 1), "loss_pct": round(u["lost_percent"], 2), "jitter_ms": round(u["jitter_ms"], 3), "seconds": seconds}
    return {"src": src, "dst": dst, "to": lan, "udp": False, "mbps_sent": round(end["sum_sent"]["bits_per_second"] / 1e6, 1), "mbps_received": round(end["sum_received"]["bits_per_second"] / 1e6, 1),
            "retransmits": end["sum_sent"].get("retransmits"), "streams": streams, "seconds": seconds}


def fmt(r):
    if r["udp"]: return f"{r['src']} -> {r['dst']} ({r['to']}): UDP {r['mbps']} Mbit/s, loss {r['loss_pct']} %, jitter {r['jitter_ms']} ms ({r['seconds']} s)"
    return f"{r['src']} -> {r['dst']} ({r['to']}): TCP {r['mbps_received']} Mbit/s received ({r['mbps_sent']} sent, {r['retransmits']} retransmits, {r['streams']} stream(s), {r['seconds']} s)"


if a.scenarios:
    steer = lambda *args: subprocess.run([sys.executable, str(LAB / "tools" / "steer.py"), *args], capture_output=True, text=True)
    src, dst, tenant, prefix = "dc1-h2", "dc3-h2", "tenant-b", HOSTS["dc3-h2"]["ports"][0]["prefix"]
    results = []
    try:
        results.append(("shortest path (pe1 -> p2 -> pe3)", run(src, dst, a.t, a.P)))
        steer("add", "pe1", tenant, prefix, "p1", "p3"); time.sleep(2); results.append(("steered pe1 -> p1 -> p3 -> pe3, one uSID carrier segment", run(src, dst, a.t, a.P)))
        steer("add", "pe1", tenant, prefix, "p1", "p3", "--uncompressed"); time.sleep(2); results.append(("steered, uncompressed three-segment SRH", run(src, dst, a.t, a.P)))
    finally:
        steer("del", "pe1", tenant, prefix)
    if a.json: print(json.dumps(results, indent=1))
    else:
        for title, r in results: print(f"{title:58s} {fmt(r)}")
elif a.src and a.dst:
    r = run(a.src, a.dst, a.t, a.P, a.u, a.b); print(json.dumps(r) if a.json else fmt(r))
else:
    sys.exit(__doc__)
