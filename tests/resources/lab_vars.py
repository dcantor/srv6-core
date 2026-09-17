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
CORE = PES + PS
VYOS = PES + PS + CES
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
        SITES[t][NODES[ce]["dc"]] = {"tenant": t, "ce": ce, "pe": pe, "host": lan_port["peer"], "lan": lan_port["prefix"], "host_ip": NODES[lan_port["peer"]]["ports"][0]["ip"].split("/")[0],
                                     "ce_lan_ip": lan_port["ip"].split("/")[0], "pe_ce_prefix": pe_port["prefix"], "ce_wan_ip": pe_port["ip"].split("/")[0],
                                     "pe_wan_ip": next(x for x in NODES[pe]["ports"] if x["peer"] == ce and x["tenant"] == t)["ip"].split("/")[0],
                                     "rd": f"{CORE_AS}:{VRF_TABLE[t] + NODES[pe]['idx']}", "ce_vrf": t}
DCS = SITES[TENANTS[0]]                                   # first tenant, kept for the suites that only need one
HOST_IP = {s["host"]: s["host_ip"] for t in SITES.values() for s in t.values()}
HOST_TENANT = {s["host"]: t for t, sites in SITES.items() for s in sites.values()}
# IS-IS system id (the 6 bytes between the area and the NSEL) as `show isis` prints it
SYSID = {n: ".".join(NODES[n]["isis_net"].split(".")[-4:-1]) for n in CORE}
