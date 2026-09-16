# SRv6 WAN core lab — VyOS PEs, a P-router triangle and BGP L3VPN over SRv6

A segment-routing-over-IPv6 service-provider core simulated on one Linux host with libvirt/KVM: four **VyOS PEs**
(one per data centre), three **VyOS P routers** in a triangle (p1 is also the VPNv4 route reflector), a **VyOS CE**
per data centre serving **two tenants** — `tenant-a` (host h1, the CE's default VRF) and `tenant-b` (host h2, VRF
`tenant-b` on the CE) — each over its own attachment circuit into its own VRF on the PE, and a **CirrOS host** per
tenant per site. The core is IPv6-only with IS-IS level-2 carrying the SRv6 locators; each tenant's IPv4 prefixes travel
as BGP VPNv4 routes whose next hop is that tenant's **SRv6 End.DT4 SID** on the remote PE, so every h1 reaches every
other h1, every h2 every other h2, and the two never meet — not even at the same site. Nineteen VMs, about 13 GiB of
RAM, all VyOS nodes 1 vCPU / 1 GiB.

```
 hosts (CirrOS)   dc1-h1 172.20.1.2  dc1-h2 172.21.1.2   … the same in dc2, dc3, dc4 (172.20.n / 172.21.n)
                   | eth2 (default VRF)  | eth4 (VRF tenant-b)
 CEs   (VyOS)     ce1 AS65001 — eBGP to pe1 once per tenant      ce2 AS65002         ce3 AS65003         ce4 AS65004
                   | eth1 172.16.n.0/30 → PE VRF tenant-a          |                   |                   |
                   | eth3 172.17.n.0/30 → PE VRF tenant-b          |                   |                   |
 PEs   (VyOS)     pe1 fd00:c:1::/64     pe2 fd00:c:2::/64              pe3 fd00:c:3::/64     pe4 fd00:c:4::/64
                   |    \      /    |                                   |    \      /    |
 P core (VyOS)    p1 (RR) ---------- p2 ------------------------------ p3      IS-IS L2, IPv6-only, MTU 9000
                  fd00:a::11         fd00:a::12                        fd00:a::13
```
pe1/pe2 are dual-homed to p1+p2, pe3/pe4 to p2+p3, so every west↔east path crosses p2 (the tests use that).
A drawn version with every interface and prefix: [docs/topology.pdf](docs/topology.pdf).

## Quick start
```bash
./lab.sh up            # define the OOB network, build the overlay disks / cloud-init seeds, start 15 VMs
./lab.sh bootstrap     # first boot only: push nodes/<n>/vyos_config.txt over the serial consoles (all in parallel, ~4 min)
./lab.sh wait          # SSH on every node
./lab.sh verify        # IS-IS adjacencies, SRv6 nodes and SIDs, VPNv4 at the RR, VRF routes, CE routes, host ping matrix
./lab.sh test          # Robot Framework, results/<timestamp>/report.html
./lab.sh down          # ACPI shutdown; configs are saved, the next `up` converges without bootstrap
```
Credentials: VyOS `vyos`/`vyos` (`./lab.sh ssh pe1`), CirrOS `cirros`/`gocubsgo` (`./lab.sh ssh h1`); consoles
`./lab.sh console <node>`. The whole thing comes up in about six minutes from cold.

## The design
| Layer | What | Where |
|---|---|---|
| Underlay | IPv6-only, IS-IS level-2 point-to-point on every core link (`fd00:b::/48`, one /64 per link, MTU 9000), loopbacks `fd00:a::/48` | every PE and P |
| SRv6 | one locator per node from `fd00:c::/40` (block 40 / node 24 / function 16 bits → a /64 each), advertised by IS-IS (`segment-routing srv6`) and also carried by the passive `dum0` interface that holds the local SIDs; encapsulation source = loopback | every PE and P |
| Service | one VRF per tenant on every PE — `tenant-a` (table 100, RT 65000:100, RD 65000:10*n*) and `tenant-b` (table 200, RT 65000:200, RD 65000:20*n*) — each with its own attachment circuit and eBGP session to the CE, VPNv4 to the route reflector over the IPv6 loopbacks with `capability extended-nexthop`, `sid vpn export auto` → one End.DT4 SID per tenant | PEs |
| Route reflection | p1, peer-group `RR-CLIENTS`, VPNv4 only — p2/p3 run no BGP and know nothing about the tenant | p1 |
| Access | the CE keeps the tenants apart too: tenant-a lives in its default VRF (eth1 to the PE, eth2 LAN `172.20.n.0/24`, host h1), tenant-b in VRF `tenant-b` (eth3 to the PE, eth4 LAN `172.21.n.0/24`, host h2); each VRF runs its own eBGP session announcing its LAN and learning the other three | CEs, hosts |

