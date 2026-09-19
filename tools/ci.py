#!/usr/bin/env python3
"""CI plumbing: the local Gitea keeps a pull mirror of the GitHub repository and runs .gitea/workflows on it.
   ci.py setup     create lab/srv6-core in Gitea as a mirror of github.com/dcantor/srv6-core (Actions enabled) if missing
   ci.py sync      ask Gitea to sync the mirror now (what `lab.sh push` does after pushing to GitHub) and, if the sync
                   does not start a workflow run by itself, dispatch lab-ci.yml on main
   ci.py status    the last workflow runs
Nothing is stored on this side: GitHub stays the only git remote; Gitea credentials are read at run time as
backup_configs.py does (GITEA_USER / GITEA_PASSWORD, or the password from lab@10.0.0.10)."""
import argparse, os, subprocess, sys, time
import requests

p = argparse.ArgumentParser(); p.add_argument("cmd", choices=["setup", "sync", "status"])
p.add_argument("--gitea", default=os.environ.get("GITEA_URL", "http://10.0.0.10:3000")); p.add_argument("--repo", default=os.environ.get("GITEA_CI_REPO", "srv6-core"))
p.add_argument("--github", default="https://github.com/dcantor/srv6-core.git"); p.add_argument("--workflow", default="lab-ci.yml"); a = p.parse_args()
user = os.environ.get("GITEA_USER", "lab"); pw = os.environ.get("GITEA_PASSWORD")
if not pw:
    pw = subprocess.run(["ssh", "-o", "StrictHostKeyChecking=no", "-o", "UserKnownHostsFile=/dev/null", "-o", "LogLevel=ERROR", "lab@10.0.0.10",
                         "grep ^GITEA_PASSWORD /opt/nautobot/.env | cut -d= -f2"], capture_output=True, text=True, timeout=30).stdout.strip()
if not pw: sys.exit("no Gitea password (set GITEA_PASSWORD or make lab@10.0.0.10 reachable)")
auth = (user, pw); api = f"{a.gitea}/api/v1"; repo = f"{api}/repos/{user}/{a.repo}"


def runs(limit=10):   # Gitea 1.24 lists jobs ("tasks"), one per job of a run
    return requests.get(f"{repo}/actions/tasks", auth=auth, params={"limit": limit}, timeout=30).json().get("workflow_runs", [])


if a.cmd == "setup":
    r = requests.get(repo, auth=auth, timeout=30)
    if r.status_code == 404:
        requests.post(f"{api}/repos/migrate", auth=auth, timeout=300, json={"clone_addr": a.github, "repo_name": a.repo, "repo_owner": user, "service": "git", "mirror": True, "mirror_interval": "10m",
                      "description": f"SRv6 core lab — pull mirror of {a.github}; CI (.gitea/workflows/{a.workflow}) runs here on the lab host"}).raise_for_status()
        print(f"created mirror {a.gitea}/{user}/{a.repo} <- {a.github}")
    else: r.raise_for_status(); print(f"exists: {a.gitea}/{user}/{a.repo}")
    requests.patch(repo, auth=auth, json={"has_actions": True, "has_wiki": False, "has_projects": False, "has_packages": False, "has_issues": False, "has_pull_requests": False}, timeout=30).raise_for_status()
    print(f"workflow runs: {a.gitea}/{user}/{a.repo}/actions")
elif a.cmd == "sync":
    before = {r["id"] for r in runs()}
    requests.post(f"{repo}/mirror-sync", auth=auth, timeout=30).raise_for_status(); print("mirror sync requested")
    for _ in range(12):
        time.sleep(5); new = [r for r in runs() if r["id"] not in before]
        if new: print(f"workflow run started: {a.gitea}/{user}/{a.repo}/actions/runs/{new[0].get('run_number', '')}"); sys.exit()
    head = requests.get(f"{repo}/branches/main", auth=auth, timeout=30).json()["commit"]["id"]
    r = requests.post(f"{repo}/actions/workflows/{a.workflow}/dispatches", auth=auth, json={"ref": "main"}, timeout=30)
    print(f"mirror at {head[:8]}; workflow dispatched ({r.status_code})" if r.status_code < 300 else f"dispatch failed: {r.status_code} {r.text[:200]}")
else:
    rr = runs()
    for r in rr: print(f"{r['created_at'][:19]}  run {r.get('run_number', '?'):>3}  {r['status']:10s} {r.get('head_branch', ''):6s} {r.get('head_sha', '')[:8]}  {r.get('name') or ''}")
    if not rr: print("no runs yet")
