"""Tenant SLA probes: delay and loss between every pair of a tenant's sites, measured from the tenant's own hosts.

Every PROBE_S seconds each host pings the host of every other site of its tenant — PINGS echo requests 0.2 s apart,
all peers in parallel, over one SSH session per host — so a probe crosses exactly what the tenant's traffic crosses:
LAN, CE, attachment circuit, ingress PE, the SRv6 core, egress PE, CE, LAN. The results are kept for HISTORY_S in
memory (for the portal's charts), exported on /metrics (lab_tenant_rtt_ms, lab_tenant_loss_ratio, lab_tenant_sla_ok —
Prometheus / VictoriaMetrics keep them, the alert rules in lab-portal fire on them) and compared with the targets:

    ok        average RTT < RTT_WARN_MS and loss < LOSS_WARN
    warning   above a warning threshold          critical   RTT ≥ RTT_CRIT_MS or loss ≥ LOSS_CRIT, or no answer at all

A pair that changes state writes a Grafana annotation, so a breach and its recovery sit on every dashboard."""
import os, re, threading, time
from collections import deque

PROBE_S = int(os.environ.get("SLA_PROBE_S", "60"))
PINGS = 10
HISTORY_S = 24 * 3600
TARGETS = {"rtt_warn_ms": float(os.environ.get("SLA_RTT_WARN_MS", "10")), "rtt_crit_ms": float(os.environ.get("SLA_RTT_CRIT_MS", "25")),
           "loss_warn": float(os.environ.get("SLA_LOSS_WARN", "0.01")), "loss_crit": float(os.environ.get("SLA_LOSS_CRIT", "0.05"))}
_COUNT = re.compile(r"(\d+) packets transmitted, (\d+) (?:packets )?received")
_RTT = re.compile(r"= ([\d.]+)/([\d.]+)/([\d.]+)")                  # min/avg/max (iputils adds /mdev, BusyBox does not)


def parse_ping(text):
    """(rtt avg ms | None, rtt min, rtt max, loss 0..1) from ping -q's summary, BusyBox or iputils."""
    c, r = _COUNT.search(text), _RTT.search(text)
    sent, got = (int(c[1]), int(c[2])) if c else (0, 0)
    return (float(r[2]) if r else None, float(r[1]) if r else None, float(r[3]) if r else None,
            round(1 - got / sent, 4) if sent else 1.0)


def grade(rtt, loss):
    if rtt is None or loss is None or loss >= 1:
        return "critical"
    if rtt >= TARGETS["rtt_crit_ms"] or loss >= TARGETS["loss_crit"]:
        return "critical"
    if rtt >= TARGETS["rtt_warn_ms"] or loss >= TARGETS["loss_warn"]:
        return "warning"
    return "ok"


def pairs(inv):
    """[(tenant, src host node, dst host node)] for every ordered pair of a tenant's sites (hosts on one tenant LAN each)."""
    hosts = [n for n in inv["nodes"] if n["role"] == "host"]
    by_t = {}
    for h in hosts:
        t = (h["ports"][0] if h.get("ports") else {}).get("tenant")
        if t:
            by_t.setdefault(t, []).append(h)
    return [(t, a, b) for t, hs in sorted(by_t.items()) for a in sorted(hs, key=lambda x: x["name"])
            for b in sorted(hs, key=lambda x: x["name"]) if a is not b]


