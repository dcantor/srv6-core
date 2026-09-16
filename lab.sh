#!/usr/bin/env bash
# SRv6 WAN core lab controller (libvirt/KVM): VyOS PEs / Ps / CEs and CirrOS hosts, see lab.conf
set -euo pipefail
source "$(dirname "$(readlink -f "$0")")/lab.conf"

# Re-exec under the libvirt group if this login session doesn't have it yet.
if ! id -nG | tr ' ' '\n' | grep -qx libvirt && getent group libvirt | grep -qw "$USER"; then
  exec sg libvirt -c "$(printf '%q ' "$0" "$@")"
fi

V() { virsh -q -c "$LIBVIRT_URI" "$@"; }
die() { echo "error: $*" >&2; exit 1; }
node_dir() { echo "$LAB_DIR/nodes/$1"; }
defined() { V dominfo "$1" &>/dev/null; }
ours() {   # libvirt domain names are host-global: refuse to touch a same-named VM that belongs to another lab
  defined "$1" || return 0
  V dumpxml "$1" | grep -q "<source file='$(node_dir "$1")/" || die "a VM named $1 exists but is not part of this lab ($(V dumpxml "$1" | sed -n "s/.*<title>\(.*\)<\/title>.*/\1/p")) — rename it in lab.conf"
}
running() { [[ "$(V domstate "$1" 2>/dev/null)" == "running" ]]; }
nodes_or_all() { [[ $# -gt 0 ]] && echo "$*" || echo "${ALL_NODES[*]}"; }
is_host() { [[ "${ROLE[$1]}" == "host" ]]; }
is_vyos() { ! is_host "$1"; }
vyos_nodes_or_all() { local n out=(); for n in $(nodes_or_all "$@"); do is_vyos "$n" && out+=("$n"); done; echo "${out[*]:-}"; }
PY="$LAB_DIR/tests/.venv/bin/python"; [[ -x "$PY" ]] || PY=python3

# ---- networks -------------------------------------------------------------
ensure_networks() {
  local n
  for n in "$OOB_NET"; do
    if ! V net-info "$n" &>/dev/null; then
      V net-define "$LAB_DIR/networks/$n.xml"
      V net-autostart "$n" >/dev/null
    fi
    [[ "$(V net-info "$n" | awk '/Active/{print $2}')" == "yes" ]] || V net-start "$n"
  done
}

# ---- point-to-point links (UDP tunnels between VMs) ------------------------
port_local() { echo $(( UDP_BASE + NODE_IDX[$1]*100 + $2 )); }           # UDP port a node's NIC listens on when it anchors a link
port_far()   { echo $(( UDP_BASE + 10000 + NODE_IDX[$1]*100 + $2 )); }   # ...and the port it sends to (the other end listens there)
node_ports() { case "${ROLE[$1]}" in pe) seq 1 "$PE_PORTS";; p) seq 1 "$P_PORTS";; ce) seq 1 "$CE_PORTS";; host) seq 1 "$HOST_PORTS";; esac; }
port_name()  { echo "eth$2"; }
mac()        { printf '%s:%02x:%02x' "$MAC_OUI" "${NODE_IDX[$1]}" "$2"; }
link_peer() {   # node port -> "peer_node peer_port prefix end(1|2) tenant|-" or "" if unwired
  local me="$1:$2" l a b pfx t
  for l in "${LINKS[@]}"; do
    read -r a b pfx t <<<"$l"
    [[ "$a" == "$me" ]] && { echo "${b%%:*} ${b##*:} $pfx 1 ${t:--}"; return; }
    [[ "$b" == "$me" ]] && { echo "${a%%:*} ${a##*:} $pfx 2 ${t:--}"; return; }
  done
  return 0
}
link_ip() {     # node port -> "address/len" on that link (first host address for end 1, second for end 2; v4 or v6)
  local peer; peer="$(link_peer "$1" "$2")"; [[ -z "$peer" ]] && return
  read -r _ _ pfx end _ <<<"$peer"
  python3 -c "import ipaddress; n=ipaddress.ip_network('$pfx'); print(f'{n.network_address + int(\"$end\")}/{n.prefixlen}')"
}
link_addr() { link_ip "$1" "$2" | cut -d/ -f1; }

