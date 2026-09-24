#!/usr/bin/env python3
"""Deploy the looking glass onto the lg VM: its FRR configuration, the lgd service and the model behind it.

The collector's configuration is rendered from the same inventory as every router (tools/render.py: `render_lg` for
frr.conf, `lg_app_config` for what lgd needs to resolve RDs, locators and next hops into names), so the looking glass is
a modelled part of the lab, not a hand-kept VM. FRR is only restarted when its configuration actually changed — a
restart drops the sessions and, with them, the live view.

   lg_deploy.py                 render, copy, (re)start lgd, restart FRR if its config changed
   lg_deploy.py --restart-frr   restart FRR as well, whatever changed
   lg_deploy.py --status        what the running looking glass holds (no changes)
   lg_deploy.py --dry-run       print what would be copied
   lg_deploy.py --exec CMD      run a command on the VM (the password lives here, not in an interactive ssh)"""
import argparse, hashlib, json, subprocess, sys, time, urllib.request
from pathlib import Path

LAB = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(LAB / "tools")); from render import render_lg, lg_app_config, lg_nodes   # noqa: E402

p = argparse.ArgumentParser()
p.add_argument("--node", default=None, help="which looking glass (default: the only one in lab.conf)")
p.add_argument("--restart-frr", action="store_true"); p.add_argument("--status", action="store_true")
p.add_argument("--dry-run", action="store_true"); p.add_argument("--no-restart", action="store_true")
p.add_argument("--exec", dest="exec_", metavar="CMD", help="run a command on the looking glass over SSH and print it (lab.sh lg logs / frr / restart)")
a = p.parse_args()

inv = json.loads(subprocess.run([str(LAB / "lab.sh"), "inventory"], capture_output=True, text=True, check=True).stdout)
lgs = lg_nodes(inv)
if not lgs: sys.exit("no looking glass in lab.conf (role lg)")
node = next((n for n in lgs if n["name"] == a.node), lgs[0])
cfg = lg_app_config(inv, node["name"])
HOST, PORT = node["mgmt_ip"], cfg["listen"]["port"]
USER, PASS = "lab", "lab"


def api(path, timeout=10):
    with urllib.request.urlopen(f"http://{HOST}:{PORT}{path}", timeout=timeout) as r: return json.loads(r.read())


if a.status:
    try: s = api("/api/status")
    except Exception as e: sys.exit(f"the looking glass at http://{HOST}:{PORT} does not answer: {e.__class__.__name__}: {e}")   # noqa: BLE001
    print(f"{s['node']} ({s['lab']}) — up {s['uptime'] / 3600:.1f} h, AS {s['collector']['asn']}, router-id {s['collector']['router_id']}")
    for pr in s["collector"]["peers"]:
        afs = ", ".join(f"{af}: {d['accepted']}" for af, d in (pr.get("families") or {}).items() if d.get("accepted") is not None)
        print(f"  session {pr['name'] or pr['ip']:<10} {pr['state']:<12} {afs}")
    for c in sorted(s["counts"], key=lambda c: (c["source"], c["afi"], c["vrf"] or "")):
        print(f"  {c['source']:<12} {c['afi']} {c['safi']:<8} {c['vrf'] or '-':<10} {c['n']:>4} paths, {c['prefixes']:>4} prefixes")
    bad = [p2 for p2 in s["polls"].values() if not p2["ok"]]
    print(f"  history: {s['db']['events']} events, {s['db']['paths']} live paths, {s['db']['db_bytes'] / 1e6:.1f} MB"
          f" — churn 1 h: {s['churn']['1h']}")
    if bad: print("  failing polls: " + ", ".join(f"{b['source']} ({b['error']})" for b in bad))
    sys.exit(0)

if a.exec_:
    import paramiko
    c = paramiko.SSHClient(); c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    c.connect(HOST, username=USER, password=PASS, timeout=30, look_for_keys=False, allow_agent=False)
    _, out, err = c.exec_command(a.exec_, timeout=300)
    print(out.read().decode(errors="replace") + err.read().decode(errors="replace"), end="")
    rc = out.channel.recv_exit_status(); c.close(); sys.exit(rc)

