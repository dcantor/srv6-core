"""Tenant provisioning: facts from lab.conf, suggestions for a new tenant / site, validation, and the plan of what
changes where (lab.conf, VMs, Nautobot) — the pieces the portal's runs execute."""
import ipaddress, json, re, subprocess
from pathlib import Path
import labconf

LAB = Path(__file__).resolve().parents[1]
CORE_AS = 65000; TABLE_STEP = 100; HOST_RAM_MIB = 256
TENANT_LETTERS = "abcdefgh"


def inventory():
    return json.loads(subprocess.run([str(LAB / "lab.sh"), "inventory"], capture_output=True, text=True, check=True).stdout)


def facts(inv=None):
    inv = inv or inventory(); N = {n["name"]: n for n in inv["nodes"]}
    tenants = inv["service"]["tenants"]
    # tenant index (0 = tenant-a, 1 = tenant-b, ...) drives the address blocks: PE-CE 172.(16+i).n.0/30, LAN 172.(20+i).n.0/24
    order = sorted(tenants, key=lambda t: tenants[t]["table"])
    dcs = sorted({n["dc"] for n in inv["nodes"] if n["dc"] != "core"})
    used_ports = {(n["name"], p["name"]) for n in inv["nodes"] for p in n["ports"] if p["peer"]}
    return {"inv": inv, "N": N, "tenants": tenants, "order": order, "dcs": dcs, "used_ports": used_ports,
            "pe_of": {n["dc"]: n["name"] for n in inv["nodes"] if n["role"] == "pe"}, "ce_of": {n["dc"]: n["name"] for n in inv["nodes"] if n["role"] == "ce"},
            "used_idx": {n["idx"] for n in inv["nodes"]}, "used_console": {n["console"] for n in inv["nodes"]}, "used_mgmt": {n["mgmt_ip"] for n in inv["nodes"]},
            "used_prefixes": {l["prefix"] for l in inv["links"]}, "hosts": [n for n in inv["nodes"] if n["role"] == "host"]}


def free_ports(f, node):
    n = f["N"][node]; return [p["name"] for p in n["ports"] if (node, p["name"]) not in f["used_ports"]]


def next_free(used, start):
    v = start
    while v in used: v += 1
    return v


def sites_for(f, tenant, index, dcs):
    """Per DC: attachment circuit (PE ethX <-> CE ethY), LAN (CE ethZ <-> new host), host VM identity."""
    sites = []; idx_used, con_used, mgmt_used = set(f["used_idx"]), set(f["used_console"]), set(f["used_mgmt"])
    for dc in dcs:
        n = int(dc[2:]); pe, ce = f["pe_of"][dc], f["ce_of"][dc]
        pe_free, ce_free = free_ports(f, pe), free_ports(f, ce)
        if not pe_free or len(ce_free) < 2: sites.append({"dc": dc, "pe": pe, "ce": ce, "error": f"no free ports on {pe if not pe_free else ce}"}); continue
        host = f"{dc}-h{index + 1}"; idx = next_free(idx_used, 20); idx_used.add(idx)
        console = next_free(con_used, 5320); con_used.add(console); mgmt = f"10.3.0.{40 + 10 * index + n}"
        if mgmt in mgmt_used: mgmt = f"10.3.0.{next_free({int(m.split('.')[-1]) for m in mgmt_used}, 60)}"
        mgmt_used.add(mgmt)
        sites.append({"dc": dc, "pe": pe, "ce": ce, "pe_port": pe_free[0], "ce_pe_port": ce_free[0], "ce_lan_port": ce_free[1],
                      "attachment_circuit": f"172.{16 + index}.{n}.0/30", "lan": f"172.{20 + index}.{n}.0/24",
                      "host": host, "host_mgmt": mgmt, "host_console": console, "host_idx": idx, "host_ip": f"172.{20 + index}.{n}.2", "gateway": f"172.{20 + index}.{n}.1"})
    return sites