What the data plane looks like on a PE (`sudo ip route show vrf tenant-a` / `sudo ip -6 route | grep seg6local`):
```
172.20.3.0/24  encap seg6 mode encap segs 1 [ fd00:c:3:0:3:: ] via fe80::... dev eth2    # remote LAN → pe3's End.DT4 SID (tenant-a)
fd00:c:1:0:3::  encap seg6local action End.DT4 vrftable tenant-a                        # our SIDs: one per tenant VRF
fd00:c:1:0:4::  encap seg6local action End.DT4 vrftable tenant-b
fd00:c:1::      encap seg6local action End dev dum0                                     # IS-IS End SID
fd00:c:1:0:2::  encap seg6local action End.X nh6 fe80::... oif eth2                     # IS-IS adjacency SIDs
```
SID function values are allocated by FRR at run time (they can change after a reconfiguration); the tests only assert
that a SID lies inside the right locator.

### The one thing that is not in the textbook
Linux (6.18 here) scopes the **outer IPv6 lookup of the SRv6 encapsulation to the ingress VRF's table for forwarded
packets** — locally generated traffic from the PE works, traffic coming in from the CE is dropped with
`Ip6OutNoRoutes`. Each PE therefore leaks the locator block into the VRF table, recursively via the loopbacks of its
two attached P routers (resolved by IS-IS, so ECMP and failover survive):
```
set vrf name tenant-a protocols static route6 fd00:c::/40 next-hop fd00:a::11 vrf default
set vrf name tenant-a protocols static route6 fd00:c::/40 next-hop fd00:a::12 vrf default
```
(Leaking through BGP `import vrf default` does not work: the IS-IS next hops are link-local and fail nexthop validation.)

## Addressing
| node | role | OOB (srv6-oob) | loopback | router-id | IS-IS NET | locator | AS |
|---|---|---|---|---|---|---|---|
| pe1..pe4 | pe | 10.3.0.11-14 | fd00:a::1-4 | 10.255.0.1-4 | 49.0001.0000.0000.000*n*.00 | fd00:c:*n*::/64 | 65000 |
| p1 (RR), p2, p3 | p | 10.3.0.21-23 | fd00:a::11-13 | 10.255.0.11-13 | …0011/0012/0013.00 | fd00:c:11-13::/64 | 65000 (p1) |
| ce1..ce4 | ce | 10.3.0.31-34 | – | 172.20.*n*.1 | – | – | 6500*n* |
| dc*n*-h1 | host (tenant-a) | 10.3.0.41-44 | – | – | – | – | – |
| dc*n*-h2 | host (tenant-b) | 10.3.0.51-54 | – | – | – | – | – |

Links: core `fd00:b:0:<ab>::/64` (`ab` = the two node numbers, e.g. p1–p2 `fd00:b:0:12::/64`, p2–pe1 `fd00:b:0:201::/64`);
tenant-a: PE–CE `172.16.n.0/30` (PE .1), CE–host `172.20.n.0/24`; tenant-b: PE–CE `172.17.n.0/30`, CE–host `172.21.n.0/24`
(CE .1 = gateway, host .2). A fourth token on a `LINKS` entry names the tenant. The first end of a link in
`lab.conf` gets the first address. `./lab.sh status` prints every link with both addresses, `./lab.sh inventory`
the whole lab as JSON (what the tests read; a future Nautobot seed would too).

## Tests (`./lab.sh test`, 19 cases)
| Suite | Checks |
|---|---|
| 01 management | every node on the OOB network with SSH, host names, host LAN addresses, MTU 9000 on all core links, config saved |
| 02 underlay | exactly the expected IS-IS L2 adjacencies (2/4/6/4/2), every loopback via IS-IS, PE↔PE pings incl. 1600-byte DF (headroom for the encapsulation) |
| 03 srv6 | locator Up with 40/24/16 on all 7 nodes, all 7 in `show isis segment-routing srv6 node`, all locators in every RIB, End / End.X SIDs and exactly one End.DT4 SID per tenant VRF in the kernel, seg6 enabled per core interface |
| 04 vpn | per tenant: 4 RR clients Established, every LAN under its RD at the RR, remote LANs imported into the right VRF only (no prefix of the other tenant) with a SID inside the right locator and a recursive seg6 route, CEs learn the other three LANs over the tenant's own session (default VRF / VRF tenant-b) |
| 05 end to end | every host reaches every host of its tenant (2 × 4×3 pings) and **none of the other tenant's**, not even at the same site; dc1→dc3 traffic transits p2 with `tcpdump` showing `IP6 fd00:a::1 > fd00:c:3:…` both ways; P routers hold no VRF and no tenant routes |

Every run captures `show configuration commands` of all VyOS nodes before and after and diffs them (`results/<ts>/configs/`).

