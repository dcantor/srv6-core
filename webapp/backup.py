"""Backup and restore: the whole lab state in one file, and a plan to bring the lab back to it.

A backup (`srv6-core-<time>.tar.gz`, kept in webapp/backups/, the newest KEEP) holds:
    manifest.json          lab, version, when, from which host, tenants and sites, steering policies, every file's SHA-256
    lab.conf               the intent — everything else is generated from it (day-0 configs, Nautobot's model)
    steering.json          the explicit-path policies on the PEs: live state that lab.conf does not hold
    running/<node>.txt     `show configuration commands` of every VyOS router, as it ran (the record, not what a restore pushes)
    rendered/<node>.txt    the rendered day-0 configuration of every router
Nautobot is not in it: `lab.sh nautobot seed` rebuilds the model from lab.conf. The looking glass's history is not in it.

A restore reads a backup, checks every checksum, and plans against the running lab from both inventories (the backup's
is built from its lab.conf by running lab.sh inventory on a copy):
    tenants the backup has and the lab does not      → added, with the backup's own allocation (ports, addresses, hosts)
    sites a tenant had then and not now               → added to that tenant
    tenants the lab has and the backup does not      → removed
    steering policies                                 → the backup's set: missing ones added, extra ones removed
    anything else the two lab.conf files disagree on  → listed; a restore does not change the core, and refuses when the
                                                        core differs (routers, core links) or a site would have to go
The run then does what the portal's add / remove runs do, tenant by tenant, and ends with Nautobot, verification and tests."""
import hashlib, io, json, os, re, shutil, socket, subprocess, tarfile, tempfile, time, zlib
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import tenants as T

LAB = Path(__file__).resolve().parents[1]
DIR = Path(__file__).resolve().parent / "backups"
DIR.mkdir(exist_ok=True)
KEEP = 20
ROUTER_ROLES = {"p", "pe", "ce", "fw"}


def _sha(b):
    return hashlib.sha256(b).hexdigest()


def _running(n):
    from netmiko import ConnectHandler
    c = ConnectHandler(device_type="vyos", host=n["mgmt_ip"], username=os.environ.get("VYOS_USERNAME", "vyos"),
                       password=os.environ.get("VYOS_PASSWORD", "vyos"), conn_timeout=30)
    try:
        return c.send_command("show configuration commands", read_timeout=120)
    finally:
        c.disconnect()


def create(steering):
    """Write a backup; returns its manifest (with `file`, the name to download)."""
    inv = T.inventory()
    routers = [n for n in inv["nodes"] if n["role"] in ROUTER_ROLES]
    files = {"lab.conf": (LAB / "lab.conf").read_bytes(), "steering.json": json.dumps(steering, indent=1).encode()}
    errors = {}

    def one(n):
        try:
            return n["name"], _running(n), None
        except Exception as e:                                           # noqa: BLE001 — a router down is recorded, not fatal
            return n["name"], None, f"{e.__class__.__name__}: {e}"
    with ThreadPoolExecutor(max_workers=8) as ex:
        for name, text, err in ex.map(one, routers):
            if text is not None:
                files[f"running/{name}.txt"] = text.encode()
            else:
                errors[name] = err
    for n in routers:
        p = LAB / "nodes" / n["name"] / "vyos_config.txt"
        if p.exists():
            files[f"rendered/{n['name']}.txt"] = p.read_bytes()
    f = T.facts(inv)
    manifest = {"lab": "srv6-core", "version": (LAB / "VERSION").read_text().strip() if (LAB / "VERSION").exists() else None,
                "created": time.time(), "host": socket.gethostname(),
                "tenants": {t: {**f["tenants"][t], "sites": [s["dc"] for s in T.tenant_sites(f, t)]} for t in f["order"]},
                "steering": steering, "routers": [n["name"] for n in routers], "unread": errors,
                "files": {k: {"sha256": _sha(v), "bytes": len(v)} for k, v in files.items()}}
    name = f"srv6-core-{time.strftime('%Y%m%d-%H%M%S')}.tar.gz"
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for k, v in [("manifest.json", json.dumps(manifest, indent=1).encode())] + sorted(files.items()):
            ti = tarfile.TarInfo(k); ti.size = len(v); ti.mtime = int(manifest["created"]); tar.addfile(ti, io.BytesIO(v))
    (DIR / name).write_bytes(buf.getvalue())
    for old in sorted(DIR.glob("srv6-core-*.tar.gz"))[:-KEEP]:
        old.unlink()
    return {**manifest, "file": name, "bytes": len(buf.getvalue())}