# ---- XML generation -------------------------------------------------------
serial_xml() {
  cat <<X
    <serial type='tcp'>
      <source mode='bind' host='127.0.0.1' service='${CONSOLE_PORT[$1]}'/>
      <protocol type='raw'/>
      <log file='$(node_dir "$1")/console.log' append='on'/>
      <target port='0'/>
    </serial>
X
}

udp_nic_xml() {    # node port -> one <interface type='udp'> (the first end of a link listens on its own port_local; the second mirrors it)
  local n="$1" p="$2" peer remote local pn pp pfx end
  peer="$(link_peer "$n" "$p")"; local="$(port_local "$n" "$p")"; remote="$(port_far "$n" "$p")"
  if [[ -n "$peer" ]]; then
    read -r pn pp pfx end _ <<<"$peer"
    [[ "$end" == "2" ]] && { local="$(port_far "$pn" "$pp")"; remote="$(port_local "$pn" "$pp")"; }
    echo "    <!-- eth$p: $(link_ip "$n" "$p") <-> $pn $(port_name "$pn" "$pp") ($pfx) -->"
  else
    echo "    <!-- eth$p: unwired -->"
  fi
  cat <<X
    <interface type='udp'>
      <mac address='$(mac "$n" "$p")'/>
      <source address='127.0.0.1' port='$remote'>
        <local address='127.0.0.1' port='$local'/>
      </source>
      <model type='virtio'/>
      <address type='pci' domain='0x0000' bus='0x00' slot='$(printf '0x%02x' $((3+p)))' function='0x0'/>
    </interface>
X
}

domain_head_xml() {   # common preamble: name, deterministic UUID (from the mgmt IP), memory, cpu, disk, eth0 = OOB
  local n="$1" title="$2" ram="$3" vcpu="$4" d; d="$(node_dir "$n")"
  cat <<X
<domain type='kvm'>
  <name>$n</name>
  <uuid>$(uuidgen --sha1 --namespace @dns --name "srv6-core.${MGMT_IP[$n]}")</uuid>
  <title>$title</title>
  <memory unit='MiB'>$ram</memory>
  <vcpu placement='static'>$vcpu</vcpu>
  <cpu mode='host-passthrough' check='none'/>
  <os><type arch='x86_64' machine='pc'>hvm</type><boot dev='hd'/></os>
  <features><acpi/><apic/></features>
  <clock offset='utc'/>
  <on_poweroff>destroy</on_poweroff><on_reboot>restart</on_reboot><on_crash>restart</on_crash>
  <devices>
    <emulator>/usr/bin/qemu-system-x86_64</emulator>
    <disk type='file' device='disk'>
      <driver name='qemu' type='qcow2'/>
      <source file='$d/disk.qcow2'/>
      <target dev='vda' bus='virtio'/>
    </disk>
X
}

oob_nic_xml() {
  cat <<X
    <!-- eth0: OOB management ${MGMT_IP[$1]} -->
    <interface type='network'>
      <mac address='$(mac "$1" 0)'/>
      <source network='$OOB_NET'/>
      <model type='virtio'/>
      <address type='pci' domain='0x0000' bus='0x00' slot='0x03' function='0x0'/>
    </interface>
X
}

vyos_xml() {       # VyOS PE / P / CE: virtio disk, eth0 = OOB, eth1.. = point-to-point links (black-holed when unwired)
  local n="$1" p
  domain_head_xml "$n" "VyOS ${ROLE[$n]} ($n, ${DC[$n]})" "$VYOS_RAM_MIB" "$VYOS_VCPU"
  oob_nic_xml "$n"
  for p in $(node_ports "$n"); do udp_nic_xml "$n" "$p"; done
  serial_xml "$n"
  cat <<X
    <memballoon model='none'/>
  </devices>
</domain>
X
}