def suggest(dcs=None):
    """A fully allocated new tenant: next letter, next kernel table, RT, one site per DC (or the given DCs)."""
    f = facts(); index = len(f["order"])
    if index >= len(TENANT_LETTERS): return {"error": "no tenant letters left"}
    name = f"tenant-{TENANT_LETTERS[index]}"; table = max(v["table"] for v in f["tenants"].values()) + TABLE_STEP
    dcs = [d for d in (dcs or f["dcs"]) if d in f["dcs"]]
    return {"name": name, "table": table, "rt": f"{CORE_AS}:{table}", "index": index, "description": f"{name}: L3VPN over SRv6 (End.DT4)",
            "sites": sites_for(f, name, index, dcs), "context": {"tenants": f["order"], "dcs": f["dcs"], "existing": {t: {**v, "sites": [s["dc"] for s in tenant_sites(f, t)]} for t, v in f["tenants"].items()}}}


def tenant_sites(f, tenant):
    out = []
    for l in f["inv"]["links"]:
        if l.get("tenant") == tenant and f["N"][l["b"]]["role"] == "host":
            ce = l["a"]; dc = f["N"][ce]["dc"]; ac = next(x for x in f["inv"]["links"] if x.get("tenant") == tenant and x["b"] == ce)
            out.append({"dc": dc, "pe": ac["a"], "ce": ce, "host": l["b"], "lan": l["prefix"], "host_ip": l["b_ip"].split("/")[0], "attachment_circuit": ac["prefix"],
                        "pe_port": ac["a_port"], "ce_pe_port": ac["b_port"], "ce_lan_port": l["a_port"], "host_mgmt": f["N"][l["b"]]["mgmt_ip"], "rd": f["N"][ac["a"]]["rd"].get(tenant)})
    return sorted(out, key=lambda s: s["dc"])


def suggest_site(tenant, dc):
    """A new site for an existing tenant (the tenant's address blocks, next free ports, a new host)."""
    f = facts()
    if tenant not in f["tenants"]: return {"error": f"unknown tenant {tenant}"}
    if dc not in f["dcs"]: return {"error": f"unknown site {dc}"}
    if any(s["dc"] == dc for s in tenant_sites(f, tenant)): return {"error": f"{tenant} already has a site in {dc}"}
    index = f["order"].index(tenant); site = sites_for(f, tenant, index, [dc])[0]
    return {"name": tenant, "table": f["tenants"][tenant]["table"], "rt": f["tenants"][tenant]["rt"], "index": index, "sites": [site]}


