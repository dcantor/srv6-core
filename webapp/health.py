"""A control-plane health score per router: 100, minus a deduction for everything that is wrong now or went wrong
recently — each one listed with its reason, so the number is never a black box.

    now (Prometheus)        an IS-IS adjacency missing (the portal's own count against the model)    -25 each
                            a BGP session not Established (frr-exporter)                             -20 each
                            a BGP session shut down by configuration (deliberate, but carrying nothing)  -10 each
                            a BFD session down (frr-exporter)                                        -15 each
                            CPU, 5-minute average (node-exporter)                     >= 75 %: -5,  >= 90 %: -15
                            memory in use                                                  >= 90 %: -10
    the window (syslog)     a BGP session dropped (FRR's %ADJCHANGE ... Down)                -5 each, at most -30
                            an IS-IS adjacency lost (%ADJCHANGE ... changed from Up to)      -5 each, at most -30
                            — but not while a test run was going (the results folders say when): the suites cut links
                            and restart daemons on purpose, so those are counted apart and cost nothing
                            commits (one "Configuration Read" per commit) and, on a reflector, BMP reconnects: shown,
                            not deducted — a change is not a fault, but it is the first thing to look at when one follows

90 and above is healthy, 70 and above degraded, below that critical. FRR logs these state changes at informational,
which tools/frr_logging.py turns on; the syslog arrives in VictoriaLogs with the router's host name."""
import datetime, json, time
from pathlib import Path

import traffic as TF                                  # the same Prometheus / VictoriaLogs endpoints and helpers

WINDOWS = {"1h": 3600, "6h": 21600, "24h": 86400}
ROLES = ("p", "pe", "ce", "fw")


def _by_node(results, key="node", value=float):
    out = {}
    for r in results:
        n = r["metric"].get(key)
        if n: out.setdefault(n, []).append((r["metric"], value(r["value"][1])))
    return out


RESULTS = Path(__file__).resolve().parents[1] / "results"


def test_windows(since):
    """(start, end) of every test run since `since`: a results folder is named by its start, and its output is written
    last. The suites cut links, shut sessions and restart daemons on purpose — what they break is not a fault."""
    out = []
    for d in RESULTS.glob("20??-??-??_??-??-??"):
        try: start = datetime.datetime.strptime(d.name, "%Y-%m-%d_%H-%M-%S").timestamp()
        except ValueError: continue
        files = [f.stat().st_mtime for f in d.rglob("*") if f.is_file()]
        end = max(files) if files else start + 7200
        if end >= since: out.append((start, end))
    return out


def _events(query, hosts, windows):
    """{host: (outside test runs, during them)} for one kind of event."""
    rows = TF.logsql(query + " | fields _time, hostname | limit 5000")
    out = {}
    for r in rows:
        h = r.get("hostname")
        if h not in hosts: continue
        t = datetime.datetime.fromisoformat(r["_time"][:19] + "+00:00").timestamp()
        during = any(a - 60 <= t <= b + 60 for a, b in windows)
        o, d = out.get(h, (0, 0)); out[h] = (o + (not during), d + during)
    return out


def _count(query, hosts):
    rows = TF.logsql(query + " | stats by (hostname) count() n")
    return {r["hostname"]: int(r["n"]) for r in rows if r.get("hostname") in hosts}


def grade(score):
    return "healthy" if score >= 90 else "degraded" if score >= 70 else "critical"