host_xml() {       # CirrOS end host: eth0 = OOB, eth1 = UDP tunnel to its CE; cloud-init NoCloud seed on an IDE cdrom
  local n="$1" d; d="$(node_dir "$n")"
  domain_head_xml "$n" "CirrOS host ($n, ${DC[$n]})" "$CIRROS_RAM_MIB" 1
  cat <<X
    <disk type='file' device='cdrom'>
      <driver name='qemu' type='raw'/>
      <source file='$d/seed.iso'/>
      <target dev='hda' bus='ide'/>
      <readonly/>
    </disk>
X
  oob_nic_xml "$n"
  udp_nic_xml "$n" 1
  serial_xml "$n"
  cat <<X
    <memballoon model='none'/>
  </devices>
</domain>
X
}

# ---- build ----------------------------------------------------------------
build_vyos() {
  local n="$1" d; d="$(node_dir "$n")"
  [[ -f "$VYOS_IMAGE" ]] || die "VyOS base image not found: $VYOS_IMAGE (see README: built by cat8000v-ipsec/tools/vyos_install.py)"
  [[ -f "$d/vyos_config.txt" ]] || die "$d/vyos_config.txt missing"
  mkdir -p "$d"
  if [[ ! -f "$d/disk.qcow2" ]]; then
    echo "[$n] creating overlay disk on $(basename "$VYOS_IMAGE")"
    qemu-img create -q -f qcow2 -b "$VYOS_IMAGE" -F qcow2 "$d/disk.qcow2"
  fi
  vyos_xml "$n" > "$d/domain.xml"
  V define "$d/domain.xml" >/dev/null
}

build_host() {
  local n="$1" d peer pn pp pfx end cidr gw; d="$(node_dir "$n")"
  [[ -f "$CIRROS_IMAGE" ]] || die "CirrOS image not found: $CIRROS_IMAGE"
  peer="$(link_peer "$n" 1)"; [[ -n "$peer" ]] || die "$n eth1 is not wired in LINKS"
  read -r pn pp pfx end _ <<<"$peer"
  cidr="$(link_ip "$n" 1)"; gw="$(link_addr "$pn" "$pp")"
  mkdir -p "$d"
  if [[ ! -f "$d/disk.qcow2" ]]; then
    echo "[$n] creating overlay disk on $(basename "$CIRROS_IMAGE")"
    qemu-img create -q -f qcow2 -b "$CIRROS_IMAGE" -F qcow2 "$d/disk.qcow2"
  fi
  host_seed "$n"
  host_xml "$n" > "$d/domain.xml"
  V define "$d/domain.xml" >/dev/null
}

host_seed() {   # cloud-init NoCloud seed; a fresh instance-id every time so CirrOS re-runs the user-data script on every boot
  local n="$1" d peer pn pp pfx end cidr gw; d="$(node_dir "$n")"
  peer="$(link_peer "$n" 1)"; read -r pn pp pfx end _ <<<"$peer"
  cidr="$(link_ip "$n" 1)"; gw="$(link_addr "$pn" "$pp")"
  echo "[$n] building cloud-init (NoCloud) seed ISO"
  # CirrOS parses meta-data as JSON (YAML meta-data fails with "json2fstree failed")
  printf '{"instance-id": "%s-%s", "local-hostname": "%s"}\n' "$n" "$(date +%s)" "$n" > "$d/meta-data"
  # CirrOS runs a user-data script; it has no netplan/cloud-init network support
  cat > "$d/user-data" <<U
#!/bin/sh
# $n: eth0 = OOB management (${MGMT_IP[$n]}), eth1 = $pn $(port_name "$pn" "$pp") (${DC[$n]} LAN $pfx, gateway $gw)
hostname $n
ip link set eth0 up
ip addr add ${MGMT_IP[$n]}/24 dev eth0
ip link set eth1 up
ip addr add $cidr dev eth1
ip route replace default via $gw dev eth1
U
  genisoimage -quiet -o "$d/seed.iso.tmp" -V cidata -J -r "$d/user-data" "$d/meta-data" && mv -f "$d/seed.iso.tmp" "$d/seed.iso"
}

