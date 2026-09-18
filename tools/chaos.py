#!/usr/bin/env python3
"""Fault injection for troubleshooting drills (and for measuring an AI operator). One fault at a time; the injector
records what it did so `reveal` and `repair` are exact. Faults are real misconfigurations, not link cuts alone:
  rt-import        a PE's tenant VRF imports the wrong route target       -> that site loses the tenant's remote routes
  locator-leak     a PE's VRF loses the locator leaks towards one PE      -> traffic to that PE from the VRF vanishes (Ip6OutNoRoutes)
  ce-shutdown      a CE administratively shuts its session to the PE      -> one site down, BGP alert
  silent-cut       a P router silently drops IS-IS/BFD on one link        -> reroute, BFD alert, adjacency count off
  sid-export       a PE's VRF stops exporting SIDs                          -> remote PEs cannot reach that site (routes without SID)
  blackhole        a static blackhole for one remote LAN in a PE VRF      -> exactly one host pair fails, everything else fine
  mtu              a PE's core port drops to MTU 1500                      -> IS-IS adjacency on that link fails (hello padding), big pings die
   chaos.py inject [fault] [--seed N]   (no fault = random)   chaos.py reveal   chaos.py repair   chaos.py list   chaos.py status"""
import json, random, subprocess, sys, time
from pathlib import Path

LAB = Path(__file__).resolve().parents[1]; STATE = LAB / ".chaos.json"
sys.path.insert(0, str(LAB / "tests" / "resources")); import LabLib   # noqa: E402
inv = json.loads(subprocess.run([str(LAB / "lab.sh"), "inventory"], capture_output=True, text=True, check=True).stdout)
N = {n["name"]: n for n in inv["nodes"]}; SVC = inv["service"]; TENANTS = sorted(SVC["tenants"]); PES = sorted(n for n in N if N[n]["role"] == "pe"); PS = sorted(n for n in N if N[n]["role"] == "p")
CE_OF = {n["pe"]: n["name"] for n in N.values() if n["role"] == "ce"}
L = LabLib.LabLib()
cfg = lambda node, *lines: L.vyos_configure(N[node]["mgmt_ip"], *lines)


def lan_of(pe, tenant):
    ce = CE_OF[pe]; return next(p["prefix"] for p in N[ce]["ports"] if p["peer"] and N[p["peer"]]["role"] == "host" and p["tenant"] == tenant)


def ac(pe, tenant):
    return next(p for p in N[pe]["ports"] if p["peer"] and N[p["peer"]]["role"] == "ce" and p["tenant"] == tenant)


FAULTS = {}
def fault(name):
    def deco(f): FAULTS[name] = f; return f
    return deco


@fault("rt-import")
def rt_import(r):
    pe, t = r.choice(PES), r.choice(TENANTS); rt = SVC["tenants"][t]["rt"]
    inject = [f"delete vrf name {t} protocols bgp address-family ipv4-unicast route-target vpn both", f"set vrf name {t} protocols bgp address-family ipv4-unicast route-target vpn export {rt}", f"set vrf name {t} protocols bgp address-family ipv4-unicast route-target vpn import 65000:999"]
    repair = [f"delete vrf name {t} protocols bgp address-family ipv4-unicast route-target vpn", f"set vrf name {t} protocols bgp address-family ipv4-unicast route-target vpn both {rt}"]
    return dict(node=pe, inject=inject, repair=repair, reveal=f"{pe}: VRF {t} imports route target 65000:999 instead of {rt} — {N[pe]['dc']}'s {t} site cannot see the other sites (its own LAN is still exported)")


@fault("locator-leak")
def locator_leak(r):
    pe, t = r.choice(PES), r.choice(TENANTS); far = r.choice([p for p in PES if p != pe]); loc = N[far]["locator"]; block = SVC["srv6"]["block"]
    leaks = [l for l in (LAB / "nodes" / pe / "vyos_config.txt").read_text().splitlines() if l.startswith(f"set vrf name {t} protocols static route6 ") and (f" {loc} " in l or f" {block} " in l)]
    inject = [f"delete vrf name {t} protocols static route6 {loc}", f"delete vrf name {t} protocols static route6 {block}"]
    return dict(node=pe, inject=inject, repair=leaks, reveal=f"{pe}: VRF {t} lost its static leaks for {far}'s locator {loc} and the block fallback — SRv6-encapsulated packets from {t} at {N[pe]['dc']} towards {far} have no outer route (watch nstat Ip6OutNoRoutes on {pe})")


@fault("ce-shutdown")
def ce_shutdown(r):
    pe, t = r.choice(PES), r.choice(TENANTS); ce = CE_OF[pe]; pe_ip = ac(pe, t)["ip"].split("/")[0]
    return dict(node=ce, inject=[f"set vrf name {t} protocols bgp neighbor {pe_ip} shutdown"], repair=[f"delete vrf name {t} protocols bgp neighbor {pe_ip} shutdown"],
                reveal=f"{ce}: the {t} eBGP session to {pe} ({pe_ip}) is administratively shut — {N[ce]['dc']}'s {t} site is down")


