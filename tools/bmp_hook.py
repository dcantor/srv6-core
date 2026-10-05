#!/usr/bin/env python3
"""BMP from the route reflectors to the looking glass, for the VPN address families — what VyOS's CLI cannot express.

FRR 10.6 monitors VPNv4 / VPNv6 over BMP (`bmp monitor ipv4|ipv6 vpn pre-policy|loc-rib`), but VyOS's CLI only offers
the unicast families, and every commit re-renders bgpd without anything else. And a BMP target committed while bgpd
still runs without the module (`system frr bmp` takes effect only once bgpd restarts) fails the whole BGP commit — on a
fresh reflector, its sessions to the PEs with it. So the rendered configuration only loads the module, and this tool:

  1. restarts bgpd once if it runs without `-M bmp` (one reflector at a time, waiting for its PEs to come back);
  2. removes the looking glass's iBGP neighbour, if the reflector still has it from LG_FEED=session;
  3. installs a commit hook (/config/scripts/commit/post-hooks.d/, persistent, run after every commit) that puts the
     BMP target and its monitors back whenever a commit has taken them away — and bounces the BMP connection when it
     does, because a monitor added to a live connection triggers no table dump; the boot script runs it too;
  4. runs it now.

With LG_FEED=session it undoes all of that (hook, target, module, the PEs' kept Adj-RIB-In); `lab.sh configure` then
puts the iBGP session back. Idempotent; run by `lab.sh configure`.
   bmp_hook.py [node ...]"""
import io, json, subprocess, sys, time
from pathlib import Path
import paramiko
from netmiko import ConnectHandler

LAB = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(LAB / "tools")); from render import lg_feed, lg_bmp_port, lg_nodes   # noqa: E402

HOOK = "/config/scripts/commit/post-hooks.d/srv6-core-bmp"
BOOT = "/config/scripts/vyos-postconfig-bootup.script"


def hook_script(asn, target, address, port):
    monitors = [f"bmp monitor {afi} vpn {pol}" for afi in ("ipv4", "ipv6") for pol in ("pre-policy", "loc-rib")]
    connect = f"bmp connect {address} port {port} min-retry 1000 max-retry 10000"
    want = "\n".join(f"  {m}" for m in monitors + [connect])
    cmds = " ".join(f"-c '{c}'" for c in ["configure terminal", f"router bgp {asn}", f"bmp targets {target}", *monitors, connect, "end"])
    return f"""#!/bin/sh
# srv6-core lab: BMP to the looking glass for the VPN families — installed by tools/bmp_hook.py, run after every commit
# and at boot. VyOS's CLI offers only unicast BMP monitors and each commit re-renders bgpd without these lines, so put
# them back when they are missing. A monitor added to a live BMP connection triggers no table dump: recreate the target
# so the reflector connects afresh and replays its tables.
V=vtysh; [ "$(id -u)" = 0 ] || V="sudo -n vtysh"
ps -o args= -C bgpd | grep -q -- '-M bmp' || exit 0     # module not loaded yet: bmp_hook.py restarts bgpd once
have=$($V -c 'show running-config bgpd' 2>/dev/null) || exit 0
missing=0
while IFS= read -r line; do printf '%s\\n' "$have" | grep -qxF -- "$line" || missing=1; done <<'EOF'
{want}
EOF
[ "$missing" = 0 ] && exit 0
$V -c 'configure terminal' -c 'router bgp {asn}' -c 'no bmp targets {target}' -c 'end' >/dev/null 2>&1
$V {cmds} >/dev/null && logger -t srv6-core-bmp "BMP target {target} ({address} port {port}) restored"
"""