def validate(spec, new_tenant=True):
    """Problems with a tenant / site spec (names, table, RT, prefixes, ports, hosts) against the current lab."""
    f = facts(); errs = []
    name = spec.get("name", "")
    if not re.fullmatch(r"[a-z][a-z0-9-]{1,30}", name): errs.append("tenant name: lowercase letters, digits and dashes")
    if new_tenant and name in f["tenants"]: errs.append(f"tenant {name} already exists")
    if not new_tenant and name not in f["tenants"]: errs.append(f"tenant {name} does not exist")
    try:
        table = int(spec.get("table"))
        if not 1 <= table <= 65000: errs.append("kernel table must be 1..65000")
        if new_tenant and table in {v["table"] for v in f["tenants"].values()}: errs.append(f"kernel table {table} is in use")
    except (TypeError, ValueError): errs.append("kernel table must be a number")
    if not re.fullmatch(r"\d+:\d+", str(spec.get("rt", ""))): errs.append("route target must be ASN:NN")
    elif new_tenant and spec["rt"] in {v["rt"] for v in f["tenants"].values()}: errs.append(f"route target {spec['rt']} is in use")
    sites = spec.get("sites") or []
    if not sites: errs.append("at least one site")
    seen_dc, seen_pfx, seen_host = set(), set(), set()
    for s in sites:
        dc = s.get("dc")
        if dc not in f["dcs"]: errs.append(f"unknown site {dc}"); continue
        if dc in seen_dc: errs.append(f"{dc} listed twice"); continue
        seen_dc.add(dc)
        if not new_tenant and any(x["dc"] == dc for x in tenant_sites(f, name)): errs.append(f"{name} already has a site in {dc}")
        pe, ce = f["pe_of"][dc], f["ce_of"][dc]
        for node, key, label in ((pe, "pe_port", "PE port"), (ce, "ce_pe_port", "CE port to the PE"), (ce, "ce_lan_port", "CE LAN port")):
            port = s.get(key, "")
            if port not in [p["name"] for p in f["N"][node]["ports"]]: errs.append(f"{dc}: {label} {port} does not exist on {node}")
            elif (node, port) in f["used_ports"]: errs.append(f"{dc}: {node} {port} is already wired")
        if s.get("ce_pe_port") == s.get("ce_lan_port"): errs.append(f"{dc}: the two CE ports must differ")
        for key, plen, label in (("attachment_circuit", 30, "attachment circuit"), ("lan", 24, "LAN")):
            try:
                net = ipaddress.IPv4Network(s.get(key, ""))
                if net.prefixlen != plen: errs.append(f"{dc}: {label} must be a /{plen}")
                if str(net) in f["used_prefixes"] or str(net) in seen_pfx: errs.append(f"{dc}: {label} {net} is in use")
                if any(net.overlaps(ipaddress.ip_network(p)) for p in f["used_prefixes"] | seen_pfx if ipaddress.ip_network(p).version == 4): errs.append(f"{dc}: {label} {net} overlaps an existing prefix")
                seen_pfx.add(str(net))
            except ValueError: errs.append(f"{dc}: {label} is not a valid IPv4 prefix")
        host = s.get("host", "")
        if not re.fullmatch(r"[a-z0-9][a-z0-9-]{1,20}", host): errs.append(f"{dc}: host name invalid")
        elif host in f["N"] or host in seen_host: errs.append(f"{dc}: host {host} already exists")
        seen_host.add(host)
        try:
            ipaddress.IPv4Address(s.get("host_mgmt", ""))
            if s["host_mgmt"] in f["used_mgmt"]: errs.append(f"{dc}: management address {s['host_mgmt']} is in use")
            if not s["host_mgmt"].startswith("10.3.0."): errs.append(f"{dc}: management address must be in 10.3.0.0/24")
        except ValueError: errs.append(f"{dc}: management address invalid")
        for key, used, label in (("host_console", f["used_console"], "console port"), ("host_idx", f["used_idx"], "node index")):
            try:
                if int(s.get(key)) in used: errs.append(f"{dc}: {label} {s[key]} is in use")
            except (TypeError, ValueError): errs.append(f"{dc}: {label} must be a number")
        # a foreign VM with the host's name would block `lab.sh up`
        if host and subprocess.run(["virsh", "-c", "qemu:///system", "dominfo", host], capture_output=True).returncode == 0: errs.append(f"{dc}: a VM named {host} already exists on the host")
    return errs


def apply_to_labconf(spec, new_tenant=True):
    """Write the tenant / sites into lab.conf; returns the list of CEs whose VM definition changes (new NIC wiring) and the new hosts."""
    text = labconf.read()
    if new_tenant: text = labconf.add_tenant(text, spec["name"], int(spec["table"]), spec["rt"])
    ces, hosts = [], []
    for s in spec["sites"]:
        text = labconf.add_host(text, s["host"], s["dc"], s["host_mgmt"], int(s["host_console"]), int(s["host_idx"]))
        text = labconf.add_link(text, s["pe"], s["pe_port"][3:], s["ce"], s["ce_pe_port"][3:], s["attachment_circuit"], spec["name"], comment=f"{spec['name']} {s['dc']} (portal)")
        text = labconf.add_link(text, s["ce"], s["ce_lan_port"][3:], s["host"], 1, s["lan"], spec["name"])
        ces.append(s["ce"]); hosts.append(s["host"])
    labconf.write(text); return ces, hosts


def removal_plan(tenant):
    f = facts()
    if tenant not in f["tenants"]: return [f"unknown tenant {tenant}"], None
    if len(f["tenants"]) <= 1: return ["the last tenant cannot be removed"], None
    sites = tenant_sites(f, tenant)
    return [], {"name": tenant, "table": f["tenants"][tenant]["table"], "rt": f["tenants"][tenant]["rt"], "sites": sites,
                "hosts": [s["host"] for s in sites], "ces": sorted({s["ce"] for s in sites}), "pes": sorted({s["pe"] for s in sites})}


def remove_from_labconf(tenant):
    text = labconf.read(); f = facts()
    for s in tenant_sites(f, tenant): text = labconf.remove_host(text, s["host"])
    text = labconf.remove_links(text, tenant); text = labconf.remove_tenant(text, tenant); labconf.write(text)