def listing():
    out = []
    for p in sorted(DIR.glob("srv6-core-*.tar.gz"), reverse=True):
        try:
            m = read(p.read_bytes())["manifest"]
            out.append({"file": p.name, "bytes": p.stat().st_size, "created": m["created"], "version": m.get("version"),
                        "tenants": list(m["tenants"]), "steering": len(m.get("steering") or [])})
        except Exception as e:                                           # noqa: BLE001
            out.append({"file": p.name, "bytes": p.stat().st_size, "error": str(e)})
    return out


def read(data):
    """Open a backup and check it: {manifest, files}; raises ValueError on anything wrong."""
    try:
        return _read(data)
    except ValueError:
        raise
    except (tarfile.TarError, OSError, EOFError, zlib.error, KeyError, json.JSONDecodeError) as e:
        raise ValueError(f"the backup is damaged: {e.__class__.__name__}: {e}")


def _read(data):
    try:
        tar = tarfile.open(fileobj=io.BytesIO(data), mode="r:gz")
    except (tarfile.TarError, OSError) as e:
        raise ValueError(f"not a backup (.tar.gz): {e}")
    members = {m.name: m for m in tar.getmembers() if m.isfile()}
    if "manifest.json" not in members:
        raise ValueError("no manifest.json: not a backup of this lab")
    manifest = json.loads(tar.extractfile(members["manifest.json"]).read())
    if manifest.get("lab") != "srv6-core":
        raise ValueError(f"a backup of {manifest.get('lab')!r}, not of srv6-core")
    files = {}
    for name, meta in manifest["files"].items():
        if name not in members:
            raise ValueError(f"{name} is missing from the backup")
        b = tar.extractfile(members[name]).read()
        if _sha(b) != meta["sha256"]:
            raise ValueError(f"{name}: checksum mismatch — the backup is damaged")
        files[name] = b
    return {"manifest": manifest, "files": files}


def inventory_of(labconf):
    """The inventory a lab.conf describes: lab.sh inventory, run on a copy of lab.sh next to that lab.conf."""
    with tempfile.TemporaryDirectory() as d:
        shutil.copy(LAB / "lab.sh", Path(d) / "lab.sh")
        (Path(d) / "lab.conf").write_bytes(labconf)
        r = subprocess.run([str(Path(d) / "lab.sh"), "inventory"], capture_output=True, text=True, timeout=60, cwd=d)
        if r.returncode != 0:
            raise ValueError(f"the backup's lab.conf does not read: {(r.stderr or r.stdout)[-300:]}")
        return json.loads(r.stdout)


def _spec(f, tenant, dcs=None):
    """A tenant (or some of its sites) as the portal's add runs take it, from an inventory's facts."""
    t = f["tenants"][tenant]
    sites = []
    for s in T.tenant_sites(f, tenant):
        if s.get("external") or (dcs and s["dc"] not in dcs) or not s.get("host"):
            continue
        h = f["N"][s["host"]]
        sites.append({"dc": s["dc"], "pe": s["pe"], "ce": s["ce"], "pe_port": s["pe_port"], "ce_pe_port": s["ce_pe_port"],
                      "ce_lan_port": s["ce_lan_port"], "attachment_circuit": s["attachment_circuit"], "lan": s["lan"],
                      "host": s["host"], "host_mgmt": s["host_mgmt"], "host_console": h["console"], "host_idx": h["idx"],
                      "host_ip": s["host_ip"], "gateway": str(__import__("ipaddress").ip_network(s["lan"]).network_address + 1)})
    return {"name": tenant, "table": t["table"], "rt": t["rt"], "description": f"{tenant} (restored)", "sites": sites}


