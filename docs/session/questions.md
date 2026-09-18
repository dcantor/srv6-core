# Question bank — SRv6 L3VPN

Grouped by depth. "Lab:" lines say where the answer can be shown live. Use them for interviews (pick five across the
levels), as a self-test, or as the Q&A backbone of a session.

## Warm-up (what is it)

1. **What is a SID?** An IPv6 address that means "do this instruction when you are the owner of the address". Owned SIDs are
   `seg6local` routes on the node; everywhere else they are ordinary IPv6 destinations. *Lab:* `ip -6 route show | grep seg6local` on p2.
2. **What is a locator?** The prefix a node advertises (IS-IS here) from which its SIDs are allocated. *Lab:* `show segment-routing srv6 locator`, `show ipv6 route isis`.
3. **What does a P router need to know about SRv6?** Only its own locator and its adjacencies' End.X SIDs. No VRFs, no BGP, no tenant routes. *Lab:* p2 has no VRF (`ip vrf show`).
4. **Name the three behaviours used in this lab.** End (uN, forward / shift), End.X (uA, forward out an adjacency), End.DT4 (decapsulate, look up the IPv4 payload in a VRF).
5. **Where does the SRH go when there is one segment?** Functionally nowhere: the destination address *is* the instruction. Linux still emits a one-entry SRH with segments-left 0; reduced encapsulation omits it. *Lab:* the p2 capture in act 3.
6. **What replaces the MPLS label in the VPN route?** The BGP Prefix-SID attribute carrying the End.DT4 SID (with the function bits transposed into the label field). *Lab:* `show bgp ipv4 vpn 172.20.3.0/24` on pe1.

## Working knowledge (how it fits together)

7. **Why IPv6-only in the core?** SRv6 needs IPv6 forwarding and nothing else; IPv4 in the core would be unused weight. The VPN payload is IPv4 (End.DT4); the sessions run over IPv6 loopbacks with `capability extended-nexthop`.
8. **Why two route reflectors and what happens if one dies?** Every PE holds every VPN route twice (Cluster list shows the reflector). Losing p1 changes nothing for the tenants. *Lab:* suite 06 shuts p1's client sessions and every ping keeps working.
9. **How is tenant isolation enforced?** By route targets: tenant-b's VRF imports 65000:200 only, so tenant-a's routes never exist in it. No firewall involved. *Lab:* dc1-h1 → dc1-h2 fails at the same site.
10. **Explain the RD scheme.** `65000:<table + PE index>`: table 100/200 per tenant, so 65000:103 = tenant-a on pe3. The RD makes overlapping prefixes distinct in VPNv4; the RT decides who imports.
11. **Why does the tenant's traceroute show a `*` and then the far CE?** The core does not decrement the inner TTL: the packet is encapsulated at the PE and decapsulated at the far PE. The `*` is the ingress PE's VRF interface with no address on the return path.
12. **What is the MTU consideration?** The outer IPv6 header (40 B) plus SRH (8 + 16·n). The core runs MTU 9000; tenants keep 1500. *Lab:* suite 02 pings 1600-byte DF frames across the core.
13. **What does `sid vpn export auto` do?** Makes FRR allocate an End.DT4 function from the locator per VRF and attach it as the Prefix-SID on exported routes. The value is runtime-allocated, so tests read it back rather than assume it.
14. **How does BFD interact with IS-IS here?** BFD (300 ms × 3) runs on every adjacency; on failure it tears the IS-IS adjacency down in ~1 s instead of the 30 s hold time. *Lab:* act 5, `show isis neighbor detail` shows "BFD is active, status Down".

## Deeper (what you learn by building it)

