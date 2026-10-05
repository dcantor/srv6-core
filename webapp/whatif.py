"""What-if: fail links or routers *on the model* and see what the tenants would feel. Nothing on the routers changes.

The core graph is steermap's (P routers and PEs; every IS-IS metric is the default, so the IGP takes the fewest hops).
With the chosen links and routers removed, for every pair of a tenant's sites — and from every site to the internet
breakout — the IGP's shortest paths before and after are compared:

    unaffected   no shortest path used a failed element
    ecmp         one did, but an equal-cost path survives: the same length, IS-IS reconverges (~1 s with BFD)
    longer       the traffic reroutes over N more hops
    cut off      no path is left (or a site's own PE is down)

Explicit-path policies are judged as the lab builds them (tools/steer.py): a static route in the tenant VRF pinned to the
first P router's interface, with a segment list naming every waypoint. The core links are UDP tunnels that never lose
carrier, so nothing withdraws that route when its first hop dies — the prefix is black-holed until the policy is
removed; a later waypoint that is down drops the packets at the waypoint before it (its locator is gone from IS-IS); a
failed link *between* waypoints only reroutes that leg along the IGP. The control plane is checked too: with every route
reflector down the PEs keep their VPN routes only until the BGP sessions time out."""
import tenants as T
import steermap as SM

BGP_HOLD = 180          # s: FRR's default hold time (the lab sets no BGP timers and runs no BFD on BGP)


def _key(a, b):
    return "~".join(sorted((a, b)))


def parse_fail(items, inv):
    """`p2` (a router) or `p1~p2` (a link) -> ({nodes}, {link keys}); unknown names are refused."""
    g = SM.graph(inv)
    nodes, links = set(), set()
    for x in items:
        x = x.strip()
        if not x: continue
        if "~" in x:
            a, b = x.split("~", 1)
            if b not in g.get(a, ()): raise ValueError(f"no core link {a} – {b}")
            links.add(_key(a, b))
        elif x in g: nodes.add(x)
        else: raise ValueError(f"not a core router: {x}")
    return nodes, links


def _without(g, nodes, links):
    return {a: {b for b in nbrs if b not in nodes and _key(a, b) not in links} for a, nbrs in g.items() if a not in nodes}


def _uses(path, nodes, links):
    return any(n in nodes for n in path) or any(_key(a, b) in links for a, b in zip(path, path[1:]))


def judge(g, g2, src, dst, nodes, links):
    """One pair of PEs: before / after paths and the verdict."""
    before = SM.shortest(g, src, dst)
    if src in nodes or dst in nodes:
        return {"verdict": "cut", "before": before, "after": [], "why": f"{src if src in nodes else dst} is down"}
    after = SM.shortest(g2, src, dst)
    hb = len(before[0]) - 1 if before else None
    if not after:
        return {"verdict": "cut", "before": before, "after": [], "why": "no path is left through the core"}
    ha = len(after[0]) - 1
    hit = [p for p in before if _uses(p, nodes, links)]
    if not hit: v = "unaffected"
    elif ha == hb: v = "ecmp"
    else: v = "longer"
    return {"verdict": v, "before": before, "after": after, "hops_before": hb, "hops_after": ha, "extra_hops": ha - hb if hb is not None else None}


def policy_fate(g2, p, nodes, links):
    """An explicit-path policy under the failure (see the module docstring)."""
    path, via, src, dst = p.get("steered") or [], p.get("via") or [], p["pe"], p.get("egress")
    if not path or not _uses(path, nodes, links):
        return {"verdict": "unaffected", "path": path}
    if src in nodes or dst in nodes:
        return {"verdict": "cut", "why": f"{src if src in nodes else dst} is down"}
    first = via[0] if via else None
    if first and (first in nodes or _key(src, first) in links):
        return {"verdict": "blackhole", "why": f"the policy pins the route to {src}'s link to {first}, and nothing withdraws it "
                                                f"(the link never loses carrier): remove the policy to fall back to the IGP"}
    down = [w for w in via if w in nodes]
    if down:
        return {"verdict": "blackhole", "why": f"waypoint {down[0]} is down: the packets are dropped where its locator was "
                                                f"expected, until the policy is removed"}
    new = SM.through(g2, [src] + via + [dst])
    if not new:
        return {"verdict": "blackhole", "why": "a leg between waypoints has no path left"}
    return {"verdict": "rerouted", "path": new, "extra_hops": len(new) - len(path),
            "why": "a leg between waypoints follows the IGP around the failure"}


