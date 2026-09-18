# Teaching / interview session: SRv6 L3VPN on real boxes

A 45–60 minute session built on this lab: whiteboard first (the mental model), then prove every claim live. Works as a
teaching session, a brown-bag, or the "explain it, then show me" part of an interview — and the same material runs in
five minutes as a demo. Files:

| File | Purpose |
|---|---|
| this guide | timing, whiteboard sequence, what to draw, what to say, where the audience usually gets stuck |
| `tools/demo_live.py` | presenter mode: six acts, real commands on the lab one Enter at a time, with the point to make after each output |
| [questions.md](questions.md) | 40 questions from warm-up to expert, with model answers that reference what the lab shows |
| [exercises.md](exercises.md) | hands-on tasks for the audience on the live lab (with solutions) |
| [slides.pptx](slides.pptx) | 14 slides: the whiteboard drawings, for when there is no whiteboard |
| `docs/srv6-walkthrough.md` / `.pdf` | the long-form version to hand out afterwards |

Before the session: `./lab.sh status` (19 VMs running), `./lab.sh test suites/05_end_to_end.robot` green, Grafana open on
the overview dashboard, one terminal ready with `tests/.venv/bin/python tools/demo_live.py`.

## The shape of the session

| min | segment | mode |
|---|---|---|
| 0–5 | why: what SRv6 replaces (LDP/RSVP, label tables, per-tunnel state) and the one idea (instructions in the address) | talk |
| 5–15 | whiteboard 1: topology, IS-IS, locators, the three SIDs | draw |
| 15–20 | act 1 live | demo |
| 20–28 | whiteboard 2: the VPN — RD/RT/reflectors unchanged, SID in place of label, the encapsulating route | draw |
| 28–33 | act 2 + act 3 live (VPN route, then the capture on p2) | demo |
| 33–40 | whiteboard 3: uSID — the address anatomy and shift-and-forward | draw |
| 40–45 | act 4 live (the address changing hop by hop) | demo |
| 45–52 | act 5 live: silent failure, BFD, packets lost; the log line that says why | demo |
| 52–60 | act 6 + questions: how it is operated (Nautobot, portal, tests, telemetry) | demo / discuss |

Five-minute version: acts 3 and 4 only ("here is the packet, here is the path rewritten"); add act 5 if there are seven.

## Whiteboard 1 — the underlay (10 min)

Draw: the P triangle on top (p1, p2, p3), four PEs below, one CE and two hosts under each. Label the links "IPv6 only,
IS-IS L2". Write `fd00:c::/32` next to the core and one `/48` per node: `fd00:c:1::` … `fd00:c:13::`.

Say:
- "A locator is just a prefix the node advertises in IS-IS. Nothing new on the wire."
- "Every address inside it is an instruction the *owner* executes. Three instructions matter today." Write them as a table:
  `End` (uN) = "shift and forward", `End.X` (uA) = "out this link", `End.DT4` = "decapsulate into this VRF".
- "uSID: 32-bit block, 16-bit node, 16-bit function. Keep that in mind for whiteboard 3."
- The one non-textbook point: "Linux scopes the outer lookup to the ingress VRF — we leak the locators into each tenant
  table. Ask me about it after; it is the kind of thing you only learn by building it."

Where people get stuck: "so where is the tunnel?" — there is none; a SID is a route on the node that owns it and an IPv6
destination everywhere else. Act 1's `ip -6 route show | grep seg6local` on p2 usually settles it.

## Whiteboard 2 — the overlay (8 min)

Draw: pe1 and pe3 with a VRF box each (tenant-a, tenant-b); p1 and p3 marked RR; an arrow "VPNv4 over IPv6 sessions".
Write one VPN route as it looks in BGP: `RD 65000:103 : 172.20.3.0/24, RT 65000:100, Prefix-SID fd00:c:3:e000::`.

Say:
- "Everything you know from MPLS L3VPN survives: RD, RT, reflectors, import/export. Only the transport attribute changed."
- "The SID's function bits ride in the label field (transposition) — that is why the route shows *Remote label 917504*
  next to *Remote SID fd00:c:3::*. 917504 is 0xE000 shifted by 4; the receiver reassembles fd00:c:3:e000::."
- "The PE installs `encap seg6 … [ fd00:c:3:e000:: ]` in the VRF. No label table, no LFIB: wrap in IPv6 to that address."
- Point out ECMP: the dc2 LAN has two next hops because pe2 is equidistant via p1 and p2 — for free from the IGP.

## Whiteboard 3 — uSID (7 min)

Draw the address as boxes: `fd00:c | 11 | 13 | 3 | e001 | ::` with labels block / p1 / p3 / pe3 / function.

Say:
- "The node whose ID is first after the block owns the packet. It deletes its own ID (shift left 16 bits) and forwards."
- "So the segment list lives in the destination address: a four-hop explicit path costs zero extra bytes and zero state in
  the core." Then show act 4: on p1's far link the destination is already `fd00:c:13:3:e001::`.
- Contrast: "the same path as an uncompressed SRH is three 128-bit segments, 56 bytes, processed at every hop."

## Act 5 — say this before pressing Enter

"Interfaces stay up. Nothing tells IS-IS the neighbour is gone. Without BFD this is a 30-second hold-time outage. With
BFD at 300 ms × 3 it is about one second. Watch the ping count." The lab typically loses 3–5 of 150 packets at 0.2 s
spacing. Then the log line: isisd's `%ADJCHANGE … bfd session went down` — "the router told us; the alert fired; Grafana
has the region annotated. That is what monitoring is for."

## Closing (act 6)

"None of this is hand-typed. Nautobot models it, one renderer produces every config, 64 tests prove it after every
change, and telemetry watches it: metrics pulled and pushed, syslog, flows. The lab is a small network operated
properly." Hand out the walkthrough PDF and the question bank.
