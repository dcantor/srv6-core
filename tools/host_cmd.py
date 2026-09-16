#!/usr/bin/env python3
"""CirrOS hosts over SSH (paramiko, cirros/gocubsgo; dropbear, password auth only).
   host_cmd.py run HOST CMD          run a command on one host (HOST = name from lab.conf or an address)
   host_cmd.py matrix [host ...]     ping every host from every other host over the tenant LANs and print the matrix"""
import json, os, subprocess, sys
from pathlib import Path
import paramiko

LAB_DIR = Path(__file__).resolve().parents[1]
USER, PASS = os.environ.get("CIRROS_USERNAME", "cirros"), os.environ.get("CIRROS_PASSWORD", "gocubsgo")


def inventory():
    return json.loads(subprocess.run([str(LAB_DIR / "lab.sh"), "inventory"], capture_output=True, text=True, check=True).stdout)


def run(host, cmd, timeout=60):
    c = paramiko.SSHClient(); c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    c.connect(host, username=USER, password=PASS, timeout=20, look_for_keys=False, allow_agent=False)
    try:
        _, out, err = c.exec_command(cmd, timeout=timeout)
        rc = out.channel.recv_exit_status(); text = out.read().decode() + err.read().decode()
    finally:
        c.close()
    return rc, text


if sys.argv[1] == "run":
    inv = {n["name"]: n for n in inventory()["nodes"]}
    host = inv[sys.argv[2]]["mgmt_ip"] if sys.argv[2] in inv else sys.argv[2]
    rc, text = run(host, " ".join(sys.argv[3:])); print(text, end=""); sys.exit(rc)
elif sys.argv[1] == "matrix":
    inv = {n["name"]: n for n in inventory()["nodes"] if n["role"] == "host"}
    names = sys.argv[2:] or sorted(inv)
    lan_ip = {n: inv[n]["ports"][0]["ip"].split("/")[0] for n in names}
    ok = total = 0
    print(f"{'from \\ to':10s}" + "".join(f"{t:>10s}" for t in names))
    for src in names:
        row = f"{src:10s}"
        for dst in names:
            if src == dst: row += f"{'-':>10s}"; continue
            total += 1
            try:
                rc, text = run(inv[src]["mgmt_ip"], f"ping -c 3 -W 2 {lan_ip[dst]}", timeout=30)
                loss = next((l for l in text.splitlines() if "packet loss" in l), "")
                pct = loss.split("%")[0].split()[-1] if loss else "?"
                cell = "ok" if rc == 0 else f"{pct}% loss"; ok += rc == 0
            except Exception as e:  # noqa: BLE001
                cell = e.__class__.__name__
            row += f"{cell:>10s}"
        print(row)
    print(f"{ok}/{total} reachable"); sys.exit(0 if ok == total else 1)
else:
    sys.exit(__doc__)