15. **Decode `sid structure=[32 16 16 0 16 48]`.** Block 32 bits, node 16, function 16, argument 0; transposition length 16 at offset 48 — the 16 function bits are carried in the label field. `Remote labels: 917504` = 0xE000 << 4 → function e000 → SID fd00:c:3:e000::.
16. **Why did the lab need static leaks of the locators into each VRF?** Linux scopes the SRv6 encapsulation's outer lookup to the ingress VRF for forwarded packets; without a route to the remote locator inside the tenant table, encapsulated packets are dropped (`Ip6OutNoRoutes`). The renderer leaks every locator via the P routers on the IGP shortest path (Dijkstra in the renderer) plus the whole block as a fallback.
17. **Why is the locator anchored on `dum0` with a /128 instead of the /48?** With uSID the connected /48 on dum0 outranked the uN route in zebra and the node stopped shifting. A /128 keeps the interface up and lets the IS-IS-installed uN own the /48.
18. **Explain uSID shift-and-forward with the carrier `fd00:c:11:13:3:e001::`.** p1 owns fd00:c:11::/48: its uN behaviour with the NEXT-C-SID flavour shifts the address left by 16 bits → fd00:c:13:3:e001::, forwards; p3 does the same → fd00:c:3:e001::; pe3's uDT4 decapsulates. *Lab:* act 4's captures on p1 and p3.
19. **How many uSIDs fit in one address, and what happens with more?** 128 − 32 (block) = 96 bits = six 16-bit slots including the final function; longer paths need a second carrier in an SRH. FRR advertises `SRH Max SL 3` etc. in IS-IS (`show isis segment-routing srv6 node`).
20. **Why does the return path of a steered flow still cross p2?** Steering is unidirectional: the static route is on pe1 only; pe3's route to dc1 is the BGP route via the shortest path. The lab's first version had ECMP on the return path through a /40 leak; per-locator leaks fixed it.
21. **Why does the failover test cut the link with a firewall rule instead of `disable`?** Admin-disabling the interface signals the neighbour; the point is the *silent* failure that only BFD detects. Also a disabled interface left a stuck BFD session on this VyOS build.
22. **What does FRR log on an adjacency change, and why didn't the lab see it at first?** `%ADJCHANGE …` at informational level; VyOS renders `log syslog notifications`, so nothing reached syslog until the level was raised (a VyOS boot-flag mechanism, `tools/frr_logging.py`). Also `log-neighbor-changes` must be set per VRF BGP instance.
23. **What is in an sFlow sample of SRv6 traffic?** The outer Ethernet/IPv6 header and the SRH (goflow2 decodes the routing-header addresses and segments-left) — so the flow view shows source PE → destination SID, i.e. the path in use; the inner IPv4 is not parsed past the routing header.

## Design / operations (interview-grade)

24. **You add a fifth tenant. What changes?** One VRF per PE and CE (table, RT, RD), an attachment circuit and a LAN per site, `sid vpn export auto` allocates a new uDT4 per PE. Nothing in the core. *Lab:* the portal's wizard does exactly this and runs the tests.
25. **A tenant reports dc1 ↔ dc3 slow but everything else fine. Where do you look?** The path: `ip route show vrf` on pe1 for the SID and outgoing interface, IS-IS metrics, BFD state, sFlow for which router carries the flow, node CPU (a 1-vCPU software data plane), then a steered iperf to compare paths.
26. **How would you steer only voice traffic of tenant-b over the p1–p3 path?** Today: a static route per destination prefix in the VRF with a segment list. Properly: policy-based routing on DSCP into a table whose routes carry the segment list, or an SR policy with colour on the VPN route (BGP colour extended community) — not in this lab yet.
27. **What breaks if two labs share one Nautobot namespace?** Prefix objects are global: two seeds "owning" the same /30 flip its role and description on every run. The IPsec lab's tunnel /30s and this lab's tenant-b circuits collided at 172.17 — the circuits moved to 172.18.
28. **How do you prove the configuration matches the model?** Render from Nautobot and from lab.conf with one renderer and diff (must be empty); then check every rendered line is present on the router (`render --live`). Both are test cases.
29. **What would you monitor first on this network?** BGP session state per VRF (frr-exporter), IS-IS/BFD adjacency counts vs expected (the portal's collector), host reachability per site, and the log-derived alerts for state changes. Then throughput and CPU. The lab has all of these.
30. **Why push and pull both?** Pull (Prometheus) is simple and central; push (Telegraf) carries device-native data (`vyos_services_status`, nstat) and tags, and survives the collector not knowing the device yet. Logs and flows are inherently push.
31. **Where would you expect vendor interop trouble?** The uSID flavour (NEXT-C-SID) and structure advertisement, transposition in the Prefix-SID, End.DT46 support, and SRH max-SL limits — the IS-IS SRv6 capability TLVs tell you the peer's limits.
32. **Explain the resource lesson from running two labs.** Eight IOS-XE VMs at 4 GiB each and one busy core apiece starve 1-vCPU VyOS data planes: timing-sensitive tests (BFD, throughput) fail while nothing is wrong. Test suites must not run concurrently across labs; pause the neighbour.

## Quick-fire (one line each)

33. Which daemon installs the End.DT4 route? — bgpd via zebra (`proto bgp` on the seg6local route).
34. Which installs uN and uA? — isisd (`proto isis`).
35. What is the outer source address? — the PE's loopback (`encapsulation source-address`).
36. What does hop limit 62 on p2's capture tell you? — two routers decremented it (pe1, p2).
37. How many BGP sessions in the lab? — 32: 16 VPNv4 (4 PEs × 2 RRs × both ends) + 16 PE–CE (4 × 2 tenants × both ends).
38. What is `fd00:c:1:e002::/64` on pe1? — a uA (End.X) SID for the adjacency out eth1.
39. What is the block? — `fd00:c::/32`, 32 bits, shared by every locator.
40. What is the one thing not in the textbook? — the VRF-scoped outer lookup on Linux and the locator leaks that fix it.
