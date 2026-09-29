"""Steering on the map: for every explicit-path policy (or one being planned), the path it takes, the IGP shortest path it
replaces, and the delay of each, measured.

Paths come from the model: the core graph is the links between P routers and PEs (every IS-IS metric in this lab is the
default, so the IGP's choice is the fewest hops; equal-cost alternatives are all returned). A policy's segment list names
its waypoints — a uSID carrier is unpacked into the routers it names, as the looking glass does — and each segment is
reached along the IGP between waypoints, so the steered path is the shortest path src → wp1 → … → egress PE.

Delay is measured from the source PE, inside the tenant's VRF, ten echo requests 0.2 s apart:
    steered   to the host on the steered prefix (the policy's own traffic)
    IGP       to the same site's CE, on its attachment circuit — the same egress PE, a prefix no policy steers
so the two differ by the path through the core, not by where they end."""
import ipaddress, re, sys
from collections import deque
from pathlib import Path

import tenants as T

LAB = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(LAB / "lg"))
import paths as lgpaths                                          # noqa: E402 — the looking glass's segment decoder

CORE = {"p", "pe"}
_COUNT = re.compile(r"(\d+) packets transmitted, (\d+) (?:packets )?received")
_RTT = re.compile(r"= ([\d.]+)/([\d.]+)/([\d.]+)")                  # min/avg/max (iputils adds /mdev, BusyBox does not)


def parse_ping(text):
    """(rtt avg ms | None, rtt min, rtt max, loss 0..1) from ping -q's summary, BusyBox or iputils."""
    c, r = _COUNT.search(text), _RTT.search(text)
    sent, got = (int(c[1]), int(c[2])) if c else (0, 0)
    return (float(r[2]) if r else None, float(r[1]) if r else None, float(r[3]) if r else None,
            round(1 - got / sent, 4) if sent else 1.0)


def graph(inv):
    role = {n["name"]: n["role"] for n in inv["nodes"]}
    g = {n: set() for n, r in role.items() if r in CORE}
    for l in inv["links"]:
        if role.get(l["a"]) in CORE and role.get(l["b"]) in CORE:
            g[l["a"]].add(l["b"]); g[l["b"]].add(l["a"])
    return g


def shortest(g, a, b, limit=6):
    """Every shortest path a → b (fewest hops), at most `limit` of them."""
    if a == b:
        return [[a]]
    dist, q = {a: 0}, deque([a])
    while q:
        x = q.popleft()
        for y in sorted(g.get(x, ())):
            if y not in dist:
                dist[y] = dist[x] + 1; q.append(y)
    if b not in dist:
        return []
    out = []

    def walk(x, acc):
        if len(out) >= limit:
            return
        if x == a:
            out.append(list(reversed(acc))); return
        for y in sorted(g.get(x, ())):
            if dist.get(y) == dist[x] - 1:
                walk(y, acc + [y])
    walk(b, [b])
    return out


def through(g, waypoints):
    """The path along the IGP between consecutive waypoints (the first shortest path of each leg)."""
    path = [waypoints[0]]
    for a, b in zip(waypoints, waypoints[1:]):
        leg = shortest(g, a, b, 1)
        if not leg:
            return None
        path += leg[0][1:]
    return path


def owner(inv, tenant, prefix):
    """The site a tenant prefix lives at: {pe, ce, host, host_ip, ce_ip (on its attachment circuit), dc}."""
    f = T.facts(inv)
    net = ipaddress.ip_network(prefix)
    for s in T.tenant_sites(f, tenant):
        if s.get("lan") and ipaddress.ip_network(s["lan"]).overlaps(net):
            ac = next(l for l in inv["links"] if l.get("tenant") == tenant and l["prefix"] == s["attachment_circuit"])
            return {"pe": s["pe"], "ce": s["ce"], "dc": s["dc"], "host": s.get("host"), "host_ip": s.get("host_ip"),
                    "ce_ip": (ac["b_ip"] if ac["b"] == s["ce"] else ac["a_ip"]).split("/")[0]}
    return None


def paths_for(inv, pe, tenant, prefix, segments=None, via=None):
    """{igp: [paths], steered: path, waypoints, egress, hops_igp, hops_steered} for a policy (segments) or a plan (via)."""
    g = graph(inv)
    o = owner(inv, tenant, prefix)
    if not o:
        raise ValueError(f"{prefix} is not a site LAN of {tenant}")
    if segments:
        cfg = {"service": inv["service"], "nodes": {n["name"]: n for n in inv["nodes"]}}
        named = lgpaths.segment_nodes(segments, cfg)
        role = {n["name"]: n["role"] for n in inv["nodes"]}
        via = [n for n in named if role.get(n) == "p"]
    via = list(via or [])
    unknown = [v for v in via if v not in g or not v.startswith("p") or v == pe]
    if unknown:
        raise ValueError(f"not P routers of the core: {', '.join(unknown)}")
    igp = shortest(g, pe, o["pe"])
    steered = through(g, [pe] + via + [o["pe"]]) if via else None
    return {"pe": pe, "tenant": tenant, "prefix": prefix, "egress": o["pe"], "owner": o, "via": via, "igp": igp,
            "steered": steered, "hops_igp": len(igp[0]) - 1 if igp else None,
            "hops_steered": len(steered) - 1 if steered else None}


def measure(inv, pe, tenant, prefix, run):
    """Ping from the PE in the tenant's VRF: the steered prefix's host, and the same site's CE (not steered).
    `run(node, command, timeout)` runs a shell command on a router. Returns {steered: {...}, igp: {...}}."""
    o = owner(inv, tenant, prefix)
    if not o:
        raise ValueError(f"{prefix} is not a site LAN of {tenant}")
    if not re.fullmatch(r"[\w-]{1,40}", tenant):
        raise ValueError("bad tenant name")
    targets = {"steered": o["host_ip"], "igp": o["ce_ip"]}
    cmd = " ".join(f"( r=$(sudo ip vrf exec {tenant} ping -c 10 -i 0.2 -W 1 -q {ip} 2>&1 | tail -2 | tr '\\n' ' '); echo \"@{k} $r\" ) &"
                   for k, ip in targets.items() if ip) + " wait"
    text = run(pe, cmd, 60)
    out = {}
    for line in text.splitlines():
        m = re.match(r"@(\S+) (.*)$", line)
        if not m:
            continue
        avg, lo, hi, loss = parse_ping(m[2])
        out[m[1]] = {"target": targets[m[1]], "rtt_ms": avg, "rtt_min_ms": lo, "rtt_max_ms": hi, "loss": loss}
    return out