class Prober:
    def __init__(self, inventory, run_on_host, annotate=None):
        self.inventory, self.run_on_host, self.annotate = inventory, run_on_host, annotate
        self.lock = threading.Lock()
        self.latest, self.hist, self.last_run, self.errors = {}, {}, 0.0, {}

    @staticmethod
    def key(t, a, b):
        return f"{t}|{a}|{b}"

    def probe_host(self, src, peers):
        """One SSH session: ping every peer in parallel; {peer name: (rtt avg ms | None, loss 0..1)}."""
        cmd = " ".join(f"( r=$(ping -c {PINGS} -i 0.2 -W 1 -q {p['ports'][0]['ip'].split('/')[0]} 2>&1 | tail -2 | tr '\\n' ' '); "
                       f"echo \"@{p['name']} $r\" ) &" for p in peers) + " wait"
        rc, text = self.run_on_host(src["mgmt_ip"], cmd, 60)
        out = {}
        for line in text.splitlines():
            m = re.match(r"@(\S+) (.*)$", line)
            if not m:
                continue
            avg, _, _, loss = parse_ping(m[2])
            out[m[1]] = (avg, loss)
        return out

    def run_once(self):
        from concurrent.futures import ThreadPoolExecutor
        inv = self.inventory()
        todo = {}
        for t, a, b in pairs(inv):
            todo.setdefault(a["name"], (a, []))[1].append(b)
        now = time.time()

        def one(item):
            name, (src, peers) = item
            try:
                return name, self.probe_host(src, peers), None
            except Exception as e:                                   # noqa: BLE001 — an unreachable host is a result
                return name, {p["name"]: (None, 1.0) for p in peers}, f"{e.__class__.__name__}: {e}"

        with ThreadPoolExecutor(max_workers=8) as ex:
            res = list(ex.map(one, todo.items()))
        tenant_of = {h["name"]: (h["ports"][0] or {}).get("tenant") for h in inv["nodes"] if h["role"] == "host" and h.get("ports")}
        dc_of = {h["name"]: h.get("dc") for h in inv["nodes"]}
        changes = []
        with self.lock:
            self.errors = {n: e for n, _, e in res if e}
            seen = set()
            for src, got, _ in res:
                for dst, (rtt, loss) in got.items():
                    t = tenant_of.get(src); k = self.key(t, src, dst); seen.add(k)
                    st = grade(rtt, loss)
                    prev = self.latest.get(k, {}).get("state")
                    self.latest[k] = {"tenant": t, "src": src, "dst": dst, "src_dc": dc_of.get(src), "dst_dc": dc_of.get(dst),
                                      "rtt_ms": rtt, "loss": loss, "state": st, "ts": now}
                    h = self.hist.setdefault(k, deque())
                    h.append((now, rtt, loss))
                    while h and h[0][0] < now - HISTORY_S:
                        h.popleft()
                    if prev and prev != st:
                        changes.append((t, src, dst, prev, st, rtt, loss))
            for k in list(self.latest):                              # a pair whose host is gone (tenant removed)
                if k not in seen:
                    self.latest.pop(k); self.hist.pop(k, None)
            self.last_run = now
        for t, src, dst, prev, st, rtt, loss in changes:
            if self.annotate:
                self.annotate(f"srv6-core: SLA {t} {src} → {dst} {prev} → {st} (rtt {rtt} ms, loss {round((loss or 0) * 100, 1)} %)",
                              ["srv6-core", "sla", t])

    def loop(self, running):
        time.sleep(15)
        while True:
            try:
                if running():
                    self.run_once()
            except Exception:                                         # noqa: BLE001 — never let the prober die
                pass
            time.sleep(PROBE_S)

    # ---- reading -------------------------------------------------------------------------------------------------
    def summary(self):
        with self.lock:
            rows = sorted(self.latest.values(), key=lambda r: (r["tenant"], r["src"], r["dst"]))
            errors, last = dict(self.errors), self.last_run
        tenants = {}
        for r in rows:
            t = tenants.setdefault(r["tenant"], {"pairs": 0, "ok": 0, "warning": 0, "critical": 0, "worst_rtt_ms": None, "worst_loss": 0})
            t["pairs"] += 1; t[r["state"]] += 1
            if r["rtt_ms"] is not None and (t["worst_rtt_ms"] is None or r["rtt_ms"] > t["worst_rtt_ms"]):
                t["worst_rtt_ms"] = r["rtt_ms"]
            t["worst_loss"] = max(t["worst_loss"], r["loss"] or 0)
        return {"targets": TARGETS, "probe_s": PROBE_S, "pings": PINGS, "last_run": last, "pairs": rows, "tenants": tenants, "errors": errors}

    def history(self, tenant, src, dst, since=None):
        with self.lock:
            h = list(self.hist.get(self.key(tenant, src, dst), ()))
        since = since or (time.time() - HISTORY_S)
        return [{"ts": ts, "rtt_ms": rtt, "loss": loss} for ts, rtt, loss in h if ts >= since]

    def metrics(self, line):
        with self.lock:
            rows = list(self.latest.values())
        out = []
        for name, help_ in (("lab_tenant_rtt_ms", "Average round-trip time between two sites of a tenant, host to host (ms)"),
                            ("lab_tenant_loss_ratio", "Share of the probe's echo requests lost between two sites of a tenant (0-1)"),
                            ("lab_tenant_sla_ok", "1 if the pair meets its SLA targets (RTT and loss below the warning thresholds)")):
            out += [f"# HELP {name} {help_}", f"# TYPE {name} gauge"]
            for r in rows:
                lab = {"lab": "srv6-core", "tenant": r["tenant"], "src": r["src"], "dst": r["dst"], "src_dc": r["src_dc"], "dst_dc": r["dst_dc"]}
                v = {"lab_tenant_rtt_ms": r["rtt_ms"], "lab_tenant_loss_ratio": r["loss"], "lab_tenant_sla_ok": int(r["state"] == "ok")}[name]
                if v is not None:
                    out.append(line(name, lab, v))
        return "\n".join(out) + ("\n" if out else "")