build() { if is_host "$1"; then build_host "$1"; else build_vyos "$1"; fi; }

# ---- readiness / day-0 ----------------------------------------------------
ssh_ready() { timeout 8 bash -c "exec 3<>/dev/tcp/${MGMT_IP[$1]}/22" 2>/dev/null; }

bootstrap_vyos() {   # VyOS day-0 over the serial console (nodes/<n>/vyos_config.txt), logged to nodes/<n>/bootstrap.log
  local n="$1" d; d="$(node_dir "$n")"
  {
    echo "[$n] waiting for the VyOS login prompt..."
    python3 "$LAB_DIR/tools/vyos_console.py" wait 127.0.0.1 "${CONSOLE_PORT[$n]}" 600
    echo "[$n] applying day-0 config"
    python3 "$LAB_DIR/tools/vyos_console.py" push 127.0.0.1 "${CONSOLE_PORT[$n]}" "$d/vyos_config.txt"
    for _ in $(seq 30); do ssh_ready "$n" && break; sleep 5; done
    ssh_ready "$n" && echo "[$n] ready: ssh vyos@${MGMT_IP[$n]} (vyos)" || echo "[$n] warning: SSH not answering yet"
  } > "$d/bootstrap.log" 2>&1
}

# ---- commands ---------------------------------------------------------------
cmd_up() {
  ensure_networks
  for n in $(nodes_or_all "$@"); do
    ours "$n"; defined "$n" || build "$n"
    is_host "$n" && ! running "$n" && host_seed "$n" >/dev/null
    # pre-create the console log so virtlogd appends to our file instead of a root-only one
    [[ -f "$(node_dir "$n")/console.log" ]] || { touch "$(node_dir "$n")/console.log"; chmod 644 "$(node_dir "$n")/console.log"; }
    if running "$n"; then echo "[$n] already running"; else V start "$n"; echo "[$n] started (console: 127.0.0.1:${CONSOLE_PORT[$n]})"; fi
  done
}

cmd_down() {       # VyOS: ACPI shutdown (config was saved by bootstrap / commit+save); hosts: power off
  for n in $(nodes_or_all "$@"); do
    running "$n" || { echo "[$n] not running"; continue; }
    if is_vyos "$n"; then V shutdown "$n" >/dev/null; for _ in $(seq 30); do running "$n" || break; sleep 2; done; fi
    running "$n" && V destroy "$n" >/dev/null; echo "[$n] stopped"
  done
}

cmd_bootstrap() {  # push the day-0 config to VyOS nodes over their serial consoles, all in parallel
  local n pids=() names=()
  for n in $(vyos_nodes_or_all "$@"); do
    running "$n" || { echo "[$n] not running, skipped"; continue; }
    bootstrap_vyos "$n" & pids+=($!); names+=("$n")
    echo "[$n] bootstrapping in the background (log: nodes/$n/bootstrap.log)"
  done
  local i rc=0
  for i in "${!pids[@]}"; do
    wait "${pids[$i]}" || true; n="${names[$i]}"
    if grep -qE 'Invalid|Commit failed|failed|Error|!!|Traceback|timeout' "$(node_dir "$n")/bootstrap.log"; then
      echo "[$n] FAIL — see nodes/$n/bootstrap.log"; grep -nE 'Invalid|Commit failed|failed|Error|!!|Traceback|timeout' "$(node_dir "$n")/bootstrap.log" | head -5; rc=1
    else
      echo "[$n] OK $(tail -1 "$(node_dir "$n")/bootstrap.log")"
    fi
  done
  return $rc
}

