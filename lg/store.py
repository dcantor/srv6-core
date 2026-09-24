#!/usr/bin/env python3
"""The looking glass's store: what the core's BGP tables hold *now*, and everything they have held before.

SQLite, because what has to be kept is not a number per timestamp but a *path with its attributes* — an AS path, a set of
route targets, an SRv6 SID, an originator — and the question asked of it ("what did this prefix look like at 14:05, and
what changed since?") is a query over attribute history, not an aggregation over samples. Numbers that *are* numbers
(prefix counts per VRF, churn, session state) go into `sample`, which is a plain time series and also what /metrics and
Grafana read.

Three tables carry it:
  path     one row per (source, address family, VRF / RD, prefix, peer) — the identity of a path, with its current
           attributes, when it was first and last seen, and whether it is still there
  event    append-only: every announce, every attribute change (with the fields that changed), every withdraw
  sample   numeric series: (ts, source, metric, labels) -> value

A snapshot from the collector or from a device is *reconciled* against the table: paths that are new raise an announce,
paths whose attributes differ raise a change carrying the diff, paths that have disappeared raise a withdraw. State at a
past moment is therefore the last event of each path at or before that moment — `state_at()` replays exactly that.
"""
import json, sqlite3, threading, time

SCHEMA = """
CREATE TABLE IF NOT EXISTS path (
  id INTEGER PRIMARY KEY,
  source TEXT NOT NULL,            -- 'collector' (this VM's own BGP table) or the device the view was polled from
  afi TEXT NOT NULL, safi TEXT NOT NULL,
  vrf TEXT,                        -- tenant-a / tenant-b / default (for VPN routes: resolved from the RD)
  rd TEXT,                         -- VPN routes only
  prefix TEXT NOT NULL,
  peer TEXT,                       -- the BGP peer that advertised it (address), '' when the path is local / imported
  disc TEXT,                       -- discriminator: a prefix can have several paths from the same peer (multipath, imported
                                   -- from several RDs) — the next hop, plus an index when even that repeats
  peer_name TEXT,                  -- that peer resolved to a node name
  origin_node TEXT,                -- the node the path came from (originator-id / next hop / SRv6 locator)
  origin_as INTEGER,               -- the last AS of the AS path (the CE that owns the prefix)
  nexthop TEXT,
  best INTEGER DEFAULT 0,
  attrs TEXT NOT NULL,             -- JSON: the whole normalised attribute set
  first_seen REAL, last_seen REAL, last_change REAL,
  alive INTEGER DEFAULT 1,
  UNIQUE(source, afi, safi, vrf, rd, prefix, peer, disc)
);
CREATE INDEX IF NOT EXISTS path_prefix   ON path(prefix);
CREATE INDEX IF NOT EXISTS path_alive    ON path(alive, source, afi, safi);
CREATE INDEX IF NOT EXISTS path_vrf      ON path(vrf, alive);

CREATE TABLE IF NOT EXISTS event (
  id INTEGER PRIMARY KEY,
  ts REAL NOT NULL,
  path_id INTEGER NOT NULL REFERENCES path(id),
  kind TEXT NOT NULL,              -- announce | change | withdraw
  attrs TEXT,                      -- the attribute set as of this event (null for a withdraw)
  changes TEXT                     -- {field: [before, after]} for a change
);
CREATE INDEX IF NOT EXISTS event_ts   ON event(ts);
CREATE INDEX IF NOT EXISTS event_path ON event(path_id, ts);

CREATE TABLE IF NOT EXISTS sample (
  ts REAL NOT NULL, source TEXT NOT NULL, metric TEXT NOT NULL, labels TEXT NOT NULL, value REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS sample_metric ON sample(metric, ts);

CREATE TABLE IF NOT EXISTS poll (          -- the health of every collection, so the UI can say when a view went stale
  source TEXT PRIMARY KEY, ts REAL, ok INTEGER, duration REAL, paths INTEGER, error TEXT
);
"""

# Attributes that change on their own (age, table version, counters) and would otherwise make every poll look like a
# change. They are still shown — they are simply not what "the path changed" means.
VOLATILE = {"version", "lastUpdate", "age", "uptime", "peerUptime", "peerUptimeMsec", "peerUptimeEstablishedEpoch",
            "table_version", "last_update", "accessible", "used", "metric_igp"}


def _norm(attrs):
    return {k: v for k, v in attrs.items() if k not in VOLATILE and v is not None and v != "" and v != []}


