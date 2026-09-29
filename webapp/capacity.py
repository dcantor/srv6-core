"""Capacity: how much room the lab has for more tenants and sites, and which limit runs out first.

A new tenant takes, per site: one PE port (its attachment circuit), two CE ports (the circuit and the LAN), one host VM
(HOST_RAM_MIB of the lab host's memory) and a management address; per tenant: a letter (tenant-a … tenant-h), an
attachment-circuit block and a LAN block (tenants.AC_BLOCK / LAN_BLOCK), a kernel table and a route target — and,
for internet breakout, a port on the firewall's PE and one on the firewall.

    room: how many more tenants fit at every data centre, and at each one alone; how many more sites each existing
          tenant can add; the limit that runs out first, for each answer.

The lab host's memory and CPUs are read live (every lab on the host counts)."""
import re, subprocess, time
from pathlib import Path

import tenants as T

LAB = Path(__file__).resolve().parents[1]
RESERVE_MIB = 4096              # memory the lab host keeps for itself and the other labs' growth
WARN, CRIT = 0.75, 0.90


def _conf_int(name, default):
    m = re.search(rf"^{name}=(\d+)", (LAB / "lab.conf").read_text(), re.M)
    return int(m[1]) if m else default


def _meminfo():
    kv = {}
    for l in Path("/proc/meminfo").read_text().splitlines():
        k, v = l.split(":", 1); kv[k] = int(v.split()[0])
    return kv


def _cpu_busy(sample=0.4):
    def read():
        f = [int(x) for x in Path("/proc/stat").read_text().split("\n", 1)[0].split()[1:]]
        return sum(f), f[3] + f[4]
    t1, i1 = read(); time.sleep(sample); t2, i2 = read()
    return round(100 * (1 - (i2 - i1) / max(1, t2 - t1)), 1)


def _vcpus():
    try:
        out = subprocess.run(["sg", "libvirt", "-c", "virsh -c qemu:///system domstats --list-running --vcpu"],
                             capture_output=True, text=True, timeout=30).stdout
        v = [int(x) for x in re.findall(r"vcpu\.current=(\d+)", out)]
        return len(v), sum(v)
    except Exception:                                                   # noqa: BLE001
        return None, None


def _res(name, used, total, unit="", note="", warn=WARN, crit=CRIT):
    pct = round(100 * used / total, 1) if total else None
    state = "unknown" if pct is None else ("critical" if pct >= crit * 100 else "warning" if pct >= warn * 100 else "ok")
    return {"name": name, "used": used, "total": total, "free": (total - used) if total is not None else None, "unit": unit,
            "pct": pct, "state": state, "note": note}


