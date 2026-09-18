#!/usr/bin/env python3
"""Make FRR log routing state changes (BGP %ADJCHANGE, IS-IS adjacency and BFD session changes) to syslog on every VyOS node.

VyOS renders `log syslog notifications` into frr.conf, and FRR emits state changes at *informational* — so by default a
failover leaves no trace in syslog. VyOS switches FRR to informational only when /tmp/vyos.frr.debug exists (the
`vyos-debug` boot flag); that file is on tmpfs. This tool installs the VyOS-supported pre-config boot hook
(/config/scripts/vyos-preconfig-bootup.script, persistent) that creates the flag before the configuration is loaded, creates
it now, and sets the level live with vtysh so no reboot is needed. Idempotent; run by `lab.sh configure`.
   frr_logging.py [node ...]"""
import json, subprocess, sys
from pathlib import Path
import paramiko

LAB = Path(__file__).resolve().parents[1]
HOOK = "#!/bin/sh\n# srv6-core lab: FRR logs routing state changes (informational) — see tools/frr_logging.py\ntouch /tmp/vyos.frr.debug\n"
inv = json.loads(subprocess.run([str(LAB / "lab.sh"), "inventory"], capture_output=True, text=True, check=True).stdout)
nodes = [n for n in inv["nodes"] if n["role"] in ("pe", "p", "ce") and (not sys.argv[1:] or n["name"] in sys.argv[1:])]
for n in nodes:
    c = paramiko.SSHClient(); c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    c.connect(n["mgmt_ip"], username="vyos", password="vyos", timeout=20, look_for_keys=False, allow_agent=False)
    sftp = c.open_sftp(); sftp.putfo(__import__("io").BytesIO(HOOK.encode()), "/tmp/preconfig.script"); sftp.close()
    _, o, e = c.exec_command("sudo install -m 755 -o root -g vyattacfg /tmp/preconfig.script /config/scripts/vyos-preconfig-bootup.script && sudo touch /tmp/vyos.frr.debug"
                             " && sudo vtysh -c 'configure terminal' -c 'log syslog informational' -c 'end' >/dev/null && sudo vtysh -c 'show logging' | grep -m1 'Syslog logging'", timeout=60)
    print(f"[{n['name']}] {o.read().decode().strip() or e.read().decode().strip()[-200:]}"); c.close()
