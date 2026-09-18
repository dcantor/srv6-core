# AI-assisted operations: the lab as tools

`lab-mcp` (in [lab-portal](https://github.com/dcantor/lab-portal), `labportal/mcp/server.py`) exposes the labs to an AI
operator over the Model Context Protocol: 19 tools, read-only by default. `tools/chaos.py` injects one of seven real
misconfigurations so there is something to diagnose. Together they make a troubleshooting drill you can run against
yourself, a colleague, or a Claude session — and compare.

## Setup

```bash
cd ~/srv6-core && webapp/.venv/bin/pip install -e "../lab-portal[mcp]"      # once; installs the lab-mcp entry point
claude                                                                        # in ~/srv6-core: .mcp.json registers the "lab" server
```

`.mcp.json` in this repo starts `webapp/.venv/bin/lab-mcp` for any Claude Code session opened here (Claude Code asks once
to trust the project's MCP config). Any other MCP client works the same way: stdio, command `lab-mcp`. Labs come from
`~/.config/lab-hub/labs.json`; the NMS URLs from `VICTORIAMETRICS_URL` / `VICTORIALOGS_URL` / `PROMETHEUS_URL` /
`VMALERT_LOGS_URL` / `NAUTOBOT_URL` (defaults: the NMS at 10.0.0.10).

## Tools

| Tool | What it gives the operator |
|---|---|
| `labs`, `lab_status`, `lab_inventory` | which labs, VM states, the wiring with addresses — the map |
| `vyos_show`, `vyos_shell` | `show …` / ping / traceroute on a VyOS node; read-only Linux (`ip`, `nstat`, `vtysh -c show`, `sudo timeout N tcpdump …`) |
| `host_command`, `host_matrix` | a command on a tenant host; the full ping matrix |
| `portal_state` | tenant health, per-site eBGP, VRF / SRv6 route counts, host reachability, steering |
| `metrics_query`, `metrics_range` | PromQL on VictoriaMetrics (exporters, Telegraf, portal metrics, ALERTS) |
| `logs_query`, `flows_query` | LogsQL on VictoriaLogs: syslog (hostname / app_name / severity) and sFlow records; flows aggregated by source PE → SID per sampler |
| `alerts`, `events` | Prometheus + vmalert-logs alerts; Grafana annotations (runs, tests, steering, drills) |
| `nautobot_graphql`, `intended_config`, `config_diff` | the source of truth; the rendered config; **drift**: intended lines missing from the running config and unexpected lines in the lab's subtrees |
| `run_tests` | one or more Robot suites, results per test (slow) |
| `vyos_configure` | set/delete + commit + save — **refused unless** the server runs with `LAB_MCP_ALLOW_WRITE=1`; always audited (`~/.config/lab-hub/mcp-audit.jsonl`) |

Every router/host command and every configuration change is appended to the audit log, so a session's actions can be
reviewed afterwards.

## The drill

```bash
tests/.venv/bin/python tools/chaos.py inject            # random fault (or name one: rt-import, locator-leak, ce-shutdown,
                                                        #   silent-cut, sid-export, blackhole, mtu); --seed N for repeatability
# ... diagnose (yourself, or ask the Claude session: "something is wrong with the SRv6 lab — find it") ...
tests/.venv/bin/python tools/chaos.py reveal            # what was done, where, and the expected symptoms
tests/.venv/bin/python tools/chaos.py repair            # exact inverse + `lab.sh configure <node>`; Grafana gets the drill as a region
```

Faults are single misconfigurations with distinct signatures: a wrong route-target import (one site loses the tenant's
remote routes, sessions fine), missing locator leaks (routes present, packets vanish, `Ip6OutNoRoutes` climbs), a shut
CE session (alert), a silent link cut (BFD alert, adjacency count off, traffic rerouted), no SID export (remote PEs hold
a route without a SID), a static blackhole (exactly one host pair), MTU 1500 on a core port (IS-IS adjacency fails).

Scoring an operator (human or AI): time to a correct root cause, number of tool calls, whether the proposed fix is the
exact inverse, and whether anything was changed that should not have been (the audit log answers the last one).

## A drill, as run through the tools (seed 20260918)

The fault was injected blind; the diagnosis used only the MCP tools:

1. `alerts` → nothing firing. `portal_state` → both tenants **up**, every session Established, route counts unchanged,
   every core node at its expected adjacency count. *So: not a session, not the IGP.*
2. `host_matrix` → 22/56: exactly **one pair** fails, both directions: dc2-h1 ↔ dc4-h1 (tenant-a). *A route problem on pe2
   or pe4 for that prefix pair.*
3. `vyos_shell` on both: pe2 has `172.20.4.0/24 … encap seg6 [ fd00:c:4:e000:: ]`, pe4 has the mirror route. *The VPN is
   fine; the encapsulation must be failing.*
4. `config_diff('pe2')` → three intended lines missing: the static leaks for `fd00:c:4::/48` and the `fd00:c::/32`
   fallback in VRF tenant-a. `config_diff('pe4')` clean. `logs_query('app_name:commit')` → two commits on pe2 minutes ago.
5. `vyos_shell('pe2', 'nstat -az Ip6OutNoRoutes')` before and after five pings: 6 → 11. *Confirmed: the outer lookup in
   the VRF has no route — the lab's known Linux quirk.*
6. `vyos_configure('pe2', [the three lines])` → refused (read-only server), proposed lines returned. `chaos.py reveal`:
   **locator-leak on pe2, VRF tenant-a, towards pe4** — an exact match. Six tool calls, one wrong turn avoided (the
   reflectors were never touched).
