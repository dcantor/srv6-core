#!/usr/bin/env python3
"""The looking glass's HTTP layer: a JSON API over the store, a Prometheus exposition, and the single page that uses them.

Everything the UI shows comes from these endpoints, so anything the page can do can be scripted:

  GET  /api/status                       collector, sessions, counts, poll health, database
  GET  /api/meta                         the model behind the filters: VRFs, RDs, sources, address families, nodes
  GET  /api/prefixes?...                 the current table (source, afi, safi, vrf, rd, origin_as, q, best_only, ...)
  GET  /api/prefix?prefix=1.2.3.0/24     every path for one prefix, in every view, with its history
  GET  /api/history?prefix=&since=       announce / change / withdraw events, newest first
  GET  /api/state?at=<epoch>&prefix=     the table as it stood at a moment (the event log replayed)
  GET  /api/series?metric=paths&...      the numeric series behind the charts
  GET  /api/peers                        the collector's BGP sessions and every device poll
  POST /api/query {device, command}      a live `show` on a router — the classic looking-glass button
  GET  /metrics                          Prometheus exposition of the same numbers
"""
import json, re, subprocess, time
from pathlib import Path
from flask import Flask, Response, jsonify, request, send_from_directory

STATIC = Path(__file__).resolve().parent / "static"
SHOW_OK = re.compile(r"^show [a-zA-Z0-9 ._:/\[\]-]{0,200}$")      # a looking glass runs `show` commands and nothing else
PING_OK = re.compile(r"^(ping|traceroute) ([0-9a-fA-F.:]{2,45})( vrf ([\w-]+))?( count (\d{1,2}))?$")