FILES = {f"/opt/lgd/{f.name}": (LAB / "lg" / f.name).read_bytes() for f in sorted((LAB / "lg").glob("*.py"))}
FILES.update({f"/opt/lgd/static/{f.name}": f.read_bytes() for f in sorted((LAB / "lg" / "static").glob("*"))})
FILES["/etc/lgd/lgd.json"] = (json.dumps(cfg, indent=1) + "\n").encode()
FILES["/etc/frr/frr.conf"] = render_lg(inv, node["name"]).encode()
FILES["/etc/init.d/lgd"] = (LAB / "lg" / "lgd.openrc").read_bytes()

if a.dry_run:
    for path, data in FILES.items(): print(f"{path:<34} {len(data):>7} bytes  {hashlib.sha256(data).hexdigest()[:12]}")
    sys.exit(0)

import paramiko                                                    # noqa: E402
c = paramiko.SSHClient(); c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
c.connect(HOST, username=USER, password=PASS, timeout=30, look_for_keys=False, allow_agent=False)


def run(cmd, check=True):
    # `rc-service ... restart` leaves the daemon holding the channel's stdout, and the read would block until the timeout:
    # send the output to a file and print that instead, so the command returns as soon as OpenRC is done
    if "rc-service" in cmd: cmd = f"({cmd}) >/tmp/.lg-svc 2>&1 </dev/null; rc=$?; cat /tmp/.lg-svc; exit $rc"
    _, out, err = c.exec_command(cmd, timeout=180)
    text = out.read().decode(errors="replace") + err.read().decode(errors="replace"); rc = out.channel.recv_exit_status()
    if check and rc != 0: sys.exit(f"[{node['name']}] {cmd}: rc={rc}\n{text}")
    return rc, text


sftp = c.open_sftp()
run("doas install -d -m 755 /opt/lgd /opt/lgd/static /etc/lgd /var/lib/lgd && doas chown -R lab /opt/lgd /etc/lgd /var/lib/lgd")
changed, frr_changed = [], False
for path, data in FILES.items():
    want = hashlib.sha256(data).hexdigest()
    rc, have = run(f"doas sha256sum {path} 2>/dev/null | cut -d' ' -f1", check=False)
    if have.strip() == want: continue
    tmp = f"/tmp/.lg-{Path(path).name}"
    with sftp.open(tmp, "wb") as f: f.write(data)
    run(f"doas install -m {'0755' if path.startswith('/etc/init.d') else '0644'} {tmp} {path} && rm -f {tmp}")
    changed.append(path); frr_changed |= path == "/etc/frr/frr.conf"
sftp.close()
run("doas chown -R frr:frr /etc/frr/frr.conf", check=False)
print(f"[{node['name']}] {len(changed)} file(s) updated" + (": " + ", ".join(changed) if changed else " (already in sync)"))

if frr_changed or a.restart_frr:
    run("doas rc-update add frr default >/dev/null 2>&1; doas rc-service frr restart")
    print(f"[{node['name']}] FRR restarted ({'configuration changed' if frr_changed else 'asked for'})")
else:
    run("doas rc-service frr status >/dev/null 2>&1 || doas rc-service frr start", check=False)

if not a.no_restart:
    run("doas rc-update add lgd default >/dev/null 2>&1; doas rc-service lgd restart")
    print(f"[{node['name']}] lgd restarted — http://{HOST}:{PORT}/")
c.close()

for _ in range(30):                                                # wait for the first collection before reporting
    try:
        s = api("/api/status", timeout=5)
        if s["counts"]: break
    except Exception: pass                                         # noqa: BLE001
    time.sleep(2)
subprocess.run([sys.executable, str(LAB / "tools" / "lg_deploy.py"), "--status"] + (["--node", a.node] if a.node else []))
