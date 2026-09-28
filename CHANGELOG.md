# Changelog

All notable changes to srv6-core are recorded here, newest first.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses
[Semantic Versioning](https://semver.org/spec/v2.0.0.html):
- **MAJOR:** a change that breaks how the lab is used, for example `lab.conf` keys, `lab.sh` commands or an API.
- **MINOR:** a new capability.
- **PATCH:** a fix to something that already existed.

The current version is in [`VERSION`](VERSION), and in git as a `v<version>` tag.

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

[1.1.0]: https://github.com/dcantor/srv6-core/compare/v1.0.0...v1.1.0
[1.0.0]: https://github.com/dcantor/srv6-core/releases/tag/v1.0.0