def create_app(cfg, store, collector):
    app = Flask(__name__, static_folder=None)
    app.config["SEND_FILE_MAX_AGE_DEFAULT"] = 0     # the page is redeployed with the lab: always revalidate it
    started = time.time()
    devices = {d["name"]: d for d in cfg["devices"]}

    def meta():
        vrfs = sorted({v for v in (cfg.get("service", {}).get("tenants") or {})} | {"default"})
        return {"lab": cfg["lab"], "node": cfg["node"], "vrfs": vrfs, "tenants": cfg.get("service", {}).get("tenants", {}),
                "rds": cfg.get("rd_map", {}), "nodes": cfg.get("nodes", {}), "devices": list(devices),
                "sources": ["collector"] + list(devices), "srv6": cfg.get("service", {}).get("srv6", {}),
                "core_as": cfg.get("service", {}).get("core_as"), "rrs": cfg.get("service", {}).get("rrs", []),
                "peers": cfg["collector"]["peers"], "families": cfg["collector"]["families"],
                "locators": cfg.get("locators", {}), "loopbacks": cfg.get("loopbacks", {})}

    @app.get("/api/meta")
    def api_meta(): return jsonify(meta())

    @app.get("/api/status")
    def api_status():
        counts = store.counts(); polls = {p["source"]: p for p in store.polls()}
        now = time.time()
        return jsonify({"lab": cfg["lab"], "node": cfg["node"], "uptime": now - started, "now": now,
                        "collector": {"asn": cfg["collector"]["asn"], "router_id": cfg["collector"]["router_id"],
                                      "peers": list(collector.peers.values())},
                        "counts": counts, "polls": polls, "churn": {"5m": store.churn(now - 300), "1h": store.churn(now - 3600),
                                                                    "24h": store.churn(now - 86400)},
                        "db": store.stats(), "poll_intervals": cfg["poll"]})

    @app.get("/api/prefixes")
    def api_prefixes():
        g = request.args
        total, rows = store.paths(alive=None if g.get("alive") == "all" else g.get("alive", "1") == "1",
                                  limit=min(int(g.get("limit", 200)), 2000), offset=int(g.get("offset", 0)),
                                  order=g.get("order", "prefix"), source=g.getlist("source") or g.get("source"),
                                  afi=g.get("afi"), safi=g.get("safi"), vrf=g.getlist("vrf") or g.get("vrf"),
                                  rd=g.get("rd"), prefix=g.get("prefix"), peer_name=g.get("peer"),
                                  origin_node=g.get("origin_node"), origin_as=g.get("origin_as"),
                                  q=g.get("q"), best_only=g.get("best_only") == "1")
        return jsonify({"total": total, "count": len(rows), "offset": int(g.get("offset", 0)), "paths": rows})

    @app.get("/api/prefix")
    def api_prefix():
        prefix = request.args.get("prefix", "")
        if not prefix: return jsonify({"error": "prefix is required"}), 400
        _, rows = store.paths(alive=None, limit=500, prefix=prefix)
        events = store.events(prefix=prefix, limit=int(request.args.get("events", 200)))
        return jsonify({"prefix": prefix, "paths": rows, "events": events,
                        "first_seen": min((r["first_seen"] for r in rows), default=None),
                        "last_change": max((r["last_change"] for r in rows), default=None)})

    @app.get("/api/history")
    def api_history():
        g = request.args
        return jsonify({"events": store.events(prefix=g.get("prefix"), since=g.get("since"), until=g.get("until"),
                                               kinds=g.getlist("kind") or None, limit=min(int(g.get("limit", 200)), 2000))})

    @app.get("/api/state")
    def api_state():
        at = float(request.args.get("at", time.time()))
        rows = store.state_at(at, prefix=request.args.get("prefix"), source=request.args.get("source"), vrf=request.args.get("vrf"))
        return jsonify({"at": at, "count": len(rows), "paths": rows})

    @app.get("/api/series")
    def api_series():
        g = request.args
        return jsonify({"metric": g.get("metric", "paths"),
                        "series": store.series(g.get("metric", "paths"), since=g.get("since", time.time() - 3600),
                                               until=g.get("until"), source=g.get("source"), step=g.get("step"))})

    @app.get("/api/peers")
    def api_peers():
        return jsonify({"peers": list(collector.peers.values()), "polls": store.polls(), "devices": cfg["devices"]})

    @app.post("/api/query")
    def api_query():
        body = request.get_json(silent=True) or {}
        device, command = body.get("device", ""), (body.get("command") or "").strip()
        if device not in devices and device != cfg["node"]:
            return jsonify({"error": f"unknown device {device!r}"}), 400
        kind = "show" if SHOW_OK.match(command) else ("ping" if PING_OK.match(command) else None)
        if not kind:
            return jsonify({"error": "only `show ...`, `ping <address> [vrf X] [count N]` and `traceroute <address>` are allowed"}), 400
        t0 = time.time()
        try:
            if device == cfg["node"]:
                if kind != "show": return jsonify({"error": "the looking glass itself only answers `show` commands"}), 400
                out = collector.vtysh(command, timeout=60)
            else:
                out = _run_on_device(cfg, devices[device], command, kind)
        except Exception as e:                                   # noqa: BLE001 — report it, the page shows it
            return jsonify({"device": device, "command": command, "error": f"{e.__class__.__name__}: {e}"}), 502
        return jsonify({"device": device, "command": command, "output": out, "seconds": round(time.time() - t0, 2)})

    @app.get("/metrics")
    def metrics():
        L = {"lab": cfg["lab"], "node": cfg["node"]}
        out = [f'# HELP lg_up the looking glass is running', f'# TYPE lg_up gauge', _m("lg_up", 1, L)]
        for c in store.counts():
            lab = {**L, "source": c["source"], "afi": c["afi"], "safi": c["safi"], "vrf": c["vrf"] or "-"}
            out += [_m("lg_paths", c["n"], lab), _m("lg_prefixes", c["prefixes"], lab)]
        for p in collector.peers.values():
            lab = {**L, "peer": p["name"] or p["ip"], "remote_as": p.get("remote_as")}
            out.append(_m("lg_session_up", 1 if p["state"] == "Established" else 0, lab))
            for af, d in (p.get("families") or {}).items():
                if d.get("accepted") is not None: out.append(_m("lg_session_accepted_prefixes", d["accepted"], {**lab, "af": af}))
        now = time.time()
        for kind, n in store.churn(now - 300).items(): out.append(_m("lg_events_5m", n, {**L, "kind": kind}))
        for kind, n in store.churn(now - 3600).items(): out.append(_m("lg_events_1h", n, {**L, "kind": kind}))
        for p in store.polls():
            lab = {**L, "source": p["source"]}
            out += [_m("lg_poll_ok", p["ok"], lab), _m("lg_poll_age_seconds", now - (p["ts"] or now), lab),
                    _m("lg_poll_duration_seconds", p["duration"] or 0, lab)]
        s = store.stats()
        out += [_m("lg_db_bytes", s["db_bytes"], L), _m("lg_db_events", s["events"], L), _m("lg_db_paths", s["paths"], L)]
        return Response("\n".join(out) + "\n", mimetype="text/plain; version=0.0.4")

    @app.get("/")
    def index(): return send_from_directory(STATIC, "index.html")

    @app.get("/<path:name>")
    def static_file(name): return send_from_directory(STATIC, name)

    return app


def _m(name, value, labels):
    lab = ",".join(f'{k}="{v}"' for k, v in labels.items() if v is not None)
    return f"{name}{{{lab}}} {value}"


def _run_on_device(cfg, dev, command, kind):
    """A live command on a router, over SSH. `show` goes to vtysh (FRR's view); ping / traceroute run as op-mode."""
    import paramiko
    ssh = cfg["ssh"]
    c = paramiko.SSHClient(); c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    c.connect(dev["mgmt_ip"], username=ssh["username"], password=ssh["password"], timeout=20, look_for_keys=False, allow_agent=False)
    try:
        if kind == "show":
            cmd = f"vtysh -c '{command}'"
        else:
            m = PING_OK.match(command); tool, target, vrf, count = m.group(1), m.group(2), m.group(4), m.group(6) or "3"
            # a tenant lives in a VRF, so the probe has to be sent from inside it — `ip vrf exec` does the route lookup
            # there (ping's own -I only binds the source device, and traceroute's -I means something else entirely)
            pre = f"sudo ip vrf exec {vrf} " if vrf else ""
            cmd = pre + (f"ping -c {count} -w {int(count) + 5} {target}" if tool == "ping" else f"traceroute -w 1 -q 1 -m 12 {target}")
        _, out, err = c.exec_command(cmd, timeout=90)
        text = out.read().decode(errors="replace") + err.read().decode(errors="replace")
        out.channel.recv_exit_status()
        return text
    finally:
        c.close()
