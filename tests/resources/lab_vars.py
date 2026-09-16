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
VRF = SERVICE["vrf"]
VRF_TABLE = SERVICE["table"]
CORE_AS = SERVICE["core_as"]
LINKS = INV["links"]
# IS-IS neighbours per core node: interface -> peer (only core links)
ISIS_NEIGHBORS = {n: {p["name"]: p["peer"] for p in NODES[n]["ports"] if p["peer"] and NODES[p["peer"]]["role"] in ("pe", "p")} for n in CORE}
# per DC: the CE, its PE, the LAN and the host with its LAN address
DCS = {}
for ce in CES:
    pe = NODES[ce]["pe"]; lan_port = next(p for p in NODES[ce]["ports"] if p["peer"] and NODES[p["peer"]]["role"] == "host")
    pe_port = next(p for p in NODES[ce]["ports"] if p["peer"] == pe)
    DCS[NODES[ce]["dc"]] = {"ce": ce, "pe": pe, "host": lan_port["peer"], "lan": lan_port["prefix"], "host_ip": NODES[lan_port["peer"]]["ports"][0]["ip"].split("/")[0],
                            "ce_lan_ip": lan_port["ip"].split("/")[0], "pe_ce_prefix": pe_port["prefix"], "ce_wan_ip": pe_port["ip"].split("/")[0],
                            "pe_wan_ip": next(x for x in NODES[pe]["ports"] if x["peer"] == ce)["ip"].split("/")[0], "rd": f"{CORE_AS}:{NODES[pe]['idx']}"}
HOST_IP = {d["host"]: d["host_ip"] for d in DCS.values()}
LAN_OF_PE = {d["pe"]: d["lan"] for d in DCS.values()}
# IS-IS system id (the 6 bytes between the area and the NSEL) as `show isis` prints it
SYSID = {n: ".".join(NODES[n]["isis_net"].split(".")[-4:-1]) for n in CORE}
