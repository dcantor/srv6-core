"""Robot variables derived from lab.conf (through `lab.sh inventory`) so the suites never hard-code the topology."""
import json, subprocess
from pathlib import Path

LAB_DIR = Path(__file__).resolve().parents[2]
INV = json.loads(subprocess.run([str(LAB_DIR / "lab.sh"), "inventory"], capture_output=True, text=True, check=True).stdout)
SERVICE = INV["service"]
NODES = {n["name"]: n for n in INV["nodes"]}
PES = sorted(n for n, v in NODES.items() if v["role"] == "pe")
PS = sorted(n for n, v in NODES.items() if v["role"] == "p")
CES = sorted(n for n, v in NODES.items() if v["role"] == "ce")
HOSTS = sorted(n for n, v in NODES.items() if v["role"] == "host")
FWS = sorted(n for n, v in NODES.items() if v["role"] == "fw")   # the internet breakout firewall (VyOS too)
CORE = PES + PS
VYOS = PES + PS + CES + FWS
MGMT = {n: v["mgmt_ip"] for n, v in NODES.items()}
LOOPBACK = {n: NODES[n]["loopback6"] for n in CORE}
LOCATOR = {n: NODES[n]["locator"] for n in CORE}
RR = SERVICE["rr"]
RRS = SERVICE["rrs"]                                       # every PE peers with every reflector
CORE_AS = SERVICE["core_as"]
SRV6 = SERVICE["srv6"]                                   # {block, format, block_len, node_len, func_bits}
USID = SRV6["format"].startswith("usid")
TENANTS = sorted(SERVICE["tenants"])                       # every tenant is a VRF on every PE
VRF_TABLE = {t: v["table"] for t, v in SERVICE["tenants"].items()}
VRF_RT = {t: v["rt"] for t, v in SERVICE["tenants"].items()}
LINKS = INV["links"]
# IS-IS neighbours per core node: interface -> peer (only core links)
ISIS_NEIGHBORS = {n: {p["name"]: p["peer"] for p in NODES[n]["ports"] if p["peer"] and NODES[p["peer"]]["role"] in ("pe", "p")} for n in CORE}
# per tenant and DC: the CE, its PE, both ends of the attachment circuit, the LAN and the host
SITES = {t: {} for t in TENANTS}
for ce in CES:
    pe = NODES[ce]["pe"]
    for lan_port in (p for p in NODES[ce]["ports"] if p["peer"] and NODES[p["peer"]]["role"] == "host"):
        t = lan_port["tenant"]; pe_port = next(p for p in NODES[ce]["ports"] if p["peer"] == pe and p["tenant"] == t)
        hp = NODES[lan_port["peer"]]["ports"][0]; pe_p = next(x for x in NODES[pe]["ports"] if x["peer"] == ce and x["tenant"] == t)
        SITES[t][NODES[ce]["dc"]] = {"tenant": t, "ce": ce, "pe": pe, "host": lan_port["peer"], "lan": lan_port["prefix"], "host_ip": hp["ip"].split("/")[0],
                                     "ce_lan_ip": lan_port["ip"].split("/")[0], "pe_ce_prefix": pe_port["prefix"], "ce_wan_ip": pe_port["ip"].split("/")[0],
                                     "pe_wan_ip": pe_p["ip"].split("/")[0], "rd": f"{CORE_AS}:{VRF_TABLE[t] + NODES[pe]['idx']}", "ce_vrf": t,
                                     # the IPv6 twin of every tenant address (dual-stack sites)
                                     "lan6": lan_port.get("prefix6"), "host_ip6": (hp.get("ip6") or "/").split("/")[0] or None, "ce_lan_ip6": (lan_port.get("ip6") or "/").split("/")[0] or None,
                                     "pe_ce_prefix6": pe_port.get("prefix6"), "ce_wan_ip6": (pe_port.get("ip6") or "/").split("/")[0] or None, "pe_wan_ip6": (pe_p.get("ip6") or "/").split("/")[0] or None}
DCS = SITES[TENANTS[0]]                                   # first tenant, kept for the suites that only need one
HOST_IP = {s["host"]: s["host_ip"] for t in SITES.values() for s in t.values()}
HOST_IP6 = {s["host"]: s["host_ip6"] for t in SITES.values() for s in t.values()}
HOST_TENANT = {s["host"]: t for t, sites in SITES.items() for s in sites.values()}
# External CEs: another lab's routers attached to a tenant (the IPsec headends of cat8000v-ipsec). EXT_SITES per external CE:
# its PE, both ends of the attachment circuit, its AS, and the LANs that lab advertises (its own site LAN and, through its
# IPsec tunnels, the branch LANs) read from that lab's Nautobot-derived NaC data.
EXT_CES = sorted(n for n, v in NODES.items() if v["role"] == "ext-ce")
EXT_SITES = {}
for ce in EXT_CES:
    n = NODES[ce]; pe = n["pe"]; port = next(p for p in n["ports"] if p["peer"] == pe); t = port["tenant"]
    EXT_SITES[ce] = {"tenant": t, "pe": pe, "lab": n["lab"], "asn": n["asn"], "mgmt_ip": n["mgmt_ip"], "pe_ce_prefix": port["prefix"], "ce_wan_ip": port["ip"].split("/")[0],
                     "pe_wan_ip": next(x for x in NODES[pe]["ports"] if x["peer"] == ce)["ip"].split("/")[0], "pe_port": next(x for x in NODES[pe]["ports"] if x["peer"] == ce)["name"], "ce_port": port["name"]}