def compute():
    f = T.facts(); inv, N = f["inv"], f["N"]
    host_mib = _conf_int("HOST_RAM_MIB", T.HOST_RAM_MIB)
    dcs = f["dcs"]
    # ---- per data centre: the PE's and the CE's ports -----------------------------------------------------------
    per_dc = []
    for dc in dcs:
        pe, ce = f["pe_of"][dc], f["ce_of"][dc]
        pe_ports, ce_ports = N[pe]["ports"], N[ce]["ports"]
        pe_free, ce_free = T.free_ports(f, pe), T.free_ports(f, ce)
        per_dc.append({"dc": dc, "pe": pe, "ce": ce,
                       "pe_ports": _res(f"{pe} ports", len(pe_ports) - len(pe_free), len(pe_ports), note="one per tenant site (attachment circuit)"),
                       "ce_ports": _res(f"{ce} ports", len(ce_ports) - len(ce_free), len(ce_ports), note="two per tenant site (circuit + LAN)"),
                       "pe_free": pe_free, "ce_free": ce_free,
                       "sites_fit": min(len(pe_free), len(ce_free) // 2),
                       "tenants_here": sorted({l["tenant"] for l in inv["links"] if l.get("tenant") and ce in (l["a"], l["b"])})})
    # ---- lab-wide ------------------------------------------------------------------------------------------------
    n_tenants = len(f["tenants"])
    letters = _res("Tenant letters", n_tenants, len(T.TENANT_LETTERS), note="tenant-a … tenant-" + T.TENANT_LETTERS[-1])
    blocks = _res("Address blocks", n_tenants, min(len(T.AC_BLOCK), len(T.LAN_BLOCK)), note="an attachment-circuit /16 and a LAN /16 per tenant")
    fw = next((n for n in inv["nodes"] if n["role"] == "fw"), None)
    fw_res = None
    if fw:
        tp = [p for p in fw["ports"] if not p.get("network")]           # the uplink sits on a libvirt network, not a tenant circuit
        fw_used = [p for p in tp if p.get("peer")]
        fw_res = _res(f"{fw['name']} tenant circuits", len(fw_used), len(tp), note="internet breakout: one circuit per tenant (the uplink not counted)")
    mem = _meminfo(); total = mem["MemTotal"] // 1024; avail = mem["MemAvailable"] // 1024
    vms, vcpus = _vcpus(); cores = len(re.findall(r"^processor", Path("/proc/cpuinfo").read_text(), re.M))
    host = {"mem_total_mib": total, "mem_available_mib": avail, "reserve_mib": RESERVE_MIB, "host_vm_mib": host_mib,
            "cores": cores, "vms_running": vms, "vcpus_allocated": vcpus, "cpu_busy_pct": _cpu_busy(),
            "resources": [_res("Memory in use", total - avail, total, "MiB"), _res("CPU busy", 0, 100, "%")]}
    host["resources"][1] = _res("CPU busy", host["cpu_busy_pct"], 100, "%", note="all cores, now")
    if vcpus is not None:
        host["resources"].append(_res("vCPUs allocated", vcpus, cores, note="running VMs, every lab; above 100% is overcommit", warn=1.0, crit=2.0))
    hosts_fit = max(0, avail - RESERVE_MIB) // host_mib
    mgmt_used = {int(n["mgmt_ip"].split(".")[-1]) for n in inv["nodes"] if n.get("mgmt_ip", "").startswith("10.3.0.")}
    mgmt_free = len([x for x in range(20, 255) if x not in mgmt_used])

    # ---- room to grow --------------------------------------------------------------------------------------------
    def tenants_at(dc_list):
        limits = {"tenant letters": letters["free"], "address blocks": blocks["free"],
                  "host memory": hosts_fit // max(1, len(dc_list)), "management addresses": mgmt_free // max(1, len(dc_list))}
        for d in per_dc:
            if d["dc"] in dc_list:
                limits[f"ports at {d['dc']} ({d['pe'] if len(d['pe_free']) <= len(d['ce_free']) // 2 else d['ce']})"] = d["sites_fit"]
        first = min(limits, key=limits.get)
        return {"tenants": limits[first], "limited_by": first, "limits": limits}

    room = {"every_dc": tenants_at(dcs), "per_dc": {d["dc"]: tenants_at([d["dc"]]) for d in per_dc},
            "internet_breakout": fw_res["free"] if fw_res else None,
            "sites_per_tenant": {t: {"missing_dcs": [d["dc"] for d in per_dc if t not in d["tenants_here"]],
                                     "can_add": [d["dc"] for d in per_dc if t not in d["tenants_here"] and d["sites_fit"] > 0 and hosts_fit > 0]}
                                 for t in f["order"]}}
    lab = [letters, blocks] + ([fw_res] if fw_res else []) + [_res("Management addresses", 235 - mgmt_free, 235, note="10.3.0.20 – .254")]
    everything = [d["pe_ports"] for d in per_dc] + [d["ce_ports"] for d in per_dc] + lab + host["resources"]
    order = {"unknown": 0, "ok": 1, "warning": 2, "critical": 3}
    warnings = [f"{r['name']}: {r['pct']}%" for r in everything if r["state"] in ("warning", "critical")]
    if room["every_dc"]["tenants"] == 0:
        warnings.append(f"no room for another tenant at every data centre: {room['every_dc']['limited_by']}")
    return {"generated": time.time(), "per_dc": per_dc, "lab": lab, "host": host, "room": room, "hosts_fit": hosts_fit,
            "state": max((r["state"] for r in everything), key=order.get), "warnings": warnings}