@fault("silent-cut")
def silent_cut(r):
    p = r.choice(PS); port = r.choice([x for x in N[p]["ports"] if x["peer"] and N[x["peer"]]["role"] == "pe"]); i = port["name"]
    inject = [f"set firewall ipv6 input filter rule 10 inbound-interface name {i}", "set firewall ipv6 input filter rule 10 action drop", f"set firewall ipv6 output filter rule 10 outbound-interface name {i}", "set firewall ipv6 output filter rule 10 action drop",
              f"set firewall ipv6 forward filter rule 10 inbound-interface name {i}", "set firewall ipv6 forward filter rule 10 action drop", f"set firewall ipv6 forward filter rule 11 outbound-interface name {i}", "set firewall ipv6 forward filter rule 11 action drop"]
    return dict(node=p, inject=inject, repair=["delete firewall"], reveal=f"{p}: a firewall silently drops everything on {i} (link to {port['peer']}) — interface up, IS-IS/BFD down on that link, traffic rerouted, one adjacency missing on {p} and {port['peer']}")


@fault("sid-export")
def sid_export(r):
    pe, t = r.choice(PES), r.choice(TENANTS)
    return dict(node=pe, inject=[f"delete vrf name {t} protocols bgp address-family ipv4-unicast sid vpn export"], repair=[f"set vrf name {t} protocols bgp address-family ipv4-unicast sid vpn export auto"],
                reveal=f"{pe}: VRF {t} no longer exports an SRv6 SID with its routes — the other PEs receive {N[pe]['dc']}'s {t} LAN without a SID and cannot encapsulate towards it")


@fault("blackhole")
def blackhole(r):
    pe, t = r.choice(PES), r.choice(TENANTS); far = r.choice([p for p in PES if p != pe]); lan = lan_of(far, t)
    return dict(node=pe, inject=[f"set vrf name {t} protocols static route {lan} blackhole"], repair=[f"delete vrf name {t} protocols static route {lan}"],
                reveal=f"{pe}: a static blackhole for {lan} in VRF {t} beats the BGP route — only {N[pe]['dc']} -> {N[far]['dc']} in {t} fails")


@fault("mtu")
def mtu(r):
    pe = r.choice(PES); port = r.choice([x for x in N[pe]["ports"] if x["peer"] and N[x["peer"]]["role"] == "p"]); i = port["name"]
    return dict(node=pe, inject=[f"set interfaces ethernet {i} mtu 1500"], repair=[f"set interfaces ethernet {i} mtu 9000"],
                reveal=f"{pe}: {i} (to {port['peer']}) is at MTU 1500 instead of 9000 — the IS-IS adjacency on it fails (hello padding) and jumbo pings die; {pe} is single-homed until repaired")


def grafana(text, tags):
    try:
        from labportal import grafana as g; return g.annotate(text, tags=["srv6-core", "chaos", *tags])
    except Exception: return None


cmd = sys.argv[1] if len(sys.argv) > 1 else "status"
if cmd == "list":
    for k, f in FAULTS.items(): print(f"{k:14s} {f.__doc__ or ''}")
    print("\n" + __doc__.split("Faults are")[1].split("chaos.py inject")[0].strip()); sys.exit()
if cmd == "status":
    print(json.dumps(json.loads(STATE.read_text()), indent=1) if STATE.exists() else "no fault active"); sys.exit()
if cmd == "inject":
    if STATE.exists(): sys.exit(f"a fault is already active since {time.ctime(json.loads(STATE.read_text())['time'])} — repair it first")
    name = next((a for a in sys.argv[2:] if a in FAULTS), None); seed = int(sys.argv[sys.argv.index("--seed") + 1]) if "--seed" in sys.argv else int(time.time())
    r = random.Random(seed); name = name or r.choice(sorted(FAULTS)); spec = FAULTS[name](r)
    out = cfg(spec["node"], *spec["inject"])
    STATE.write_text(json.dumps({"fault": name, "seed": seed, "time": time.time(), **spec, "annotation": grafana(f"srv6-core: drill — a fault was injected (reveal with tools/chaos.py reveal)", ["drill"])}, indent=1))
    print(f"fault injected (seed {seed}). Symptoms are yours to find; `chaos.py reveal` tells, `chaos.py repair` fixes."); sys.exit()
st = json.loads(STATE.read_text()) if STATE.exists() else sys.exit("no fault active")
if cmd == "reveal":
    print(f"[{st['fault']}] {st['reveal']}\n  injected on {st['node']} at {time.ctime(st['time'])}:\n    " + "\n    ".join(st["inject"])); sys.exit()
if cmd == "repair":
    cfg(st["node"], *st["repair"]); subprocess.run([str(LAB / "lab.sh"), "configure", st["node"]], capture_output=True)
    try:
        from labportal import grafana as g; g.update(st.get("annotation"), text=f"srv6-core: drill — {st['fault']} on {st['node']}, repaired", end=time.time(), tags=["srv6-core", "chaos", "drill", st["fault"]])
    except Exception: pass
    STATE.unlink(); print(f"repaired {st['fault']} on {st['node']} ({time.time() - st['time']:.0f} s after injection); intended configuration re-applied"); sys.exit()
sys.exit(__doc__)