def analyse(inv, fail, policies=()):
    nodes, links = parse_fail(fail, inv)
    g = SM.graph(inv); g2 = _without(g, nodes, links)
    f = T.facts(inv); svc = inv["service"]
    pairs = []
    for t in f["order"]:
        sites = [s for s in T.tenant_sites(f, t) if s.get("pe")]
        for i, a in enumerate(sites):
            for b in sites[i + 1:]:
                pairs.append({"tenant": t, "a": a["dc"], "b": b["dc"], "a_pe": a["pe"], "b_pe": b["pe"],
                              **judge(g, g2, a["pe"], b["pe"], nodes, links)})
    inet = svc.get("internet") or {}
    internet = []
    if inet.get("pe"):
        for t in f["order"]:
            for s in T.tenant_sites(f, t):
                if not s.get("lan"): continue
                internet.append({"tenant": t, "dc": s["dc"], "pe": s["pe"], **judge(g, g2, s["pe"], inet["pe"], nodes, links)})
    pols = []
    for p in policies:
        if p.get("error") or "pe" not in p: continue
        pols.append({k: p.get(k) for k in ("pe", "tenant", "prefix", "via", "egress", "steered")} | {"fate": policy_fate(g2, p, nodes, links)})
    rrs_down = [r for r in svc["rrs"] if r in nodes]
    control = []
    if rrs_down and len(rrs_down) == len(svc["rrs"]):
        control.append({"severity": "critical", "text": f"every route reflector ({', '.join(rrs_down)}) is down: the PEs keep their VPN "
                        f"routes only until the BGP sessions time out (hold time {BGP_HOLD} s), then every tenant pair across "
                        f"sites is cut off, whatever paths the data plane still has. The looking glass loses both BMP feeds."})
    elif rrs_down:
        control.append({"severity": "warning", "text": f"route reflector {', '.join(rrs_down)} is down: every PE still has the VPN routes "
                        f"from {', '.join(r for r in svc['rrs'] if r not in nodes)}, so nothing is lost — but there is no "
                        f"redundancy left, and the looking glass loses that reflector's BMP feed."})
    if inet.get("pe") and inet["pe"] in nodes:
        control.append({"severity": "critical", "text": f"{inet['pe']} carries the internet breakout ({inet.get('fw', 'the firewall')}): "
                        f"no tenant can reach the internet."})
    for n in sorted(nodes):
        if n.startswith("pe"):
            gone = [f"{t} at {s['dc']}" for t in f["order"] for s in T.tenant_sites(f, t) if s.get("pe") == n]
            if gone: control.append({"severity": "critical", "text": f"{n} is down: its sites are cut off from everything — {', '.join(gone)}."})
    if rrs_down and len(rrs_down) == len(svc["rrs"]):
        for p in pairs:
            if p["verdict"] != "cut": p.update(verdict="cut", why=f"no route reflector: VPN routes expire after {BGP_HOLD} s")
        for x in internet:
            if x["verdict"] != "cut": x.update(verdict="cut", why=f"no route reflector: VPN routes expire after {BGP_HOLD} s")
    count = lambda xs: {v: sum(1 for x in xs if x["verdict"] == v) for v in ("unaffected", "ecmp", "longer", "cut")}
    return {"failed": {"nodes": sorted(nodes), "links": sorted(links)}, "pairs": pairs, "internet": internet,
            "policies": pols, "control": control, "summary": {"pairs": count(pairs), "internet": count(internet),
            "policies": {v: sum(1 for p in pols if p["fate"]["verdict"] == v) for v in ("unaffected", "rerouted", "blackhole", "cut")}}}
