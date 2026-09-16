#!/usr/bin/env python3
"""Snapshot every VyOS node into a results directory: the running configuration (`show configuration commands`,
into <dir>/<node>.config.txt — diffed pre vs post by run.sh) and the routing tables (<dir>/routes/<node>.routes.txt:
IPv4/IPv6 RIB of the default VRF, every tenant VRF, the kernel's SRv6 routes (seg6 / seg6local), BGP VPNv4 and
IS-IS SRv6 state — kept for the record, not diffed, as they carry timers).      capture_configs.py <dir>"""
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "resources"))
from LabLib import LabLib                                   # noqa: E402
from lab_vars import VYOS, MGMT, NODES, TENANTS, RRS, CORE  # noqa: E402

out = Path(sys.argv[1]); routes = out / "routes"; routes.mkdir(parents=True, exist_ok=True)
lib = LabLib(); stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def route_commands(name):
    role = NODES[name]["role"]
    cmds = [("show ip route", "IPv4 RIB, default VRF"), ("show ipv6 route", "IPv6 RIB, default VRF")]
    if role in ("pe", "ce"):
        cmds += [(f"show ip route vrf {t}", f"IPv4 RIB, VRF {t}") for t in TENANTS]
    if role in ("pe", "p"):
        cmds += [("show isis route", "IS-IS SPF result"), ("show isis segment-routing srv6 node", "IS-IS SRv6 nodes"), ("show segment-routing srv6 locator", "SRv6 locators")]
    if role == "pe":
        cmds += [("show bgp ipv4 vpn", "BGP VPNv4 table"), ("show bgp segment-routing srv6", "BGP SRv6 SIDs"),
                 ("sudo ip -c=never -6 route show | grep seg6local", "kernel: local SIDs (seg6local)")]
        cmds += [(f"sudo ip -c=never route show vrf {t}", f"kernel: VRF {t} (SRv6 encapsulation routes)") for t in TENANTS]
    if name in RRS:
        cmds += [("show bgp ipv4 vpn summary", "VPNv4 clients"), ("show bgp ipv4 vpn", "BGP VPNv4 table (reflected)")]
    if role in ("pe", "p"):
        cmds += [("show bfd peers brief", "BFD sessions"), ("show isis neighbor", "IS-IS adjacencies")]
    return cmds


try:
    for name in VYOS:
        try:
            cfg = lib.run_vyos_command(MGMT[name], "show configuration commands", timeout=120)
        except Exception as e:      # a node that is down must not stop the capture
            print(f"[{name}] {e.__class__.__name__}: {e}"); continue
        path = out / f"{name}.config.txt"
        path.write_text(f"# {name} ({MGMT[name]}) show configuration commands captured {stamp}\n{cfg}\n")
        print(f"[{name}] {path} ({len(cfg)} bytes)")
        blocks = []
        for cmd, title in route_commands(name):
            try:
                txt = lib.run_vyos_command(MGMT[name], cmd, timeout=120)
            except Exception as e:  # noqa: BLE001
                txt = f"{e.__class__.__name__}: {e}"
            blocks.append(f"==== {title}: {cmd}\n{txt.rstrip()}\n")
        rpath = routes / f"{name}.routes.txt"
        rpath.write_text(f"# {name} ({MGMT[name]}) routing tables captured {stamp}\n\n" + "\n".join(blocks))
        print(f"[{name}] {rpath} ({rpath.stat().st_size} bytes, {len(blocks)} tables)")
finally:
    lib.close_all_connections()