cmd_configure() {  # (re)apply nodes/<n>/vyos_config.txt over SSH — idempotent, for changes made after the first boot
  [[ -x "$LAB_DIR/tests/.venv/bin/python" ]] || "$LAB_DIR/tests/setup.sh"
  local n; for n in $(vyos_nodes_or_all "$@"); do "$PY" "$LAB_DIR/tools/vyos_push.py" "${MGMT_IP[$n]}" "$(node_dir "$n")/vyos_config.txt" | sed "s/^/[$n] /"; done
}

cmd_wait() {       # block until SSH answers on the given nodes (VyOS sshd, CirrOS dropbear)
  for n in $(nodes_or_all "$@"); do
    for _ in $(seq 60); do ssh_ready "$n" && break; sleep 5; done
    ssh_ready "$n" && echo "[$n] SSH ready" || echo "[$n] SSH NOT ready"
  done
}

cmd_rebuild() {    # re-define domains (and host seeds) from lab.conf without touching disks
  for n in $(nodes_or_all "$@"); do
    running "$n" && die "$n is running; stop it first"
    defined "$n" && V undefine "$n" >/dev/null
    build "$n"; echo "[$n] redefined"
  done
}

cmd_clean() {      # destroy VMs and delete overlay disks (base images untouched)
  for n in $(nodes_or_all "$@"); do
    running "$n" && V destroy "$n" >/dev/null
    defined "$n" && V undefine "$n" >/dev/null
    rm -f "$(node_dir "$n")"/{disk.qcow2,seed.iso,seed.iso.tmp,meta-data,user-data,domain.xml,console.log,bootstrap.log}
    echo "[$n] removed"
  done
}