class Store:
    def __init__(self, path, history_days=30):
        self.path, self.history_days = path, history_days
        self._lock = threading.Lock()
        self._local = threading.local()
        with self.conn() as c:
            c.executescript(SCHEMA)

    def conn(self):
        """One connection per thread (SQLite objects are not shareable); WAL so the API reads while the collector writes."""
        c = getattr(self._local, "conn", None)
        if c is None:
            c = sqlite3.connect(self.path, timeout=30, isolation_level=None)
            c.row_factory = sqlite3.Row
            c.execute("PRAGMA journal_mode=WAL"); c.execute("PRAGMA synchronous=NORMAL"); c.execute("PRAGMA busy_timeout=20000")
            self._local.conn = c
        return c

    # ---- writing ---------------------------------------------------------------------------------------------
    def reconcile(self, source, paths, ts=None, scope=None):
        """Replace `source`'s view with `paths` and record what changed.

        `paths` is a list of dicts with the identity fields and `attrs`. `scope` narrows what a partial snapshot is
        allowed to withdraw — a device poll that only read tenant-a must not withdraw tenant-b's paths — as a dict of
        column -> allowed values."""
        ts = ts or time.time()
        key = lambda p: (p["afi"], p["safi"], p.get("vrf") or "", p.get("rd") or "", p["prefix"], p.get("peer") or "", p.get("disc") or "")
        seen = {key(p): p for p in paths}
        announced = changed = withdrawn = 0
        with self._lock:
            c = self.conn()
            c.execute("BEGIN")
            try:
                where, args = "source = ?", [source]
                for col, vals in (scope or {}).items():
                    where += f" AND {col} IN ({','.join('?' * len(vals))})"; args += list(vals)
                have = {(r["afi"], r["safi"], r["vrf"] or "", r["rd"] or "", r["prefix"], r["peer"] or "", r["disc"] or ""): r
                        for r in c.execute(f"SELECT * FROM path WHERE {where}", args)}
                for k, p in seen.items():
                    attrs = p["attrs"]; row = have.get(k)
                    cols = dict(peer_name=p.get("peer_name"), origin_node=p.get("origin_node"), origin_as=p.get("origin_as"),
                                nexthop=p.get("nexthop"), best=1 if p.get("best") else 0, attrs=json.dumps(attrs, sort_keys=True))
                    if row is None:
                        cur = c.execute("INSERT INTO path (source, afi, safi, vrf, rd, prefix, peer, disc, peer_name, origin_node,"
                                        " origin_as, nexthop, best, attrs, first_seen, last_seen, last_change, alive)"
                                        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,1)",
                                        (source, p["afi"], p["safi"], p.get("vrf"), p.get("rd"), p["prefix"], p.get("peer"),
                                         p.get("disc"), cols["peer_name"], cols["origin_node"], cols["origin_as"], cols["nexthop"],
                                         cols["best"], cols["attrs"], ts, ts, ts))
                        c.execute("INSERT INTO event (ts, path_id, kind, attrs) VALUES (?,?,'announce',?)", (ts, cur.lastrowid, cols["attrs"]))
                        announced += 1
                        continue
                    before = _norm(json.loads(row["attrs"])); after = _norm(attrs)
                    diff = {k2: [before.get(k2), after.get(k2)] for k2 in set(before) | set(after) if before.get(k2) != after.get(k2)}
                    if row["alive"] == 0:      # it came back
                        c.execute("UPDATE path SET alive=1, attrs=?, peer_name=?, origin_node=?, origin_as=?, nexthop=?, best=?,"
                                  " last_seen=?, last_change=? WHERE id=?",
                                  (cols["attrs"], cols["peer_name"], cols["origin_node"], cols["origin_as"], cols["nexthop"],
                                   cols["best"], ts, ts, row["id"]))
                        c.execute("INSERT INTO event (ts, path_id, kind, attrs, changes) VALUES (?,?,'announce',?,?)",
                                  (ts, row["id"], cols["attrs"], json.dumps(diff) if diff else None))
                        announced += 1
                    elif diff:
                        c.execute("UPDATE path SET attrs=?, peer_name=?, origin_node=?, origin_as=?, nexthop=?, best=?,"
                                  " last_seen=?, last_change=? WHERE id=?",
                                  (cols["attrs"], cols["peer_name"], cols["origin_node"], cols["origin_as"], cols["nexthop"],
                                   cols["best"], ts, ts, row["id"]))
                        c.execute("INSERT INTO event (ts, path_id, kind, attrs, changes) VALUES (?,?,'change',?,?)",
                                  (ts, row["id"], cols["attrs"], json.dumps(diff)))
                        changed += 1
                    else:
                        c.execute("UPDATE path SET last_seen=?, attrs=? WHERE id=?", (ts, cols["attrs"], row["id"]))
                for k, row in have.items():
                    if k in seen or row["alive"] == 0: continue
                    c.execute("UPDATE path SET alive=0, last_change=? WHERE id=?", (ts, row["id"]))
                    c.execute("INSERT INTO event (ts, path_id, kind) VALUES (?,?,'withdraw')", (ts, row["id"]))
                    withdrawn += 1
                c.execute("COMMIT")
            except Exception:
                c.execute("ROLLBACK"); raise
        return {"announced": announced, "changed": changed, "withdrawn": withdrawn, "total": len(seen)}

    def sample(self, source, metric, value, ts=None, **labels):
        with self._lock:
            self.conn().execute("INSERT INTO sample (ts, source, metric, labels, value) VALUES (?,?,?,?,?)",
                                (ts or time.time(), source, metric, json.dumps(labels, sort_keys=True), float(value)))

    def note_poll(self, source, ok, duration, paths=0, error=None, ts=None):
        with self._lock:
            self.conn().execute("INSERT INTO poll (source, ts, ok, duration, paths, error) VALUES (?,?,?,?,?,?)"
                                " ON CONFLICT(source) DO UPDATE SET ts=excluded.ts, ok=excluded.ok, duration=excluded.duration,"
                                " paths=excluded.paths, error=excluded.error",
                                (source, ts or time.time(), 1 if ok else 0, duration, paths, error))

    def prune(self):
        """Drop history past the retention window, and the paths that died before it (their events are gone)."""
        cut = time.time() - self.history_days * 86400
        with self._lock:
            c = self.conn(); c.execute("BEGIN")
            c.execute("DELETE FROM event WHERE ts < ?", (cut,))
            c.execute("DELETE FROM sample WHERE ts < ?", (cut,))
            c.execute("DELETE FROM path WHERE alive = 0 AND last_change < ?", (cut,))
            c.execute("COMMIT")

    # ---- reading ---------------------------------------------------------------------------------------------
    @staticmethod
    def _row(r):
        d = dict(r); d["attrs"] = json.loads(d["attrs"]) if d.get("attrs") else {}
        d["alive"] = bool(d.get("alive")); d["best"] = bool(d.get("best"))
        return d

    def paths(self, alive=True, limit=500, offset=0, order="prefix", **f):
        """Current (or historical, alive=None) paths with the usual filters; `q` matches prefix / AS path / next hop / SID."""
        where, args = [], []
        if alive is not None: where.append("alive = ?"); args.append(1 if alive else 0)
        for col in ("source", "afi", "safi", "vrf", "rd", "prefix", "peer_name", "origin_node", "origin_as"):
            v = f.get(col)
            if v in (None, "", "all"): continue
            vals = v if isinstance(v, (list, tuple)) else [v]
            where.append(f"{col} IN ({','.join('?' * len(vals))})"); args += list(vals)
        if f.get("q"):
            q = f"%{f['q']}%"; where.append("(prefix LIKE ? OR attrs LIKE ? OR nexthop LIKE ? OR rd LIKE ? OR origin_node LIKE ?)")
            args += [q, q, q, q, q]
        if f.get("best_only"): where.append("best = 1")
        sql = "SELECT * FROM path" + (" WHERE " + " AND ".join(where) if where else "")
        total = self.conn().execute(sql.replace("SELECT *", "SELECT COUNT(*)", 1), args).fetchone()[0]
        order = {"prefix": "prefix, vrf, source", "changed": "last_change DESC", "seen": "last_seen DESC",
                 "age": "first_seen"}.get(order, "prefix")
        rows = self.conn().execute(f"{sql} ORDER BY {order} LIMIT ? OFFSET ?", args + [int(limit), int(offset)])
        return total, [self._row(r) for r in rows]

    def path_by_id(self, pid):
        r = self.conn().execute("SELECT * FROM path WHERE id = ?", (pid,)).fetchone()
        return self._row(r) if r else None

    def events(self, path_ids=None, since=None, until=None, kinds=None, limit=500, prefix=None):
        where, args = [], []
        if path_ids: where.append(f"path_id IN ({','.join('?' * len(path_ids))})"); args += list(path_ids)
        if prefix: where.append("path_id IN (SELECT id FROM path WHERE prefix = ?)"); args.append(prefix)
        if since: where.append("ts >= ?"); args.append(float(since))
        if until: where.append("ts <= ?"); args.append(float(until))
        if kinds: where.append(f"kind IN ({','.join('?' * len(kinds))})"); args += list(kinds)
        sql = ("SELECT e.*, p.source, p.afi, p.safi, p.vrf, p.rd, p.prefix, p.peer_name, p.origin_node FROM event e"
               " JOIN path p ON p.id = e.path_id")
        if where: sql += " WHERE " + " AND ".join(where)
        rows = self.conn().execute(sql + " ORDER BY e.ts DESC, e.id DESC LIMIT ?", args + [int(limit)])
        out = []
        for r in rows:
            d = dict(r)
            d["attrs"] = json.loads(d["attrs"]) if d["attrs"] else None
            d["changes"] = json.loads(d["changes"]) if d["changes"] else None
            out.append(d)
        return out

    def state_at(self, ts, prefix=None, source=None, vrf=None):
        """What the table held at `ts`: for every path, the last event at or before it — a withdraw means it was gone."""
        where, args = ["e.ts <= ?"], [float(ts)]
        if prefix: where.append("p.prefix = ?"); args.append(prefix)
        if source: where.append("p.source = ?"); args.append(source)
        if vrf: where.append("p.vrf = ?"); args.append(vrf)
        rows = self.conn().execute(
            "SELECT p.*, e.ts AS event_ts, e.kind, e.attrs AS event_attrs FROM path p JOIN event e ON e.path_id = p.id"
            " JOIN (SELECT path_id, MAX(ts) AS mts, MAX(id) AS mid FROM event WHERE ts <= ? GROUP BY path_id) last"
            "   ON last.path_id = e.path_id AND e.id = last.mid"
            " WHERE " + " AND ".join(where), [float(ts)] + args)
        out = []
        for r in rows:
            if r["kind"] == "withdraw": continue
            d = self._row(r)
            d["attrs"] = json.loads(r["event_attrs"]) if r["event_attrs"] else d["attrs"]
            d["as_of"] = r["event_ts"]; d["alive"] = True
            out.append(d)
        return sorted(out, key=lambda d: (d["prefix"], d["vrf"] or "", d["source"]))

    def series(self, metric, since=None, until=None, source=None, step=None):
        where, args = ["metric = ?"], [metric]
        if since: where.append("ts >= ?"); args.append(float(since))
        if until: where.append("ts <= ?"); args.append(float(until))
        if source: where.append("source = ?"); args.append(source)
        rows = self.conn().execute(f"SELECT ts, source, labels, value FROM sample WHERE {' AND '.join(where)} ORDER BY ts", args)
        out = {}
        for r in rows:
            out.setdefault(r["labels"], {"labels": json.loads(r["labels"]), "source": r["source"], "points": []})["points"].append([r["ts"], r["value"]])
        if step:                                     # thin the series for a chart: one point per step
            for s in out.values():
                keep, last = [], None
                for ts, v in s["points"]:
                    if last is None or ts - last >= float(step): keep.append([ts, v]); last = ts
                s["points"] = keep
        return list(out.values())

    def counts(self, alive=True):
        rows = self.conn().execute("SELECT source, afi, safi, vrf, COUNT(*) n, COUNT(DISTINCT prefix) prefixes FROM path"
                                   " WHERE alive = ? GROUP BY source, afi, safi, vrf", (1 if alive else 0,))
        return [dict(r) for r in rows]

    def polls(self):
        return [dict(r) for r in self.conn().execute("SELECT * FROM poll ORDER BY source")]

    def churn(self, since):
        rows = self.conn().execute("SELECT kind, COUNT(*) n FROM event WHERE ts >= ? GROUP BY kind", (float(since),))
        return {r["kind"]: r["n"] for r in rows}

    def stats(self):
        c = self.conn()
        return {"paths": c.execute("SELECT COUNT(*) FROM path WHERE alive = 1").fetchone()[0],
                "paths_gone": c.execute("SELECT COUNT(*) FROM path WHERE alive = 0").fetchone()[0],
                "events": c.execute("SELECT COUNT(*) FROM event").fetchone()[0],
                "samples": c.execute("SELECT COUNT(*) FROM sample").fetchone()[0],
                "oldest_event": c.execute("SELECT MIN(ts) FROM event").fetchone()[0],
                "db_bytes": c.execute("SELECT page_count * page_size FROM pragma_page_count(), pragma_page_size()").fetchone()[0]}
