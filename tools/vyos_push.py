#!/usr/bin/env python3
"""Apply a file of VyOS `set` lines over SSH (netmiko): configure, set*, commit, save. Idempotent — VyOS only commits
the difference.   vyos_push.py HOST FILE"""
import os, re, sys
from netmiko import ConnectHandler

host, path = sys.argv[1], sys.argv[2]
lines = [l.strip() for l in open(path) if l.strip() and not l.startswith("#")]
c = ConnectHandler(device_type="vyos", host=host, username=os.environ.get("VYOS_USERNAME", "vyos"), password=os.environ.get("VYOS_PASSWORD", "vyos"))
try:
    out = c.send_config_set(lines + ["commit", "save"], exit_config_mode=True, cmd_verify=False, read_timeout=240)
finally:
    c.disconnect()
bad = [l for l in out.splitlines() if re.search(r"Invalid|Commit failed|is not valid|Error", l)]
if bad: print("FAILED:", *bad, sep="\n  "); sys.exit(1)
print("no changes" if "No configuration changes to commit" in out else f"applied {len(lines)} lines, committed and saved")