cmd_status() {
  printf '%-6s %-5s %-10s %-10s %-5s %-12s %-14s %-6s %-7s\n' NODE ROLE STATE MGMT-IP DC LOOPBACK LOCATOR AS CONSOLE
  for n in "${ALL_NODES[@]}"; do
    printf '%-6s %-5s %-10s %-10s %-5s %-12s %-14s %-6s %-7s\n' "$n" "${ROLE[$n]}" "$(V domstate "$n" 2>/dev/null || echo undefined)" \
      "${MGMT_IP[$n]}" "${DC[$n]}" "${LOOPBACK6[$n]:--}" "${LOCATOR[$n]:--}" "${BGP_AS[$n]:--}" "${CONSOLE_PORT[$n]}"
  done
  echo; echo "links (point-to-point UDP tunnels):"
  local l a b pfx t; for l in "${LINKS[@]}"; do read -r a b pfx t <<<"$l"
    echo "  ${a%%:*} $(port_name "${a%%:*}" "${a##*:}") $(link_addr "${a%%:*}" "${a##*:}")  <->  ${b%%:*} $(port_name "${b%%:*}" "${b##*:}") $(link_addr "${b%%:*}" "${b##*:}")   ($pfx${t:+, $t})"; done
  echo; for t in "${TENANTS[@]}"; do echo "VRF $t (table ${VRF_TABLE[$t]}, RT ${VRF_RT[$t]}): CE eBGP -> PE, VPNv4 over SRv6 End.DT4, route reflector $RR"; done
}

cmd_inventory() {  # the lab as JSON (nodes, links, service) — consumed by tests/resources/lab_vars.py (and a future Nautobot seed)
  local n l a b pfx
  {
    echo '{"lab": "srv6-core", "oob": {"network": "'"$OOB_NET"'", "gateway": "'"$OOB_GATEWAY"'"},'
    local t tj=""; for t in "${TENANTS[@]}"; do tj+="${tj:+, }\"$t\": {\"table\": ${VRF_TABLE[$t]}, \"rt\": \"${VRF_RT[$t]}\"}"; done
    echo ' "service": {"core_as": '"$CORE_AS"', "rr": "'"$RR"'", "isis_area": "'"$ISIS_AREA"'", "tenants": {'"$tj"'}},'
    echo ' "nodes": ['
    local first=1
    for n in "${ALL_NODES[@]}"; do
      [[ $first -eq 1 ]] || echo ','; first=0
      printf '  {"name": "%s", "role": "%s", "dc": "%s", "mgmt_ip": "%s", "console": %s, "idx": %s, "loopback6": %s, "router_id": %s, "locator": %s, "isis_net": %s, "asn": %s, "pe": %s, "ports": [' \
        "$n" "${ROLE[$n]}" "${DC[$n]}" "${MGMT_IP[$n]}" "${CONSOLE_PORT[$n]}" "${NODE_IDX[$n]}" \
        "$( [[ -n "${LOOPBACK6[$n]:-}" ]] && echo "\"${LOOPBACK6[$n]}\"" || echo null )" "$( [[ -n "${ROUTER_ID[$n]:-}" ]] && echo "\"${ROUTER_ID[$n]}\"" || echo null )" \
        "$( [[ -n "${LOCATOR[$n]:-}" ]] && echo "\"${LOCATOR[$n]}\"" || echo null )" "$( [[ -n "${ISIS_NET[$n]:-}" ]] && echo "\"${ISIS_NET[$n]}\"" || echo null )" \
        "$( [[ "${BGP_AS[$n]:--}" == "-" ]] && echo null || echo "${BGP_AS[$n]}" )" "$( [[ -n "${PE_OF[$n]:-}" ]] && echo "\"${PE_OF[$n]}\"" || echo null )"
      local p pf=1 peer
      for p in $(node_ports "$n"); do
        [[ $pf -eq 1 ]] || printf ','; pf=0; peer="$(link_peer "$n" "$p")"
        if [[ -n "$peer" ]]; then read -r pn pp pfx end t <<<"$peer"; printf '{"name": "eth%s", "ip": "%s", "peer": "%s", "peer_port": "eth%s", "prefix": "%s", "tenant": %s}' "$p" "$(link_ip "$n" "$p")" "$pn" "$pp" "$pfx" "$( [[ "$t" == "-" ]] && echo null || echo "\"$t\"" )"
        else printf '{"name": "eth%s", "ip": null, "peer": null}' "$p"; fi
      done
      printf ']}'
    done
    echo; echo ' ],'
    echo ' "links": ['
    first=1
    for l in "${LINKS[@]}"; do read -r a b pfx t <<<"$l"; [[ $first -eq 1 ]] || echo ','; first=0
      printf '  {"a": "%s", "a_port": "eth%s", "a_ip": "%s", "b": "%s", "b_port": "eth%s", "b_ip": "%s", "prefix": "%s", "tenant": %s}' \
        "${a%%:*}" "${a##*:}" "$(link_ip "${a%%:*}" "${a##*:}")" "${b%%:*}" "${b##*:}" "$(link_ip "${b%%:*}" "${b##*:}")" "$pfx" "$( [[ -z "$t" ]] && echo null || echo "\"$t\"" )"
    done
    echo; echo ' ]}'
  } | python3 -m json.tool
}

cmd_console() {
  local n="${1:?node}"; running "$n" || die "$n is not running"
  echo "Connecting to $n console (exit: Ctrl-] then q)"; echo
  socat -,raw,echo=0,escape=0x1d "tcp:127.0.0.1:${CONSOLE_PORT[$n]}"
}

cmd_ssh() {
  local n="${1:?node}"; shift || true
  if is_host "$n"; then echo "(CirrOS: user cirros, password gocubsgo)" >&2; ssh -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o LogLevel=ERROR -o PubkeyAuthentication=no "cirros@${MGMT_IP[$n]}" "$@"
  else ssh -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o LogLevel=ERROR "vyos@${MGMT_IP[$n]}" "$@"; fi
}