def _core(inv):
    nodes = sorted((n["name"], n["role"]) for n in inv["nodes"] if n["role"] not in ("host",))
    links = sorted(tuple(sorted((l["a"], l["b"]))) + (l["prefix"],) for l in inv["links"] if not l.get("tenant"))
    return nodes, links


def plan(backup, current_steering):
    """What restoring this backup would change, and whether it can."""
    m, files = backup["manifest"], backup["files"]
    binv, cinv = inventory_of(files["lab.conf"]), T.inventory()
    bf, cf = T.facts(binv), T.facts(cinv)
    problems, notes = [], []
    bn, bl = _core(binv); cn, cl = _core(cinv)
    if bn != cn:
        problems.append("the core differs from the backup's (routers: " + ", ".join(sorted({x[0] for x in set(bn) ^ set(cn)})) + ") — a restore changes tenants and steering only")
    if bl != cl:
        problems.append(f"the core links differ from the backup's ({len(set(bl) ^ set(cl))} link(s)) — a restore changes tenants and steering only")
    add, add_sites, remove = [], [], []
    for t in bf["order"]:
        if t not in cf["tenants"]:
            add.append(_spec(bf, t))
            continue
        bd = {s["dc"] for s in T.tenant_sites(bf, t) if not s.get("external")}
        cd = {s["dc"] for s in T.tenant_sites(cf, t) if not s.get("external")}
        if bd - cd:
            add_sites.append(_spec(bf, t, sorted(bd - cd)))
        if cd - bd:
            problems.append(f"{t} has site(s) {', '.join(sorted(cd - bd))} the backup does not: the portal cannot remove a single site")
        if (bf["tenants"][t]["table"], bf["tenants"][t]["rt"]) != (cf["tenants"][t]["table"], cf["tenants"][t]["rt"]):
            problems.append(f"{t}: kernel table / route target differ from the backup's")
    for t in cf["order"]:
        if t not in bf["tenants"]:
            remove.append(t)
    key = lambda p: (p["pe"], p["tenant"], p["prefix"])
    want = {key(p): p for p in (m.get("steering") or []) if "pe" in p}
    have = {key(p): p for p in (current_steering or []) if "pe" in p}
    steer_add = [p for k, p in want.items() if k not in have or have[k].get("segments") != p.get("segments")]
    steer_del = [p for k, p in have.items() if k not in want or want[k].get("segments") != p.get("segments")]
    for p in steer_add:
        if not p.get("via"):
            p["via"] = _via(binv, p)
            if not p["via"]:
                problems.append(f"steering {p['pe']} {p['tenant']} {p['prefix']}: its path could not be read back from the segments")
    if (LAB / "lab.conf").read_bytes() != files["lab.conf"] and not (add or add_sites or remove):
        notes.append("lab.conf differs from the backup's in something other than tenants — not restored")
    return {"backup": {k: m.get(k) for k in ("created", "version", "host", "tenants", "unread")},
            "add": add, "add_sites": add_sites, "remove": remove, "steering_add": steer_add, "steering_remove": steer_del,
            "same": not (add or add_sites or remove or steer_add or steer_del), "problems": problems, "notes": notes}


def _via(inv, policy):
    """The P routers a stored policy's segments name (a uSID carrier unpacked), for steer.py add."""
    import sys
    sys.path.insert(0, str(LAB / "lg"))
    import paths
    cfg = {"service": inv["service"], "nodes": {n["name"]: n for n in inv["nodes"]}}
    names = paths.segment_nodes(policy.get("segments") or [], cfg)
    role = {n["name"]: n["role"] for n in inv["nodes"]}
    return [n for n in names if role.get(n) == "p"]


def save_upload(data):
    """Keep an uploaded backup next to the others (checked first); returns its file name."""
    b = read(data)
    name = f"srv6-core-{time.strftime('%Y%m%d-%H%M%S', time.localtime(b['manifest']['created']))}.tar.gz"
    p = DIR / name
    if not p.exists():
        p.write_bytes(data)
    return name, b


def load(name):
    if not re.fullmatch(r"srv6-core-\d{8}-\d{6}\.tar\.gz", name or "") or not (DIR / name).exists():
        raise ValueError(f"no backup named {name!r}")
    return read((DIR / name).read_bytes())