EXT_LAB_DIR = {}
if EXT_CES:
    for line in subprocess.run(["bash", "-c", f"source {LAB_DIR}/lab.conf; for n in \"${{EXT_NODES[@]}}\"; do echo \"$n ${{EXT_LAB[$n]}} ${{BGP_AS[$n]}}\"; done"], capture_output=True, text=True).stdout.splitlines():
        name, path, _ = line.split(); EXT_LAB_DIR[name] = path
# LANs the external lab advertises into the tenant, per router: {router: {"lan": prefix, "loopback": ip, "asn": asn}} — from that lab's NaC data
EXT_LAB_ROUTERS = {}
for path in set(EXT_LAB_DIR.values()):
    nac = Path(path) / "nac" / "data" / "devices.nac.yaml"
    if nac.exists():
        import yaml
        mgmt = dict(l.split() for l in subprocess.run(["bash", "-c", f"source {path}/lab.conf; for n in \"${{!MGMT_IP[@]}}\"; do echo \"$n ${{MGMT_IP[$n]}}\"; done"], capture_output=True, text=True).stdout.splitlines())
        for dev in yaml.safe_load(nac.read_text())["iosxe"]["devices"]:
            c = dev["configuration"]; lo = {l["id"]: l["ipv4"]["address"] for l in c["interfaces"].get("loopbacks", [])}
            EXT_LAB_ROUTERS[dev["name"]] = {"lan": f"{lo[10].rsplit('.', 1)[0]}.0/24" if 10 in lo else None, "loopback": lo.get(0), "asn": c["routing"]["bgp"]["as_number"], "mgmt_ip": mgmt.get(dev["name"])}
EXT_LANS = sorted(r["lan"] for r in EXT_LAB_ROUTERS.values() if r["lan"])            # every site LAN of the external lab (headends + branches)
EXT_LAN_IP = {lan: lan.rsplit(".", 1)[0] + ".1" for lan in EXT_LANS}                 # the router's address in it (Loopback10 .1)
# IS-IS system id (the 6 bytes between the area and the NSEL) as `show isis` prints it
SYSID = {n: ".".join(NODES[n]["isis_net"].split(".")[-4:-1]) for n in CORE}
# Internet breakout: the firewall is a CE of every tenant on one PE (VRF-lite, one circuit per tenant, IPv4 only) announcing a
# default route; its uplink is the host's libvirt NAT network. INTERNET_CIRCUITS per tenant: PE / firewall ends of the circuit.
INTERNET = SERVICE.get("internet")
INTERNET_CIRCUITS = {}
if INTERNET:
    for l in LINKS:
        if l["b"] == INTERNET["fw"]:
            INTERNET_CIRCUITS[l["tenant"]] = {"pe": l["a"], "pe_port": l["a_port"], "pe_ip": l["a_ip"].split("/")[0], "fw": l["b"], "fw_port": l["b_port"], "fw_ip": l["b_ip"].split("/")[0],
                                              "prefix": l["prefix"], "asn": INTERNET["asn"], "fw_mgmt": NODES[INTERNET["fw"]]["mgmt_ip"]}
    INTERNET_UPLINK = next(p["name"] for p in NODES[INTERNET["fw"]]["ports"] if p.get("network"))
INTERNET_PROBE = "1.1.1.1"     # a public address every host must reach through the breakout (ICMP), and a URL over TCP
INTERNET_URL = "http://example.com"

# Extra loopbacks injected on every CE (lab.conf CE_LOOPBACKS / CE_LOOPBACK_TENANTS): {ce: {tenant: [address, ...]}},
# and the same set flattened per tenant — what every site of that tenant must be able to reach.
CE_LOOPBACKS = {ce: {} for ce in CES}
for ce in CES:
    for l in NODES[ce].get("loopbacks") or []:
        CE_LOOPBACKS[ce].setdefault(l["tenant"], []).append(l["address"])
CE_LOOPBACK_TENANTS = sorted({t for v in CE_LOOPBACKS.values() for t in v})
CE_LOOPBACK_IPS = {t: [a.split("/")[0] for ce in CES for a in CE_LOOPBACKS[ce].get(t, [])] for t in CE_LOOPBACK_TENANTS}
CE_LOOPBACK_OF = {t: {ce: [a.split("/")[0] for a in CE_LOOPBACKS[ce].get(t, [])] for ce in CES} for t in CE_LOOPBACK_TENANTS}

# BGP looking glass (role lg): a passive route collector peering with every reflector over its own link, serving the
# looking-glass API on LG_PORT. LG_PEERS: what the collector's sessions should be (the reflector, and both link ends).
LGS = sorted(n for n, v in NODES.items() if v["role"] == "lg")
LG = LGS[0] if LGS else None
LG_PORT = int(subprocess.run(["bash", "-c", f"source {LAB_DIR}/lab.conf; echo ${{LG_PORT:-8080}}"], capture_output=True, text=True).stdout.strip() or 8080)
LG_URL = f"http://{NODES[LG]['mgmt_ip']}:{LG_PORT}" if LG else None
LG_PEERS = {}
if LG:
    for p in NODES[LG]["ports"]:
        if not p["peer"]: continue
        LG_PEERS[p["peer"]] = {"rr": p["peer"], "lg_ip": p["ip"].split("/")[0], "port": p["name"],
                               "rr_ip": next(x["ip"] for x in NODES[p["peer"]]["ports"] if x["peer"] == LG).split("/")[0]}
# RD -> (tenant, PE): what the collector must resolve a VPN route to
RD_MAP = {f"{CORE_AS}:{VRF_TABLE[t] + NODES[pe]['idx']}": {"vrf": t, "pe": pe} for t in TENANTS for pe in PES}