cmd_log() { tail -n "${2:-50}" -f "$(node_dir "${1:?node}")/console.log"; }

vy() { "$PY" "$LAB_DIR/tools/vyos_cmd.py" "${MGMT_IP[$1]}" "${@:2}"; }

cmd_verify() {     # a quick look at the control plane and the data plane end to end
  [[ -x "$LAB_DIR/tests/.venv/bin/python" ]] || "$LAB_DIR/tests/setup.sh"
  local n
  echo "== IS-IS adjacencies (PEs: 2, p1: 4, p2: 6, p3: 4)"
  for n in "${PES[@]}" "${PS[@]}"; do echo "-- $n"; vy "$n" "show isis neighbor" | grep -E 'Up|Init|Down' || echo "   (none)"; done
  echo; echo "== SRv6 locators (IS-IS view on $RR)"; vy "$RR" "show isis segment-routing srv6 node"
  echo; echo "== VPNv4 at the route reflector $RR"; vy "$RR" "show bgp ipv4 vpn summary" | grep -E '^fd00|Neighbor'; vy "$RR" "show bgp ipv4 vpn" | grep -E 'Route Distinguisher|\*>'
  for n in "${PES[@]}"; do
    echo; echo "== $n: VRF routes with SRv6 encapsulation, local SIDs (one End.DT4 per tenant)"
    for t in "${TENANTS[@]}"; do echo "-- vrf $t"; vy "$n" "sudo ip -c=never route show vrf $t" | grep -E "^172" || true; done
    vy "$n" "sudo ip -c=never -6 route show" | grep seg6local || echo "   (no seg6local route!)"
  done
  echo; echo "== ${CES[0]}: routes learned from ${PE_OF[${CES[0]}]} (default VRF = tenant-a, vrf tenant-b)"
  vy "${CES[0]}" "show ip route bgp" | grep -E '^B' || true; vy "${CES[0]}" "show ip route vrf tenant-b bgp" | grep -E '^B' || true
  echo; echo "== host ping matrix (${#HOSTS[@]} hosts, 3 pings each) — expected: reachable inside a tenant, unreachable across (h1 = tenant-a, h2 = tenant-b)"
  "$PY" "$LAB_DIR/tools/host_cmd.py" matrix "${HOSTS[@]}" || true
}

cmd_test() {       # Robot Framework suite; results in results/<date>_<time>/
  [[ -x "$LAB_DIR/tests/.venv/bin/robot" ]] || "$LAB_DIR/tests/setup.sh"
  exec "$LAB_DIR/tests/run.sh" "$@"
}

usage() {
  cat <<U
usage: $(basename "$0") <command> [node...]
  up [node..]        create (if needed) and start VMs                 (default: all)
  bootstrap [node..] push the day-0 config to VyOS nodes over the console (first boot only; parallel)
  configure [node..] re-apply nodes/<n>/vyos_config.txt over SSH (after editing lab.conf + tools/gen_configs.py)
  wait [node..]      wait until SSH answers
  down [node..]      stop VMs (VyOS: ACPI shutdown)
  status             nodes, addresses, links, consoles
  inventory          the lab as JSON
  verify             IS-IS / SRv6 / VPNv4 / VRF routes / host ping matrix
  test [robot args]  run the Robot Framework tests
  console <node>     attach to the serial console
  ssh <node> [cmd]   ssh to a node's OOB address (vyos/vyos, cirros/gocubsgo)
  log <node> [n]     follow a node's console log
  rebuild [node..]   re-generate domain XML / host seed ISOs (keeps disks)
  clean [node..]     stop, undefine and delete overlay disks
nodes: ${ALL_NODES[*]}
U
}

cmd="${1:-}"; shift || true
case "$cmd" in
  up|down|bootstrap|configure|wait|status|inventory|verify|test|console|ssh|log|rebuild|clean) "cmd_$cmd" "$@" ;;
  *) usage; exit 1 ;;
esac