## What is where
| Path | Purpose |
|---|---|
| `lab.conf` | the topology: nodes, roles, addresses, `LINKS`, service parameters (AS, VRF, RT, RR) |
| `lab.sh` | libvirt controller: `up down bootstrap configure wait status inventory verify test console ssh log rebuild clean` |
| `docs/topology.pdf`, `docs/topology.py` | the topology as a two-page PDF (diagram, addressing, packet walk), drawn from `lab.sh inventory` — rerun the script after editing `lab.conf` |
| `tools/gen_configs.py` | renders `nodes/<n>/vyos_config.txt` (the day-0 `set` lines) from `lab.sh inventory` — run after editing `lab.conf` |
| `tools/vyos_console.py`, `tools/vyos_push.py` | serial-console helper (first boot) and the SSH equivalent (`configure`) |
| `tools/vyos_cmd.py`, `tools/host_cmd.py` | SSH helpers (netmiko for VyOS, paramiko for CirrOS; `host_cmd.py matrix` = the ping matrix) |
| `nodes/<n>/` | per node: `vyos_config.txt` (committed), generated `domain.xml`, `disk.qcow2` overlay, `console.log`, `bootstrap.log`; hosts: `user-data`, `meta-data`, `seed.iso` |
| `networks/srv6-oob.xml` | the isolated OOB bridge `virbr-srv6oob` 10.3.0.0/24 (host = 10.3.0.1) |
| `tests/` | Robot Framework: `resources/lab_vars.py` (from `lab.sh inventory`), `resources/LabLib.py`, `suites/0*.robot`, `run.sh` |

## Design notes and quirks worth knowing
- **Images are shared with the other labs**: the VyOS base image is `../cat8000v-ipsec/images/vyos-base.qcow2`
  (rolling 2026.09.09, kernel 6.18, FRR 10 — built once by `cat8000v-ipsec/tools/vyos_install.py`) and CirrOS comes
  from `../cat9000v/images/`. Every node is a qcow2 overlay; rebuilding a base image invalidates them (`./lab.sh clean && ./lab.sh up`).
- **Point-to-point links are QEMU UDP sockets** on 127.0.0.1 (`UDP_BASE` 14000 + idx·100 + port, far side +10000), the
  first end of a link anchors the pair; unwired ports are still created (black-holed) so adding a link never changes
  an anchor VM's XML. The port band, the OOB subnet 10.3.0.0/24, MAC OUI `52:54:00:c6` and console ports 5301-5315
  were chosen not to collide with the cat9000v / cat8000v labs on the same host.
- **libvirt domain names are host-global**: `lab.sh` refuses to touch a same-named VM that belongs to another lab
  (that is why the hosts are `dcN-h1/h2`, not `host1..`).
- **Day-0 over the serial console**, in parallel: `bootstrap` pushes ~65 `set` lines per PE and grades the log for
  `Invalid`/`Commit failed`. Commit + save takes about a minute per node; `vyos_console.py` handles the
  first-boot login. Everything after that is SSH.
- **VyOS SRv6 specifics** (rolling `current`): a locator needs `protocols segment-routing interface <if> srv6` on at
  least one interface (that is what enables `seg6_enabled`); IS-IS SRv6 needs `segment-routing srv6 interface dum0`
  where the SIDs are installed; the VRF BGP instance needs its own `system-as`; `sid vpn export auto` under the VRF's
  `address-family ipv4-unicast` gives End.DT4 (`protocols bgp sid vpn per-vrf export auto` would give End.DT46 — never both).
- **VPNv4 over IPv6-only sessions** needs `capability extended-nexthop` on both the PE and the RR.
- **Changing `rd vpn export` on a live VRF** (FRR 10) silently stops the export until `export vpn` is toggled off and on
  again — seen when the tenant-a RD moved from 65000:*n* to 65000:10*n*. `./lab.sh configure` re-applies
  `vyos_config.txt` over SSH (idempotent) for changes after the first boot; adding NICs needs `down`, `rebuild`, `up` first.
- **MTU**: the 64-byte SRv6 overhead is absorbed by the 9000-byte core; hosts and CEs stay at 1500.
- **CirrOS** is IPv4-only with busybox tools and dropbear (password auth only); its `meta-data` must be JSON, and it
  runs the `user-data` script once per instance-id — `lab.sh up` therefore regenerates the seed with a fresh
  instance-id on every start so the hosts get their addresses back after a reboot.
- **Don't run this alongside the cat9000v lab** (two 18 GiB Cat9kv); with the IPsec lab down there is ample headroom.

## Next (not in this pass)
Nautobot modelling on the shared NMS (needs a 5th NIC on `srv6-oob` as 10.3.0.10): devices with roles pe/p/ce/host,
locations dc1..dc4 + core, platform vyos/cirros, the prefixes/VRF/RD/RT above, cables from `LINKS` —
`./lab.sh inventory` is the seed input. Then rendering `vyos_config.txt` from Nautobot instead of `lab.conf`.
