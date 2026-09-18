"""What the portal shows: the lab as lab.conf describes it, joined with live state from the PEs (per-tenant eBGP
sessions and VRF routes, VPNv4 sessions to the reflectors), host reachability, steering policies, and Nautobot links."""
import concurrent.futures, ipaddress, os, re, subprocess, threading, time
from pathlib import Path
import paramiko
from netmiko import ConnectHandler
import tenants as T

LAB = Path(__file__).resolve().parents[1]
VYOS = dict(username=os.environ.get("VYOS_USERNAME", "vyos"), password=os.environ.get("VYOS_PASSWORD", "vyos"))
NAUTOBOT_PUBLIC_URL = os.environ.get("NAUTOBOT_PUBLIC_URL", "http://192.168.50.231:8080")


class State:
    def __init__(self, ttl=30):
        self.ttl, self._cache, self._lock = ttl, None, threading.Lock()

    def model(self):
        """Tenants, sites and hosts from lab.conf (no device access)."""
        f = T.facts(); inv = f["inv"]
        tenants = []
        for t in f["order"]:
            v = f["tenants"][t]; sites = T.tenant_sites(f, t)
            tenants.append({"name": t, "table": v["table"], "rt": v["rt"], "sites": sites, "hosts": [s["host"] for s in sites],
                            "nautobot": {"vrf": f"{NAUTOBOT_PUBLIC_URL}/ipam/vrfs/?q={t}", "tenant": f"{NAUTOBOT_PUBLIC_URL}/tenancy/tenants/?q={t}", "prefixes": f"{NAUTOBOT_PUBLIC_URL}/ipam/prefixes/?tenant={t}"}})
        return {"inv": inv, "tenants": tenants, "dcs": f["dcs"], "pes": sorted(n["name"] for n in inv["nodes"] if n["role"] == "pe"),
                "nautobot_url": NAUTOBOT_PUBLIC_URL, "generated": time.time()}

    def pe_state(self, pe, tenants_):
        """Live: per tenant the eBGP session to the CE (state / prefixes) and the VRF route count; the VPNv4 sessions."""
        c = ConnectHandler(device_type="vyos", host=pe["mgmt_ip"], **VYOS); out = {"tenants": {}, "vpnv4": {}}
        try:
            summ = c.send_command("show bgp ipv4 vpn summary", read_timeout=60)
            for line in summ.splitlines():
                m = re.match(r"^(fd00:\S+)\s+4\s+\d+\s+\d+\s+\d+\s+\d+\s+\d+\s+\d+\s+(\S+)\s+(\S+)\s+(\d+)\s+(.*)$", line)
                if m: out["vpnv4"][m[1]] = {"state": "Established" if m[3].isdigit() else m[3], "prefixes": int(m[3]) if m[3].isdigit() else 0, "uptime": m[2], "desc": m[5].strip()}
            for t in tenants_:
                s = c.send_command(f"show bgp vrf {t} summary", read_timeout=60); sess = {}   # both families: IPv4 and IPv6 unicast sessions
                for line in s.splitlines():
                    m = re.match(r"^([0-9a-f.:]+)\s+4\s+(\d+)\s+\d+\s+\d+\s+\d+\s+\d+\s+\d+\s+(\S+)\s+(\S+)\s+(\d+)\s*(.*)$", line)
                    if m: sess[m[1]] = {"as": int(m[2]), "state": "Established" if m[4].isdigit() else m[4], "prefixes": int(m[4]) if m[4].isdigit() else 0, "uptime": m[3], "desc": m[6].strip()}
                rt = c.send_command(f"sudo ip -c=never route show vrf {t}", read_timeout=60)
                out["tenants"][t] = {"sessions": sess, "routes": len([l for l in rt.splitlines() if re.match(r"^\d", l) and not l.startswith("127.")]),
                                     "srv6_routes": len([l for l in rt.splitlines() if "encap seg6" in l and re.match(r"^\d", l)]),
                                     "steered": [l.strip() for l in rt.splitlines() if "proto static" in l and "seg6" in l]}
            sids = c.send_command("sudo ip -c=never -6 route show", read_timeout=60)
            out["dt4"] = {m[2]: m[1] for m in re.finditer(r"^(\S+)\s.*action End\.DT4(?:6)? vrftable (\S+)", sids, re.M)}
        finally:
            c.disconnect()
        return out

    def core_state(self, node):
        """Live per core node: IS-IS adjacencies up, BFD sessions up."""
        c = ConnectHandler(device_type="vyos", host=node["mgmt_ip"], **VYOS)
        try:
            isis = c.send_command("show isis neighbor", read_timeout=60); bfd = c.send_command("show bfd peers brief", read_timeout=60)
        finally:
            c.disconnect()
        return {"isis_up": len([l for l in isis.splitlines() if re.search(r"\s2\s+Up\b", l)]), "bfd_up": len([l for l in bfd.splitlines() if re.search(r"^\d+\s+\S+\s+\S+\s+up\b", l)]),
                "core_links": len([p for p in node["ports"] if p["peer"] and (p["peer"].startswith("pe") or p["peer"].startswith("p"))])}

    def host_reachable(self, host):
        c = paramiko.SSHClient(); c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        try:
            c.connect(host["mgmt_ip"], username=os.environ.get("HOST_USERNAME", "lab"), password=os.environ.get("HOST_PASSWORD", "lab"), timeout=20, look_for_keys=False, allow_agent=False)
            _, out, _ = c.exec_command("ip -4 -br addr show eth1; ip route | grep default", timeout=15); txt = out.read().decode(); c.close()
            return {"reachable": True, "detail": " ".join(txt.split())}
        except Exception as e:  # noqa: BLE001
            return {"reachable": False, "detail": e.__class__.__name__}

    def get(self, refresh=False, live=True):
        with self._lock:
            if self._cache and not refresh and (not live or (time.time() - self._cache["generated"] < self.ttl and self._cache.get("live_done"))): return self._cache
            st = self.model(); tnames = [t["name"] for t in st["tenants"]]
            if not live and self._cache and self._cache.get("live_done"): return self._cache   # a model-only request never discards live data (the model changes only through runs, which reset the cache)
            if live:
                pes = [n for n in st["inv"]["nodes"] if n["role"] == "pe"]; hosts = [n for n in st["inv"]["nodes"] if n["role"] == "host"]
                core = [n for n in st["inv"]["nodes"] if n["role"] in ("pe", "p")]
                with concurrent.futures.ThreadPoolExecutor(max_workers=8) as ex:
                    pe_res = dict(zip([p["name"] for p in pes], ex.map(lambda p: self._safe(self.pe_state, p, tnames), pes)))
                    host_res = dict(zip([h["name"] for h in hosts], ex.map(self.host_reachable, hosts)))
                    core_res = dict(zip([n["name"] for n in core], ex.map(lambda n: self._safe(self.core_state, n), core)))
                st["pes_live"] = pe_res; st["hosts_live"] = host_res; st["core_live"] = core_res
                for t in st["tenants"]:
                    for s in t["sites"]:
                        live_pe = pe_res.get(s["pe"]) or {}; tl = (live_pe.get("tenants") or {}).get(t["name"]) or {}
                        ce_wan = s.get("ce_wan_ip") or str(ipaddress.ip_network(s["attachment_circuit"]).network_address + 2); sess = (tl.get("sessions") or {}).get(ce_wan, {})
                        sess6 = (tl.get("sessions") or {}).get(s.get("ce_wan_ip6") or "", {})
                        s["live"] = {"bgp": sess.get("state", "n/a"), "bgp6": sess6.get("state") if s.get("ce_wan_ip6") else None, "prefixes_from_ce": sess.get("prefixes"), "vrf_routes": tl.get("routes"), "srv6_routes": tl.get("srv6_routes"),
                                     "dt4_sid": (live_pe.get("dt4") or {}).get(t["name"]), "host": host_res.get(s["host"], {}) if s["host"] else {"reachable": None}, "error": live_pe.get("error")}
                    ok = [s for s in t["sites"] if s["live"]["bgp"] == "Established" and (s["live"]["host"].get("reachable") or not s["host"])]
                    t["health"] = "up" if len(ok) == len(t["sites"]) else ("degraded" if ok else "down")
                st["live_done"] = True
            st["steering"] = self.steering() if live else []
            self._cache = st; return st

    def _safe(self, fn, *args):
        try: return fn(*args)
        except Exception as e:  # noqa: BLE001
            return {"error": f"{e.__class__.__name__}: {e}", "tenants": {}, "vpnv4": {}}

    def steering(self):
        try:
            out = subprocess.run([str(LAB / "tests" / ".venv" / "bin" / "python"), str(LAB / "tools" / "steer.py"), "show"], capture_output=True, text=True, timeout=120).stdout
        except Exception as e:  # noqa: BLE001
            return [{"error": str(e)}]
        pol, pe = [], None
        for line in out.splitlines():
            if line.endswith(":") or line.endswith("no steering policies"): pe = line.split(":")[0]; continue
            m = re.match(r"\s+(\S+): (\S+) .*segs \d+ \[ ([^\]]+)\] dev (\S+)", line)
            if m: pol.append({"pe": pe, "tenant": m[1], "prefix": m[2], "segments": m[3].split(), "interface": m[4]})
        return pol