def collect(inv, window="1h"):
    t0 = time.time(); lab = inv["lab"]
    nodes = [n for n in inv["nodes"] if n["role"] in ROLES]
    hosts = {n["name"] for n in nodes}
    hl = ",".join(json.dumps(h) for h in sorted(hosts))
    adj_up = {k: v[0][1] for k, v in _by_node(TF.prom("lab_isis_adjacencies_up")).items()}
    adj_exp = {k: v[0][1] for k, v in _by_node(TF.prom("lab_isis_adjacencies_expected")).items()}
    bgp = _by_node(TF.prom(f'frr_bgp_peer_state{{lab="{lab}"}}'))
    bfd = _by_node(TF.prom(f'frr_bfd_peer_state{{lab="{lab}"}}'))
    cpu = {k: v[0][1] for k, v in _by_node(TF.prom(f'100*(1-avg by (node)(rate(node_cpu_seconds_total{{lab="{lab}",mode="idle"}}[5m])))')).items()}
    mem = {k: v[0][1] for k, v in _by_node(TF.prom(f'100*(1-node_memory_MemAvailable_bytes{{lab="{lab}"}}/node_memory_MemTotal_bytes)')).items()}
    w = f"_time:{window} hostname:in({hl})"
    tests = test_windows(time.time() - WINDOWS.get(window, 3600))
    bgp_down = _events(f'{w} app_name:bgpd "%ADJCHANGE" "Down"', hosts, tests)
    isis_lost = _events(f'{w} app_name:isisd "%ADJCHANGE" "changed from Up to"', hosts, tests)
    commits = _count(f'{w} app_name:bgpd "Configuration Read in"', hosts)
    bmp = _count(f'{w} app_name:bgpd "bmp[" "connect in progress"', hosts)
    out = []
    for n in nodes:
        name, ded = n["name"], []
        if name in adj_exp and adj_up.get(name, 0) < adj_exp[name]:
            k = int(adj_exp[name] - adj_up.get(name, 0))
            ded.append({"points": 25 * k, "what": f"{k} of {int(adj_exp[name])} IS-IS adjacencies missing", "now": True})
        # frr-exporter: 1 = Established, 0 = down, 2 = administratively down. One session per peer and VRF, whatever
        # its address families: the worst of them counts (down, then shut, then up)
        rank = {1: 0, 2: 1, 0: 2}; peers = {}
        for m, v in bgp.get(name, []):
            k = (m.get("vrf"), m.get("peer")); v = int(v)
            if k not in peers or rank.get(v, 2) > rank.get(peers[k], 2): peers[k] = v
        label = lambda vrf, p: f"{p} ({vrf})" if vrf != "default" else p
        down = sorted(label(vrf, p) for (vrf, p), v in peers.items() if v not in (1, 2))
        shut = sorted(label(vrf, p) for (vrf, p), v in peers.items() if v == 2)
        if down: ded.append({"points": 20 * len(down), "what": f"BGP not Established: {', '.join(down)}", "now": True})
        if shut: ded.append({"points": 10 * len(shut), "what": f"BGP shut down by configuration: {', '.join(shut)}", "now": True})
        bdown = [m.get("peer") for m, v in bfd.get(name, []) if v != 1]
        if bdown: ded.append({"points": 15 * len(bdown), "what": f"BFD down: {', '.join(bdown)}", "now": True})
        c = cpu.get(name)
        if c is not None and c >= 75: ded.append({"points": 15 if c >= 90 else 5, "what": f"CPU {c:.0f} % (5-minute average)", "now": True})
        mm = mem.get(name)
        if mm is not None and mm >= 90: ded.append({"points": 10, "what": f"memory {mm:.0f} % in use", "now": True})
        b_out, b_in = bgp_down.get(name, (0, 0)); i_out, i_in = isis_lost.get(name, (0, 0))
        if b_out: ded.append({"points": min(30, 5 * b_out), "what": f"{b_out} BGP session drop(s) in the last {window}, outside test runs"})
        if i_out: ded.append({"points": min(30, 5 * i_out), "what": f"{i_out} IS-IS adjacency loss(es) in the last {window}, outside test runs"})
        score = max(0, 100 - sum(d["points"] for d in ded))
        info = []
        if b_in or i_in: info.append(f"{b_in} BGP drop(s) and {i_in} IS-IS loss(es) during test runs — the suites break things on purpose")
        if commits.get(name): info.append(f"{commits[name]} commit(s) in the last {window}")
        if bmp.get(name): info.append(f"{bmp[name]} BMP (re)connect attempt(s) in the last {window}")
        out.append({"node": name, "role": n["role"], "dc": n.get("dc"), "score": score, "grade": grade(score), "deductions": ded,
                    "info": info, "sessions": {"bgp": len(peers), "bgp_down": len(down) + len(shut), "bfd": len(bfd.get(name, [])),
                    "bfd_down": len(bdown), "isis": adj_up.get(name), "isis_expected": adj_exp.get(name)},
                    "cpu": round(c, 1) if c is not None else None, "memory": round(mm, 1) if mm is not None else None,
                    "commits": commits.get(name, 0)})
    out.sort(key=lambda x: (x["score"], x["node"]))
    role = {n["name"]: n["role"] for n in inv["nodes"]}
    links = [{"a": l["a"], "b": l["b"]} for l in inv["links"] if role.get(l["a"]) in ("p", "pe") and role.get(l["b"]) in ("p", "pe")]
    return {"window": window, "routers": out, "test_runs": [{"start": a, "end": b} for a, b in tests], "links": links, "took": round(time.time() - t0, 2), "as_of": time.time(),
            "nodes": [{"name": n["name"], "role": n["role"], "dc": n.get("dc")} for n in inv["nodes"] if n["role"] in ("p", "pe")]}


def events(node, window="1h", limit=40):
    """The routing-state syslog of one router, newest first: what lies behind its score."""
    q = (f'_time:{window} hostname:{json.dumps(node)} app_name:in(bgpd,isisd,bfdd) '
         f'("%ADJCHANGE" OR "%NOTIFICATION" OR "Configuration Read in" OR "connect in progress") | sort by (_time desc) | limit {int(limit)}')
    rows = [r for r in TF.logsql(q) if "Configuration Read" not in (r.get("_msg") or "") or r.get("app_name") == "bgpd"]   # one line per commit, not one per daemon
    tests = test_windows(time.time() - WINDOWS.get(window, 3600))
    out = []
    for r in rows:
        t = datetime.datetime.fromisoformat(r["_time"][:19] + "+00:00").timestamp()
        out.append({"time": r.get("_time"), "daemon": r.get("app_name"), "text": r.get("_msg"),
                    "during_test_run": any(a - 60 <= t <= b + 60 for a, b in tests)})
    return out
