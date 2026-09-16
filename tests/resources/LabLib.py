"""Robot Framework keyword library for the SRv6 core lab: VyOS nodes over SSH (netmiko / paramiko for shell
commands), CirrOS hosts over SSH (paramiko, password auth), and host-side helpers."""
import ipaddress
import os
import re
import socket
import subprocess
import sys
import time
from pathlib import Path

import paramiko
import requests
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
    def vyos_configure(self, host, *lines, timeout=180):
        """Apply `set`/`delete` lines on a VyOS node (configure, lines, commit, save). Used by tests that change the lab
        on purpose — they must restore it in their teardown so the pre/post configuration diff stays empty."""
        out = self._conn(host).send_config_set(list(lines) + ["commit", "save"], exit_config_mode=True, cmd_verify=False, read_timeout=float(timeout))
        logger.info(f"<pre>{host}# configure\n{chr(10).join(lines)}\ncommit; save\n{out[-1500:]}</pre>", html=True)
        if re.search(r"Invalid|Commit failed|is not valid", out): raise AssertionError(f"configuration on {host} failed: {out[-500:]}")
        return out

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

    # ---- Nautobot (shared NMS, 10.3.0.10 on this lab's OOB network) ----------------------------------------
    def _nautobot(self):
        if not hasattr(self, "_nb"):
            url = os.environ.get("NAUTOBOT_URL", "http://10.0.0.10:8080"); token = os.environ.get("NAUTOBOT_TOKEN")
            if not token:
                token = subprocess.run(["ssh", "-o", "StrictHostKeyChecking=no", "-o", "UserKnownHostsFile=/dev/null", "-o", "LogLevel=ERROR", "lab@10.0.0.10",
                                        "grep ^NAUTOBOT_SUPERUSER_API_TOKEN /opt/nautobot/.env | cut -d= -f2"], capture_output=True, text=True, timeout=30).stdout.strip()
            self._nb = (url, token)
        return self._nb

    @keyword
    def nautobot_get(self, path, **params):
        url, token = self._nautobot()
        r = requests.get(f"{url}/api/{path.lstrip('/')}", params=params, timeout=60, headers={"Authorization": f"Token {token}", "Accept": "application/json"})
        logger.info(f"GET {r.url} -> {r.status_code}\n{r.text[:1500]}"); r.raise_for_status()
        return r.json()

    @keyword
    def nautobot_graphql(self, query):
        url, token = self._nautobot()
        r = requests.post(f"{url}/api/graphql/", json={"query": query}, timeout=120, headers={"Authorization": f"Token {token}"})
        logger.info(f"GraphQL {query}\n-> {r.status_code} {r.text[:2000]}"); r.raise_for_status()
        body = r.json()
        if body.get("errors"): raise AssertionError(f"GraphQL errors: {body['errors']}")
        return body["data"]

    @keyword
    def nautobot_render(self, *args, timeout=600):
        """Run nautobot/render.py with the given flags (--check / --live / --inventory); returns (rc, output)."""
        url, token = self._nautobot()
        r = subprocess.run([sys.executable, str(LAB_DIR / "nautobot" / "render.py"), *args], capture_output=True, text=True, timeout=float(timeout),
                           env={**os.environ, "NAUTOBOT_URL": url, "NAUTOBOT_TOKEN": token})
        logger.info(f"<pre>render.py {' '.join(args)}\nrc={r.returncode}\n{r.stdout[-6000:]}{r.stderr[-1500:]}</pre>", html=True)
        return r.returncode, r.stdout + r.stderr

    @keyword
    def steer(self, *args, timeout=180):
        """Run tools/steer.py (explicit-path SRv6 steering): add|del|show|sid ... — returns its output, fails on error."""
        r = subprocess.run([sys.executable, str(LAB_DIR / "tools" / "steer.py"), *args], capture_output=True, text=True, timeout=float(timeout))
        logger.info(f"<pre>steer.py {' '.join(args)}\nrc={r.returncode}\n{r.stdout}{r.stderr}</pre>", html=True)
        if r.returncode != 0: raise AssertionError(f"steer.py {' '.join(args)} failed: {(r.stderr or r.stdout).strip()[-400:]}")
        return r.stdout

    @keyword
    def ping_loss(self, text):
        """Packets lost according to a ping summary line ('N packets transmitted, M received, ...')."""
        m = re.search(r"(\d+) packets transmitted, (\d+) (?:packets )?received", text)
        if not m: raise AssertionError(f"no ping summary in: {text[-300:]}")
        return int(m[1]) - int(m[2])

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
