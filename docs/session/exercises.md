# Exercises on the live lab

For a class or a hands-on interview. Each has a check the audience can run and a solution. Credentials: VyOS `vyos/vyos`,
hosts `lab/lab`, OOB addresses in `./lab.sh status`.

1. **Find the SID.** Without looking at the config, find pe4's End.DT46 SID for tenant-b and explain each part of it.
   *Check:* `ip -6 route show | grep End.DT46` on pe4; block fd00:c, node 4, function allocated by FRR.
2. **Follow a packet.** From dc2-h1, ping dc4-h1 and capture on the P router that carries it. Which P is it and why?
   *Solution:* pe2 → p2 → pe4 (pe2's shortest path to pe4's locator is via p2); `tcpdump -ni eth6 'ip6 and dst net fd00:c:4::/48'` on p2.
3. **Break isolation on purpose — safely.** Predict what would happen if tenant-b's VRF also imported RT 65000:100 (do not apply it on a shared lab): which routes appear, what still isolates the hosts?
   *Solution:* tenant-a's LANs appear in tenant-b on every PE; the hosts would reach each other; nothing else stops it — RT is the only isolation.
4. **Steer and prove it.** Steer tenant-a's dc4 LAN from pe1 via p1 and p3, then prove with a capture that p2 no longer sees the traffic and that p3 receives `fd00:c:3…`-style shifted addresses… (careful: dc4 sits behind pe4 — which path makes sense? p1 → p3 → pe4).
   *Solution:* `tools/steer.py add pe1 tenant-a 172.20.4.0/24 p1 p3`; capture on p2 shows nothing for that flow; p3 sees `fd00:c:4:e00x::` after two shifts. `steer.py del` afterwards.
5. **Measure BFD.** Change nothing; run suite 08 and read the packet loss. Then compute the theoretical detection time from the BFD timers in `show bfd peers`. Do they agree?
   *Solution:* 300 ms × 3 = 900 ms detection + IS-IS SPF + FIB update ≈ 1 s → ~5 lost packets at 0.2 s.
6. **Find the quirk.** On pe1, list the static IPv6 routes inside VRF tenant-a and explain why they exist.
   *Solution:* `show ipv6 route vrf tenant-a static` — the locator leaks (Linux scopes the encapsulation's outer lookup to the ingress VRF).
7. **Read the telemetry.** In Grafana, find the last failover on the overview (annotation), then the isisd log line in VictoriaLogs, then the flow record on p2 during the ping — three views of one event.
8. **Add a site (portal).** Use the portal to add a tenant-a site at dc4 for a new tenant and watch the pipeline; afterwards explain every step it ran and what the tests proved.
