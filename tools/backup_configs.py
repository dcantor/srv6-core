#!/usr/bin/env python3
"""Commit the lab's configurations to the local Gitea (the NMS, http://10.0.0.10:3000, repo lab/srv6-core-configs):
   running/<node>.config.txt     `show configuration commands` of every VyOS node (the backup of record)
   intended/<node>.config.txt    the rendered day-0 configuration (nodes/<node>/vyos_config.txt)
   routes/<node>.routes.txt      routing tables per node (same capture as the test runs; hosts included)
   hosts/<host>.txt              Alpine host addresses and routes
The repo is created on first use; a local clone lives in .gitea-backup/ (ignored). Credentials come from GITEA_USER /
GITEA_PASSWORD or, by default, the NMS's /opt/nautobot/.env over SSH.      backup_configs.py [-m "message"]"""
import argparse, os, shutil, subprocess, sys
from datetime import datetime
from pathlib import Path
import requests

LAB = Path(__file__).resolve().parents[1]; CLONE = LAB / ".gitea-backup"
p = argparse.ArgumentParser(); p.add_argument("-m", "--message", default=None); p.add_argument("--gitea", default=os.environ.get("GITEA_URL", "http://10.0.0.10:3000"))
p.add_argument("--repo", default=os.environ.get("GITEA_REPO", "srv6-core-configs")); a = p.parse_args()
user = os.environ.get("GITEA_USER", "lab"); pw = os.environ.get("GITEA_PASSWORD")
if not pw:
    pw = subprocess.run(["ssh", "-o", "StrictHostKeyChecking=no", "-o", "UserKnownHostsFile=/dev/null", "-o", "LogLevel=ERROR", "lab@10.0.0.10",
                         "grep ^GITEA_PASSWORD /opt/nautobot/.env | cut -d= -f2"], capture_output=True, text=True, timeout=30).stdout.strip()
if not pw: sys.exit("no Gitea password (set GITEA_PASSWORD or make lab@10.0.0.10 reachable)")
auth = (user, pw)

# the repo (create on first use)
r = requests.get(f"{a.gitea}/api/v1/repos/{user}/{a.repo}", auth=auth, timeout=30)
if r.status_code == 404:
    requests.post(f"{a.gitea}/api/v1/user/repos", auth=auth, json={"name": a.repo, "description": "SRv6 core lab: running and intended VyOS configurations, routing tables (committed by tools/backup_configs.py)",
                                                                   "auto_init": True, "default_branch": "main", "private": False}, timeout=30).raise_for_status(); print(f"created {user}/{a.repo} on Gitea")
url = a.gitea.replace("://", f"://{user}:{pw}@") + f"/{user}/{a.repo}.git"
git = lambda *args: subprocess.run(["git", "-C", str(CLONE), *args], capture_output=True, text=True)
if not (CLONE / ".git").exists():
    shutil.rmtree(CLONE, ignore_errors=True); subprocess.run(["git", "clone", "-q", url, str(CLONE)], check=True)
else:
    git("remote", "set-url", "origin", url); git("pull", "-q", "--rebase")

# capture (same code as the test runs), then lay the files out
sys.path.insert(0, str(LAB / "tests" / "resources"))
cap = CLONE / ".capture"; shutil.rmtree(cap, ignore_errors=True)
subprocess.run([sys.executable, str(LAB / "tests" / "capture_configs.py"), str(cap)], check=True, stdout=subprocess.DEVNULL)
for d in ("running", "intended", "routes", "hosts"): shutil.rmtree(CLONE / d, ignore_errors=True); (CLONE / d).mkdir()
for f in cap.glob("*.config.txt"): shutil.copy(f, CLONE / "running" / f.name)
for f in (cap / "routes").glob("*.routes.txt"):
    node = f.name.split(".")[0]; shutil.copy(f, (CLONE / ("hosts" if node.startswith("dc") and "-h" in node else "routes")) / f.name)
for f in LAB.glob("nodes/*/vyos_config.txt"): shutil.copy(f, CLONE / "intended" / f"{f.parent.name}.config.txt")
shutil.rmtree(cap)
(CLONE / "README.md").write_text(f"# srv6-core configurations\n\nCommitted by `tools/backup_configs.py` from the lab host. `running/` = `show configuration commands` of every VyOS node, "
                                 f"`intended/` = the rendered day-0 configuration (what Nautobot / lab.conf say the node should run), `routes/` = routing tables, `hosts/` = the Alpine tenant hosts.\n\nSource: https://github.com/dcantor/srv6-core\n")
git("add", "-A"); st = git("status", "--porcelain").stdout
if not st.strip(): print("no changes since the last backup"); sys.exit()
msg = a.message or f"lab backup {datetime.now():%Y-%m-%d %H:%M}"
git("-c", "user.name=lab", "-c", "user.email=lab@lab.local", "commit", "-q", "-m", msg); push = git("push", "-q", "origin", "HEAD:main")
if push.returncode: sys.exit(push.stderr[-500:])
changed = [l[3:] for l in st.splitlines()]
print(f"pushed '{msg}' to {a.gitea}/{user}/{a.repo}: {len(changed)} file(s) changed" + (" — " + ", ".join(changed[:8]) + (" …" if len(changed) > 8 else "")))
