#!/usr/bin/env python3
"""Ansible dynamic inventory from `lab.sh inventory`: groups pe / p / ce (VyOS, network_cli) and host (Alpine, ssh),
plus tenants and dcs as groups; every node carries its lab facts as host vars (role, dc, loopback6, locator, asn, rd, ports).
   ansible-inventory --list | --graph"""
import json, subprocess, sys
from pathlib import Path

LAB = Path(__file__).resolve().parents[1]
inv = json.loads(subprocess.run([str(LAB / "lab.sh"), "inventory"], capture_output=True, text=True, check=True).stdout)
out = {"_meta": {"hostvars": {}}, "all": {"children": ["vyos", "hosts", "external"]}, "vyos": {"children": ["pe", "p", "ce"]}, "hosts": {"hosts": []}, "external": {"hosts": []},
       "pe": {"hosts": []}, "p": {"hosts": []}, "ce": {"hosts": []},
       "vyos": {"children": ["pe", "p", "ce"], "vars": {"ansible_network_os": "vyos.vyos.vyos", "ansible_connection": "ansible.netcommon.network_cli", "ansible_user": "vyos", "ansible_password": "vyos", "ansible_become": False}},
       "hosts": {"hosts": [], "vars": {"ansible_user": "lab", "ansible_password": "lab", "ansible_connection": "ssh"}}}
for n in inv["nodes"]:
    g = {"pe": "pe", "p": "p", "ce": "ce", "host": "hosts", "ext-ce": "external"}[n["role"]]
    out[g]["hosts"].append(n["name"])
    out["_meta"]["hostvars"][n["name"]] = {"ansible_host": n["mgmt_ip"], "lab_role": n["role"], "dc": n["dc"], "loopback6": n["loopback6"], "locator": n["locator"], "asn": n["asn"], "rd": n["rd"], "ports": n["ports"],
                                           "intended_config": str(LAB / "nodes" / n["name"] / "vyos_config.txt")}
    out.setdefault(n["dc"], {"hosts": []})["hosts"].append(n["name"])
for t in inv["service"]["tenants"]:
    out[t.replace("-", "_")] = {"hosts": sorted({n["name"] for n in inv["nodes"] for p in n["ports"] if p.get("tenant") == t})}
out["all"]["vars"] = {"service": inv["service"]}
if "--host" in sys.argv: print(json.dumps(out["_meta"]["hostvars"].get(sys.argv[-1], {})))
else: print(json.dumps(out, indent=1))
