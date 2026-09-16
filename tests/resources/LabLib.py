"""Robot Framework keyword library for the SRv6 core lab: VyOS nodes over SSH (netmiko / paramiko for shell
commands), CirrOS hosts over SSH (paramiko, password auth), and host-side helpers."""
import ipaddress
import os
import re
import socket
import subprocess
import time
from pathlib import Path

import paramiko
from netmiko import ConnectHandler
from robot.api import logger
from robot.api.deco import keyword, library

LAB_DIR = Path(__file__).resolve().parents[2]
VYOS_USER, VYOS_PASS = os.environ.get("VYOS_USERNAME", "vyos"), os.environ.get("VYOS_PASSWORD", "vyos")
CIRROS_USER, CIRROS_PASS = os.environ.get("CIRROS_USERNAME", "cirros"), os.environ.get("CIRROS_PASSWORD", "gocubsgo")


@library(scope="GLOBAL")
class LabLib:
    def __init__(self):
        self._ssh = {}      # host -> netmiko connection (VyOS operational mode)
        self._bg = {}       # handle -> (client, stdout channel) for background commands

    # ---- VyOS ------------------------------------------------------------------
    def _conn(self, host):
        if host not in self._ssh:
            self._ssh[host] = ConnectHandler(device_type="vyos", host=host, username=VYOS_USER, password=VYOS_PASS)
        return self._ssh[host]

    @keyword
    def run_vyos_command(self, host, command, timeout=90):
        """Run an operational-mode command on a VyOS node and return its output."""
        out = self._conn(host).send_command(command, read_timeout=float(timeout))
        logger.info(f"<pre>{host}$ {command}\n{out}</pre>", html=True)
        return out

    @keyword
    def vyos_shell(self, host, command, timeout=90):
        """Run a Linux shell command on a VyOS node (e.g. `sudo ip -6 route`) and return stdout+stderr; fails on non-zero rc."""
        c = paramiko.SSHClient(); c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        c.connect(host, username=VYOS_USER, password=VYOS_PASS, timeout=20, look_for_keys=False, allow_agent=False)
        try:
            _, out, err = c.exec_command(command, timeout=float(timeout))
            rc = out.channel.recv_exit_status(); text = out.read().decode() + err.read().decode()
        finally:
            c.close()
        logger.info(f"<pre>{host}# {command}\nrc={rc}\n{text}</pre>", html=True)
        if rc != 0: raise AssertionError(f"'{command}' on {host} failed rc={rc}: {text.strip()[-300:]}")
        return text

    @keyword
    def close_all_connections(self):
        for c in self._ssh.values():
            try: c.disconnect()
            except Exception: pass
        self._ssh.clear()

    # ---- CirrOS hosts --------------------------------------------------------------
    def _host_exec(self, host, command, timeout):
        c = paramiko.SSHClient(); c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        c.connect(host, username=CIRROS_USER, password=CIRROS_PASS, timeout=20, look_for_keys=False, allow_agent=False)
        try:
            _, out, err = c.exec_command(command, timeout=float(timeout))
            rc = out.channel.recv_exit_status(); text = out.read().decode() + err.read().decode()
        finally:
            c.close()
        logger.info(f"<pre>{host}$ {command}\nrc={rc}\n{text}</pre>", html=True)
        return rc, text

    @keyword
    def host_command(self, host, command, timeout=60):
        """Run a shell command on a CirrOS host; returns the output, fails on non-zero rc."""
        rc, text = self._host_exec(host, command, timeout)
        if rc != 0: raise AssertionError(f"'{command}' on {host} failed rc={rc}: {text.strip()[-300:]}")
        return text

    @keyword
    def host_command_rc(self, host, command, timeout=60):
        """Run a command on a CirrOS host and return its exit code (never fails)."""
        return self._host_exec(host, command, timeout)[0]

    # ---- background commands (tcpdump while something else happens) ----------------------
    @keyword
    def start_background(self, host, command, user=VYOS_USER, password=VYOS_PASS):
        """Start a command over SSH and return a handle; collect its output with `Finish Background`."""
        c = paramiko.SSHClient(); c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        c.connect(host, username=user, password=password, timeout=20, look_for_keys=False, allow_agent=False)
        _, out, err = c.exec_command(command); handle = f"{host}:{len(self._bg) + 1}"
        self._bg[handle] = (c, out, err, command); time.sleep(1.5)   # let tcpdump attach before the traffic starts
        return handle

    @keyword
    def finish_background(self, handle, timeout=60):
        c, out, err, command = self._bg.pop(handle)
        out.channel.settimeout(float(timeout))
        try:
            text = out.read().decode() + err.read().decode()
        finally:
            c.close()
        logger.info(f"<pre>[background {handle}] {command}\n{text}</pre>", html=True)
        return text

    # ---- helpers -------------------------------------------------------------------------
    @keyword
    def ip_in_network(self, address, prefix):
        ok = ipaddress.ip_address(address.split("/")[0]) in ipaddress.ip_network(prefix, strict=False)
        if not ok: raise AssertionError(f"{address} is not inside {prefix}")
        return True

    @keyword
    def regex_findall(self, text, pattern):
        return re.findall(pattern, text, re.M)

    @keyword
    def host_ping(self, target, count=3):
        """ICMP ping from the lab host; fails unless at least one reply."""
        r = subprocess.run(["ping", "-c", str(count), "-W", "2", target], capture_output=True, text=True)
        logger.info(r.stdout)
        if r.returncode != 0: raise AssertionError(f"no ICMP reply from {target}")

    @keyword
    def tcp_port_should_be_open(self, host, port, timeout=5):
        with socket.socket() as s:
            s.settimeout(float(timeout))
            try: s.connect((host, int(port)))
            except OSError as e: raise AssertionError(f"{host}:{port} not reachable: {e}")
