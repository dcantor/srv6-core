#!/usr/bin/env bash
# Build images/lg.qcow2 — the base image of the BGP looking glass: Alpine (NoCloud cloud-init image) + FRR (the route
# collector), Python with Flask / waitress / paramiko (the lgd service), sqlite and node-exporter. Like the tenant hosts,
# a throw-away builder VM on the libvirt NAT network installs the packages, cleans cloud-init and powers off; its disk
# becomes the base image. Nothing lab-specific is baked in: the configuration and the service come from tools/lg_deploy.py,
# so iterating on the looking glass never needs a new image.   tools/build_lg_image.sh [alpine.qcow2]
set -euo pipefail
LAB="$(cd "$(dirname "$(readlink -f "$0")")/.." && pwd)"; SRC="${1:-$LAB/images/alpine-3.22.4-nocloud.qcow2}"; OUT="$LAB/images/lg.qcow2"
V() { virsh -q -c qemu:///system "$@"; }
B="$LAB/images/.lg-builder"; rm -rf "$B"; mkdir -p "$B"
qemu-img create -q -f qcow2 -b "$SRC" -F qcow2 "$B/disk.qcow2" 4G
cat > "$B/user-data" <<'U'
#cloud-config
package_update: true
packages: [frr, frr-openrc, sudo, py3-flask, py3-waitress, py3-paramiko, sqlite, tcpdump, curl, prometheus-node-exporter]
users:
  - name: lab
    plain_text_passwd: lab
    lock_passwd: false
    sudo: ALL=(ALL) NOPASSWD:ALL
    shell: /bin/sh
ssh_pwauth: true
runcmd:
  # bgpd is the only daemon the collector needs; zebra comes along as FRR's RIB manager but installs nothing (no VRFs here)
  - sed -i 's/^bgpd=.*/bgpd=yes/' /etc/frr/daemons
  - install -d -o frr -g frr /var/log/frr /var/lib/lgd
  - rc-update add frr default
  - rc-update add sshd default
  - sed -i 's/^#\?PasswordAuthentication .*/PasswordAuthentication yes/' /etc/ssh/sshd_config
  - python3 -c "import flask, waitress, paramiko, sqlite3; print('lg deps ok', flask.__version__)"
  - cloud-init clean --logs
  - poweroff
U
printf 'instance-id: lg-builder-001\nlocal-hostname: lg-builder\n' > "$B/meta-data"
genisoimage -quiet -o "$B/seed.iso" -V cidata -J -r "$B/user-data" "$B/meta-data"
cat > "$B/domain.xml" <<X
<domain type='kvm'><name>lg-image-builder</name><memory unit='MiB'>1024</memory><vcpu>2</vcpu><cpu mode='host-passthrough' check='none'/>
<os><type arch='x86_64' machine='pc'>hvm</type><boot dev='hd'/></os><features><acpi/><apic/></features><on_poweroff>destroy</on_poweroff>
<devices><emulator>/usr/bin/qemu-system-x86_64</emulator>
<disk type='file' device='disk'><driver name='qemu' type='qcow2'/><source file='$B/disk.qcow2'/><target dev='vda' bus='virtio'/></disk>
<disk type='file' device='cdrom'><driver name='qemu' type='raw'/><source file='$B/seed.iso'/><target dev='hda' bus='ide'/><readonly/></disk>
<interface type='network'><source network='default'/><model type='virtio'/></interface>
<serial type='tcp'><source mode='bind' host='127.0.0.1' service='5399'/><protocol type='raw'/><log file='$B/console.log' append='on'/><target port='0'/></serial><memballoon model='none'/></devices></domain>
X
# pre-create the log so virtlogd appends to our file instead of a root-only one (as lab.sh does for the nodes)
touch "$B/console.log"; chmod 644 "$B/console.log"
V destroy lg-image-builder >/dev/null 2>&1 || true; V undefine lg-image-builder >/dev/null 2>&1 || true
V define "$B/domain.xml" >/dev/null; V start lg-image-builder >/dev/null
echo "builder started (NAT network, installing FRR / Flask / waitress / paramiko / node-exporter) ..."
for _ in $(seq 150); do [[ "$(V domstate lg-image-builder)" == "running" ]] || break; sleep 5; done
[[ "$(V domstate lg-image-builder)" == "running" ]] && { echo "builder still running after 12 min — see $B/console.log" >&2; exit 1; }
grep -q "lg deps ok" "$B/console.log" || { echo "error: the dependency check did not print 'lg deps ok' — see $B/console.log" >&2; exit 1; }
V undefine lg-image-builder >/dev/null
qemu-img convert -O qcow2 -c "$B/disk.qcow2" "$OUT" && rm -rf "$B"
echo "built $OUT ($(du -h "$OUT" | cut -f1)); user lab / lab, FRR + Flask, configured by tools/lg_deploy.py"
