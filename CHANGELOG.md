# Changelog

All notable changes to srv6-core are recorded here, newest first.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses
[Semantic Versioning](https://semver.org/spec/v2.0.0.html):
- **MAJOR:** a change that breaks how the lab is used, for example `lab.conf` keys, `lab.sh` commands or an API.
- **MINOR:** a new capability.
- **PATCH:** a fix to something that already existed.

The current version is in [`VERSION`](VERSION), and in git as a `v<version>` tag.

## [1.6.0] — 2026-09-29

### Added
- **SRv6 In Depth** (`docs/srv6-in-depth.pdf`, 19 pages), the second guide.
  - **Part 1** is a high-level overview for anyone: SRv6 in one page, what it gives an operator, the network-programming
    model, a comparison with MPLS and SR-MPLS with the trade-offs, and a VPN packet's life in five steps.
  - **Part 2** covers the details, shown on the lab's routers:
    - the SID structure, and one real packet decoded byte by byte (outer IPv6, SRH, inner IPv4);
    - the behaviours and flavours, and the two compressed-SID flavours;
    - pe1's IS-IS LSP with its SRv6 TLVs, and the Maximum SID Depths;
    - BGP's Prefix-SID and SID transposition, decoded from the route;
    - SR Policy, resilience, the Linux data plane (including `seg6_enabled` on the customer-facing ports), OAM,
      security, design and migration;
    - a troubleshooting checklist, a command cheat sheet and the RFCs.
- `docs/indepth_capture.py`: the nine extra read-only captures the guide quotes.

### Changed
- `docs/build_lab_guide.py` builds either guide: `srv6-lab-guide` (the default) or `srv6-in-depth`.

## [1.5.0] — 2026-09-29

### Added
- **The SRv6 Lab Guide** (`docs/srv6-lab-guide.pdf`, 26 pages), a starting point for readers new to SRv6.
  - **Part 1:** SRv6 from first principles, with six diagrams: segment routing, the SID and this lab's bit layout,
    behaviours, the SRH, IS-IS versus BGP, uSID shifting, L3VPN, a comparison with MPLS, and a glossary.
  - **Part 2:** a tour of the lab: the topology and addressing, then every view of the portal and the looking glass,
    Nautobot, monitoring, and the tests and CI.
  - **Part 3:** a hands-on packet walk with live output, then steering with uSID and failover.
  - Built by `docs/build_lab_guide.py`: two passes, so the contents page has page numbers. The screenshots come from
    `docs/guide_screenshots.py`.
- **Looking glass themes.** The same Default / Apple picker as the portal, with its own headline on the Overview page.

### Changed
- The walkthrough captures (`docs/walkthrough/`) were re-taken from the lab, and `docs/srv6-walkthrough.pdf` was rebuilt
  from them.

## [1.4.0] — 2026-09-28

### Added
- **Portal themes.** A picker in the header switches the look of the portal; the browser remembers the choice.
  - **Default:** the existing layout, unchanged.
  - **Apple:** styled after the apple.com homepage. It has a light translucent nav bar, the system SF font, white
    rounded tiles on light grey, blue pill buttons and a large centred headline, with a phone-width layout.
  - Both themes have light and dark modes.

## [1.3.0] — 2026-09-28

### Added
- **Steering on the map.** The portal's Steering view draws each policy on a map of the core: its path in colour beside
  the IGP shortest path it replaces, dashed.
  - **Preview** shows a path before it is applied.
  - **Measure delay** pings from the source PE in the tenant's VRF, over the policy and over the IGP to the same egress
    PE. For example, pe1 → 172.21.3.0/24 via p1 p3 measured 3.6 ms against 2.5 ms over p2.
  - API: `GET /api/steering/map`, `GET /api/steering/plan`, `GET /api/steering/measure`.
- **Tenant SLA probes.** Every minute, each tenant host pings every other site of its tenant (ten requests), and each
  pair is graded against delay and loss targets.
  - The SLA view shows a matrix per tenant, and each pair's last 24 hours as a chart.
  - A change of state writes a Grafana annotation.
  - `/metrics` exports `lab_tenant_rtt_ms`, `lab_tenant_loss_ratio` and `lab_tenant_sla_ok`.
  - New alert rules in lab-portal: **TenantSlaBreach** and **TenantSlaDown**.
  - API: `GET /api/sla`, `GET /api/sla/history`, `POST /api/sla/probe`.
- **Capacity view.** Shows how many more tenants fit at every data centre and at each one, and which limit runs out
  first: PE and CE ports, tenant letters, address blocks, firewall circuits, host memory or management addresses.
  - It also shows where each tenant can still add a site, and the lab host's memory, CPU and vCPU allocation.
  - `/metrics` exports `lab_capacity_room_tenants` and `lab_capacity_used_ratio`.
  - New info alert: **LabNoRoomForTenant**.
  - API: `GET /api/capacity`.
  - On this lab today it shows that pe4 has no free port, so no new tenant fits at every data centre.
- **Backup and restore.** The whole lab state goes into one checksummed `.tar.gz`: `lab.conf`, the steering policies,
  and every router's running and rendered configuration.
  - The Backups view can create, download and upload a backup, and shows what a restore would change.
  - A restore runs as a portal run (mode `restore`). It adds and removes tenants and sites with the existing add and
    remove steps, puts the steering policies back, re-seeds Nautobot and verifies.
  - It refuses when the core differs, or when a tenant would have to lose a single site.
  - API: `/api/backups`, `/api/backups/<file>`, `/api/backups/<file>/plan`, `/api/backups/upload`.
- **Tests:** a new suite, `16_operations` (6 cases), covering all four features, including a real restore. 109 cases in
  all.

### Changed
- `steer.py show` reads the PEs in parallel: about 9 s instead of 27 s.
- Suite 11 ignores info-severity alerts (a capacity notice is not a fault).

## [1.2.0] — 2026-09-28

### Added
- **Live packet capture.** A capture on a link streams its packets into the page while it runs, and **Stop** ends it
  early.
  - Behind it, `tcpdump` writes the capture, `tee` keeps the pcap, and a second `tcpdump -l` decodes line by line.
  - API: `POST /api/capture` with `stream: true`, `GET /api/capture/<id>?since=N`, `POST /api/capture/<id>/stop`.
- **Capture along a path.** On a prefix's path, **Capture along this path** captures on every link at once and can
  send five tenant pings along it from the first router, in the tenant's VRF.
  - The **journey grid** follows each echo request and reply hop by hop. It shows plain ICMP on the access links and
    the SRv6-encapsulated packet in the core, with a gap where a packet did not get through.
  - Each point has its own pcap.
  - API: `POST /api/capture/path`, `GET /api/capture/path/<id>`.
- **Compare two moments.** A new **Compare** page lists every path that moved between two times in a view or VRF,
  from the history: added, removed, changed (field by field, down to a single next hop's SID or label) or flapped.
  - API: `GET /api/diff`.
- **Tests:** two new cases in `15_looking_glass` (streaming with Stop, and a path capture). The withdraw test now also
  checks Compare. 103 cases in all.

### Changed
- One capture at a time per router **port** (it was per router), so a path can capture both sides of a CE.
- The last 60 captures are kept (was 30).

## [1.1.0] — 2026-09-28

### Added
- **Packet capture on any link, from the looking glass.**
  - **Starting a capture:** click a link on the Overview map or on a prefix's path. `tcpdump` runs on the router at
    one end of it, over SSH, with the port and filter you choose. The packets come back decoded: an SRv6 packet shows
    its segment routing header and the tenant's own packet inside.
  - **Filters:** everything, data only, SRv6-encapsulated tenant traffic, pings, BGP, BFD, IS-IS, or a custom pcap
    filter. You can also ping the far end while capturing.
  - **Bounds:** 1–1000 packets, 1–60 s, 64–1600 bytes per packet. One capture at a time per router. Only the model's
    own link interfaces can be named, and a filter may use only a restricted alphabet.
  - **Results:** the `.pcap` downloads for Wireshark, and the last 30 captures are kept.
  - **API:** `POST /api/capture`, `GET /api/capture/links`, `GET /api/captures`, `GET /api/capture/<id>.pcap`
    (`lg/capture.py`).
  - **Tests:** a new case in `15_looking_glass` (101 cases in all). It captures the pings across a PE's core link,
    checks the pcap and checks the refusals.

### Fixed
- The looking glass's navigation listed **Prefixes** twice.

## [1.0.0] — 2026-09-25

The lab as it stood at `a1ef950`, before this changelog:
- the SRv6 core (IS-IS, SRv6 uSID L3VPN, two route reflectors) and its tenants;
- the tenant provisioning portal;
- the BGP looking glass with its history;
- monitoring;
- the internet breakout;
- the interconnect;
- Nautobot as the source of truth;
- CI;
- the decks and videos.

[1.2.0]: https://github.com/dcantor/srv6-core/compare/v1.1.0...v1.2.0
[1.1.0]: https://github.com/dcantor/srv6-core/compare/v1.0.0...v1.1.0
[1.0.0]: https://github.com/dcantor/srv6-core/releases/tag/v1.0.0
