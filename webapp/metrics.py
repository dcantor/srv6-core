"""Prometheus for the portal: /metrics (what exporters cannot know: tenant health per site, host reachability, IS-IS / BFD
adjacency counts, steering policies, run outcomes) and /api/sd (HTTP service discovery: every node-exporter,
frr-exporter and host exporter of the lab, plus the portal itself, labelled with node / role / dc / tenant)."""
import os, subprocess, threading, time
from labportal import metric_line as line, run_metrics, exposition

LAB_HOST = os.environ.get("LAB_HOST_IP", "10.3.0.1")   # the lab host as the NMS sees it (OOB bridge address)
PORTAL_PORT = int(os.environ.get("WEBAPP_PORT", "8091"))


def render(st, runs):
    """Prometheus text exposition from the portal's state (model + live) and the run history."""
    out = ["# HELP lab_state_generated_seconds When the state below was collected", "# TYPE lab_state_generated_seconds gauge", line("lab_state_generated_seconds", {"lab": "srv6-core"}, int(st["generated"]))]
    hl = st.get("hosts_live") or {}; N = {n["name"]: n for n in st["inv"]["nodes"]}
    out += ["# HELP lab_host_reachable 1 if the tenant host answers SSH over the OOB network", "# TYPE lab_host_reachable gauge"]
    for t in st["tenants"]:
        for s in t["sites"]:
            h = hl.get(s["host"]) if s["host"] else None
            if h is not None: out.append(line("lab_host_reachable", {"lab": "srv6-core", "host": s["host"], "tenant": t["name"], "dc": s["dc"]}, int(bool(h.get("reachable")))))
    out += ["# HELP lab_tenant_site_bgp_up 1 if the PE's eBGP session to the CE for the tenant is Established", "# TYPE lab_tenant_site_bgp_up gauge",
            "# HELP lab_tenant_vrf_routes IPv4 routes in the tenant VRF on the PE", "# TYPE lab_tenant_vrf_routes gauge",
            "# HELP lab_tenant_srv6_routes SRv6-encapsulated routes in the tenant VRF on the PE", "# TYPE lab_tenant_srv6_routes gauge"]
    for t in st["tenants"]:
        for s in t["sites"]:
            l = s.get("live") or {}
            if "bgp" in l:
                lab = {"lab": "srv6-core", "tenant": t["name"], "dc": s["dc"], "pe": s["pe"], "ce": s["ce"], "external": s.get("external") or ""}
                out.append(line("lab_tenant_site_bgp_up", lab, int(l.get("bgp") == "Established")))
                if l.get("vrf_routes") is not None: out.append(line("lab_tenant_vrf_routes", lab, l["vrf_routes"]))
                if l.get("srv6_routes") is not None: out.append(line("lab_tenant_srv6_routes", lab, l["srv6_routes"]))
    out += ["# HELP lab_tenant_health 2 = up (every site BGP up and host reachable), 1 = degraded, 0 = down", "# TYPE lab_tenant_health gauge"]
    for t in st["tenants"]:
        if t.get("health"): out.append(line("lab_tenant_health", {"lab": "srv6-core", "tenant": t["name"], "sites": len(t["sites"])}, {"up": 2, "degraded": 1, "down": 0}[t["health"]]))
    out += ["# HELP lab_isis_adjacencies_up IS-IS level-2 adjacencies in state Up on a core node", "# TYPE lab_isis_adjacencies_up gauge",
            "# HELP lab_isis_adjacencies_expected core links of the node (what should be Up)", "# TYPE lab_isis_adjacencies_expected gauge",
            "# HELP lab_bfd_sessions_up BFD sessions up on a core node", "# TYPE lab_bfd_sessions_up gauge"]
    for name, c in (st.get("core_live") or {}).items():
        if "isis_up" in c:
            lab = {"lab": "srv6-core", "node": name, "role": N[name]["role"]}
            out += [line("lab_isis_adjacencies_up", lab, c["isis_up"]), line("lab_isis_adjacencies_expected", lab, c["core_links"]), line("lab_bfd_sessions_up", lab, c["bfd_up"])]
    out += ["# HELP lab_vpnv4_session_up 1 if the PE's VPNv4 session to a route reflector is Established", "# TYPE lab_vpnv4_session_up gauge"]
    for pe, live in (st.get("pes_live") or {}).items():
        for peer, v in (live.get("vpnv4") or {}).items(): out.append(line("lab_vpnv4_session_up", {"lab": "srv6-core", "pe": pe, "reflector": peer}, int(v.get("state") == "Established")))
    out += ["# HELP lab_steering_policies explicit-path SRv6 policies present", "# TYPE lab_steering_policies gauge", line("lab_steering_policies", {"lab": "srv6-core"}, len([p for p in st.get("steering") or [] if "prefix" in p]))]
    out += ["# HELP lab_vm_running 1 if the lab VM is running (virsh)", "# TYPE lab_vm_running gauge"]
    try: running = set(subprocess.run(["sg", "libvirt", "-c", "virsh list --name"], capture_output=True, text=True, timeout=20).stdout.split())
    except Exception: running = set()  # noqa: BLE001
    for n in st["inv"]["nodes"]:
        if n["role"] != "ext-ce": out.append(line("lab_vm_running", {"lab": "srv6-core", "node": n["name"], "role": n["role"]}, int(n["name"] in running)))
    out += run_metrics("srv6-core", runs)
    return exposition(out)


def targets(st):
    """Prometheus http_sd: one target group per exporter, labelled for the dashboards."""
    groups = []
    for n in st["inv"]["nodes"]:
        if n["role"] == "ext-ce": continue   # another lab's router (IOS-XE: no exporter)
        base = {"lab": "srv6-core", "node": n["name"], "role": n["role"], "dc": n["dc"]}
        if n["role"] == "host":
            t = next((p["tenant"] for p in n["ports"] if p["peer"]), None); groups.append({"targets": [f"{n['mgmt_ip']}:9100"], "labels": {**base, "job": "node", "tenant": t or ""}})
        else:
            groups.append({"targets": [f"{n['mgmt_ip']}:9100"], "labels": {**base, "job": "node"}})
            groups.append({"targets": [f"{n['mgmt_ip']}:9342"], "labels": {**base, "job": "frr"}})
    groups.append({"targets": [f"{LAB_HOST}:{PORTAL_PORT}"], "labels": {"lab": "srv6-core", "job": "portal", "role": "portal"}})
    return groups


class Collector:
    """Refreshes the live state in the background so a scrape answers from the cache instantly (a live refresh takes ~40 s:
    SSH to 11 VyOS nodes and 8 hosts). Started by the app; the first scrape before the first refresh only carries the model."""

    def __init__(self, state, interval=60):
        self.state, self.interval = state, interval; self.last = None; self.error = None
        threading.Thread(target=self._loop, daemon=True, name="metrics-collector").start()

    def _loop(self):
        while True:
            try:
                self.state.get(refresh=True, live=True); self.last = time.time(); self.error = None
            except Exception as e:  # noqa: BLE001
                self.error = f"{e.__class__.__name__}: {e}"
            time.sleep(self.interval)

    def snapshot(self):
        st = self.state._cache if self.state._cache else self.state.get(live=False)
        return st
