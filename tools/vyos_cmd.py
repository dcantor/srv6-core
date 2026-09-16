#!/usr/bin/env python3
"""Run operational commands on a VyOS node over SSH (netmiko, vyos/vyos).   vyos_cmd.py HOST CMD [CMD ...]"""
import os, sys
from netmiko import ConnectHandler

host, cmds = sys.argv[1], sys.argv[2:]
c = ConnectHandler(device_type="vyos", host=host, username=os.environ.get("VYOS_USERNAME", "vyos"), password=os.environ.get("VYOS_PASSWORD", "vyos"))
try:
    for cmd in cmds:
        out = c.send_command(cmd, read_timeout=90)
        if len(cmds) > 1: print(f"$ {cmd}")
        print(out)
finally:
    c.disconnect()