class Router:
    def __init__(self, n):
        self.n = n; self.name = n["name"]
        self.c = paramiko.SSHClient(); self.c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        self.c.connect(n["mgmt_ip"], username="vyos", password="vyos", timeout=20, look_for_keys=False, allow_agent=False)

    def run(self, cmd, stdin=None, timeout=120):
        i, o, e = self.c.exec_command(cmd, timeout=timeout)
        if stdin: i.write(stdin); i.flush(); i.channel.shutdown_write()
        out = o.read().decode(); err = e.read().decode()
        return out, err, o.channel.recv_exit_status()

    def put(self, path, text, mode="755"):
        sftp = self.c.open_sftp(); sftp.putfo(io.BytesIO(text.encode()), "/tmp/.srv6-core-put"); sftp.close()
        _, err, rc = self.run(f"sudo mkdir -p {Path(path).parent} && sudo install -m {mode} -o root -g vyattacfg /tmp/.srv6-core-put {path}")
        if rc: raise RuntimeError(err)

    def vyos_config(self, lines):
        """delete / set lines in configuration mode, then commit and save (netmiko: VyOS's shell is not vbash over exec)."""
        if not lines: return
        c = ConnectHandler(device_type="vyos", host=self.n["mgmt_ip"], username="vyos", password="vyos")
        try: out = c.send_config_set(lines + ["commit", "save"], exit_config_mode=True, cmd_verify=False, read_timeout=240)
        finally: c.disconnect()
        if "Commit failed" in out: raise RuntimeError(out[-800:])

    def bmp_loaded(self):
        return "-M bmp" in self.run("ps -o args= -C bgpd")[0]

    def established(self):
        out, _, _ = self.run("sudo vtysh -c 'show bgp ipv4 vpn summary json'")
        try: peers = json.loads(out).get("peers", {})
        except ValueError: return 0, 0
        return sum(1 for p in peers.values() if p.get("state") == "Established"), len(peers)

    def restart_bgpd(self, pes):
        """bgpd with the BMP module: restart it (VyOS's own op-mode script; it asks for confirmation on stdin) and wait
        for every PE to be back, so the next reflector is only touched once this one reflects again."""
        # output to a file, not the SSH channel: the restarted daemons inherit it and the channel would never close
        out, err, rc = self.run("printf 'y\\ny\\ny\\ny\\n' | sudo /usr/libexec/vyos/op_mode/restart_frr.py --action restart --daemon bgpd"
                                " >/tmp/srv6-core-bgpd-restart.log 2>&1; tail -3 /tmp/srv6-core-bgpd-restart.log", timeout=180)
        deadline = time.time() + 180
        while time.time() < deadline:
            time.sleep(5)
            if self.bmp_loaded() and self.established()[0] >= pes: return
        raise RuntimeError(f"bgpd restarted, but not every PE is back after 3 minutes: {self.established()}; {out[-300:]}{err[-300:]}")


def main():
    inv = json.loads(subprocess.run([str(LAB / "lab.sh"), "inventory"], capture_output=True, text=True, check=True).stdout)
    lgs = lg_nodes(inv)
    if not lgs: return
    N = {n["name"]: n for n in inv["nodes"]}; svc = inv["service"]; feed = lg_feed(inv); port = lg_bmp_port(inv)
    pes = sum(1 for n in inv["nodes"] if n["role"] == "pe")
    only = set(sys.argv[1:])
    for rr in svc["rrs"]:
        n = N[rr]
        if only and rr not in only: continue
        links = [(p, N[p["peer"]]) for p in n["ports"] if p["peer"] and N[p["peer"]]["role"] == "lg"]
        if not links: continue
        p, lg = links[0]
        net = __import__("ipaddress").ip_interface(p["ip"]); base = net.network.network_address
        lg_ip = str(base + 2 if net.ip == base + 1 else base + 1)
        r = Router(n)
        try:
            if feed == "bmp":
                if r.run("/bin/cli-shell-api existsActive system frr bmp")[2] != 0:   # normally rendered and pushed already
                    r.vyos_config(["set system frr bmp"])
                if not r.bmp_loaded():
                    print(f"[{rr}] restarting bgpd to load the BMP module (the other reflector keeps the VPNs up)", flush=True)
                    r.restart_bgpd(pes)
                cfg, _, _ = r.run("/bin/cli-shell-api showCfg protocols bgp neighbor 2>/dev/null; true")
                if lg_ip in cfg:
                    print(f"[{rr}] removing the looking glass's iBGP session ({lg_ip}): it is fed over BMP now", flush=True)
                    r.vyos_config([f"delete protocols bgp neighbor {lg_ip}"])
                r.put(HOOK, hook_script(svc["core_as"], lg["name"], lg_ip, port))
                r.run(f"grep -qxF '{HOOK}' {BOOT} || echo '{HOOK}' | sudo tee -a {BOOT} >/dev/null")
                r.run(f"sudo {HOOK}")
                out, _, _ = r.run("sudo vtysh -c 'show bmp'")
                state = next((l.split()[1] for l in out.splitlines() if l.strip().startswith(f"{lg_ip}:{port}")), "?")
                print(f"[{rr}] BMP to {lg['name']} ({lg_ip} port {port}): {state}", flush=True)
            else:
                r.run(f"sudo rm -f {HOOK}; sudo sed -i '\\#^{HOOK}$#d' {BOOT}")
                r.run(f"sudo vtysh -c 'configure terminal' -c 'router bgp {svc['core_as']}' -c 'no bmp targets {lg['name']}' -c end")
                r.vyos_config(["delete system frr bmp"] + [f"delete protocols bgp peer-group RR-CLIENTS address-family {af} soft-reconfiguration"
                                                           for af in ("ipv4-vpn", "ipv6-vpn")])
                print(f"[{rr}] BMP removed; `lab.sh configure` puts the iBGP session to {lg['name']} back", flush=True)
        finally:
            r.c.close()


if __name__ == "__main__":
    main()
