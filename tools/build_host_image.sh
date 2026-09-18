#!/usr/bin/env bash
# Build images/alpine-host.qcow2 — the base image of the tenant hosts: Alpine (NoCloud cloud-init image) + iperf3, tcpdump, node-exporter,
# mtr, with a `lab` user. A throw-away builder VM on the libvirt NAT network (default) installs the packages, cleans
# cloud-init and powers off; its disk becomes the base image. Re-run to rebuild.   tools/build_host_image.sh [alpine.qcow2]
set -euo pipefail
LAB="$(cd "$(dirname "$(readlink -f "$0")")/.." && pwd)"; SRC="${1:-$LAB/images/alpine-3.22.4-nocloud.qcow2}"; OUT="$LAB/images/alpine-host.qcow2"
V() { virsh -q -c qemu:///system "$@"; }
B="$LAB/images/.builder"; rm -rf "$B"; mkdir -p "$B"
qemu-img create -q -f qcow2 -b "$SRC" -F qcow2 "$B/disk.qcow2" 2G
cat > "$B/user-data" <<'U'
#cloud-config
package_update: true
packages: [iperf3, tcpdump, mtr, curl, prometheus-node-exporter]
users:
  - name: lab
    plain_text_passwd: lab
    lock_passwd: false
    sudo: ALL=(ALL) NOPASSWD:ALL
    shell: /bin/sh
ssh_pwauth: true
runcmd:
  - rc-update add sshd default
  - sed -i 's/^#\?PasswordAuthentication .*/PasswordAuthentication yes/' /etc/ssh/sshd_config
  - cloud-init clean --logs
  - poweroff
U
printf 'instance-id: builder-001\nlocal-hostname: builder\n' > "$B/meta-data"
genisoimage -quiet -o "$B/seed.iso" -V cidata -J -r "$B/user-data" "$B/meta-data"
cat > "$B/domain.xml" <<X
<domain type='kvm'><name>alpine-host-builder</name><memory unit='MiB'>512</memory><vcpu>1</vcpu><cpu mode='host-passthrough' check='none'/>
<os><type arch='x86_64' machine='pc'>hvm</type><boot dev='hd'/></os><features><acpi/><apic/></features><on_poweroff>destroy</on_poweroff>
<devices><emulator>/usr/bin/qemu-system-x86_64</emulator>
<disk type='file' device='disk'><driver name='qemu' type='qcow2'/><source file='$B/disk.qcow2'/><target dev='vda' bus='virtio'/></disk>
<disk type='file' device='cdrom'><driver name='qemu' type='raw'/><source file='$B/seed.iso'/><target dev='hda' bus='ide'/><readonly/></disk>
<interface type='network'><source network='default'/><model type='virtio'/></interface>
<serial type="file"><source path="$B/console.log"/><target port="0"/></serial><memballoon model='none'/></devices></domain>
X
touch "$B/console.log"; chmod 644 "$B/console.log"
touch "$B/console.log"; chmod 666 "$B/console.log"
V destroy alpine-host-builder >/dev/null 2>&1 || true; V undefine alpine-host-builder >/dev/null 2>&1 || true
V define "$B/domain.xml" >/dev/null; V start alpine-host-builder >/dev/null
echo "builder started (NAT network, installing iperf3 / tcpdump / mtr / curl / node-exporter) ..."
for _ in $(seq 120); do [[ "$(V domstate alpine-host-builder)" == "running" ]] || break; sleep 5; done
[[ "$(V domstate alpine-host-builder)" == "running" ]] && { echo "builder still running after 10 min — see $B/console.log" >&2; exit 1; }
grep -q "iperf3" "$B/console.log" || echo "warning: no iperf3 in the console log — check $B/console.log" >&2
V undefine alpine-host-builder >/dev/null
qemu-img convert -O qcow2 -c "$B/disk.qcow2" "$OUT" && rm -rf "$B"
echo "built $OUT ($(du -h "$OUT" | cut -f1)); user lab / lab, iperf3 installed"
