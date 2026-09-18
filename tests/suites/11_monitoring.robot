*** Settings ***
Documentation     Monitoring: node-exporter and frr-exporter answer on every VyOS node, node-exporter on every tenant host,
...               the portal exposes /metrics and /api/sd, and the Prometheus / VictoriaMetrics / Grafana stack on the NMS
...               (lab-portal/monitoring) scrapes every target, evaluates the alert rules and serves the dashboards.
...               Set PROMETHEUS / VICTORIAMETRICS / GRAFANA to other URLs if the stack runs elsewhere.
Resource          ../resources/common.resource

*** Variables ***
${PORTAL}           http://127.0.0.1:8091
${PROMETHEUS}       http://10.0.0.10:9090
${VICTORIAMETRICS}  http://10.0.0.10:8428
${GRAFANA}          http://10.0.0.10:3001
${VICTORIALOGS}     http://10.0.0.10:9428

*** Test Cases ***
Every VyOS node serves node-exporter and frr-exporter on its OOB address
    FOR    ${n}    IN    @{VYOS}
        ${node}=    Http Get    http://${MGMT}[${n}]:9100/metrics
        Should Contain    ${node}    node_uname_info{    msg=${n}: node-exporter has no node_uname_info
        ${frr}=    Http Get    http://${MGMT}[${n}]:9342/metrics
        Should Contain    ${frr}    frr_route_total    msg=${n}: frr-exporter has no frr_route_total
    END

The frr-exporter reports every BGP session of every PE as Established
    FOR    ${pe}    IN    @{PES}
        ${text}=    Http Get    http://${MGMT}[${pe}]:9342/metrics
        ${peers}=    Metric Samples    ${text}    frr_bgp_peer_state
        ${expected}=    Evaluate    len($RRS) + len($TENANTS) + len([s for s in $EXT_SITES.values() if s["pe"] == "${pe}"])    # reflectors + one CE per tenant + external CEs
        Length Should Be    ${peers}    ${expected}    msg=${pe}: expected ${expected} BGP peers in frr_bgp_peer_state
        FOR    ${p}    IN    @{peers}
            Should Be Equal As Numbers    ${p}[value]    1    msg=${pe}: peer ${p}[labels][peer] (${p}[labels][vrf]) is not Established
        END
    END

Every tenant host serves node-exporter
    FOR    ${h}    IN    @{HOSTS}
        ${text}=    Http Get    http://${MGMT}[${h}]:9100/metrics
        Should Contain    ${text}    node_network_receive_bytes_total{device="eth1"}    msg=${h}: no eth1 counters
    END

The portal's service discovery lists every exporter of the lab
    ${sd}=    Http Get    ${PORTAL}/api/sd
    ${targets}=    Evaluate    sorted(t for g in $sd for t in g["targets"])
    FOR    ${n}    IN    @{VYOS}
        Should Contain    ${targets}    ${MGMT}[${n}]:9100    msg=${n} node-exporter missing from /api/sd
        Should Contain    ${targets}    ${MGMT}[${n}]:9342    msg=${n} frr-exporter missing from /api/sd
    END
    FOR    ${h}    IN    @{HOSTS}
        Should Contain    ${targets}    ${MGMT}[${h}]:9100    msg=${h} node-exporter missing from /api/sd
    END
    ${portal}=    Evaluate    [g for g in $sd if g["labels"].get("job") == "portal"]
    Length Should Be    ${portal}    1    msg=the portal itself is not in /api/sd

The portal's metrics report every tenant healthy and the core fully adjacent
    Wait Until Keyword Succeeds    4 min    30 s    Portal Metrics Show Everything Up

Prometheus scrapes every target of the lab successfully
    ${targets}=    Http Get    ${PROMETHEUS}/api/v1/targets
    ${lab}=    Evaluate    [t for t in $targets["data"]["activeTargets"] if t["labels"].get("lab") == "srv6-core"]
    ${expected}=    Evaluate    2 * len($VYOS) + len($HOSTS) + 1
    Length Should Be    ${lab}    ${expected}    msg=Prometheus has ${{ len($lab) }} srv6-core targets, expected ${expected}
    ${down}=    Evaluate    [t["scrapeUrl"] + " " + t["lastError"] for t in $lab if t["health"] != "up"]
    Should Be Empty    ${down}    msg=targets not up: ${down}

Prometheus has the alert rules loaded and none of the lab's alerts firing
    ${rules}=    Http Get    ${PROMETHEUS}/api/v1/rules
    ${names}=    Evaluate    [r["name"] for g in $rules["data"]["groups"] for r in g["rules"]]
    FOR    ${a}    IN    ExporterDown    TenantHostUnreachable    TenantSiteBgpDown    TenantDegraded    IsisAdjacencyMissing    BfdSessionDown    VpnV4SessionDown    LabTestsFailed
        Should Contain    ${names}    ${a}    msg=alert rule ${a} not loaded
    END
    Wait Until Keyword Succeeds    6 min    30 s    No Lab Alert Firing    # an earlier suite's failover clears from the collector / scrape / `for` pipeline within a few minutes

VictoriaMetrics holds the series remote-written by Prometheus
    ${up}=    Prometheus Query    ${VICTORIAMETRICS}    count(up{lab="srv6-core"} == 1)
    ${expected}=    Evaluate    2 * len($VYOS) + len($HOSTS) + 1
    Should Be Equal As Numbers    ${up}[0][value]    ${expected}    msg=VictoriaMetrics sees ${up}[0][value] srv6-core targets up
    ${health}=    Prometheus Query    ${VICTORIAMETRICS}    lab_tenant_health{lab="srv6-core"}
    Length Should Be    ${health}    ${{ len($TENANTS) }}
    ${bgp}=    Prometheus Query    ${VICTORIAMETRICS}    count(frr_bgp_peer_state{lab="srv6-core",role="pe"} == 1)
    ${expected}=    Evaluate    len($PES) * (len($RRS) + len($TENANTS)) + len($EXT_SITES)
    Should Be Equal As Numbers    ${bgp}[0][value]    ${expected}    msg=${bgp}[0][value] Established PE BGP sessions in VictoriaMetrics, expected ${expected}

Every VyOS node pushes Telegraf metrics into VictoriaMetrics, tagged with the lab, role and DC
    ${pushing}=    Prometheus Query    ${VICTORIAMETRICS}    count by (host, role, dc) (cpu_usage_idle{lab="srv6-core",cpu="cpu-total"})
    ${hosts}=    Evaluate    sorted(r["labels"]["host"] for r in $pushing)
    Lists Should Be Equal    ${hosts}    ${{ sorted($VYOS) }}    msg=Telegraf series missing for some VyOS nodes
    FOR    ${r}    IN    @{pushing}
        ${h}=    Set Variable    ${r}[labels][host]
        Should Be Equal    ${r}[labels][role]    ${NODES}[${h}][role]
        Should Be Equal    ${r}[labels][dc]    ${NODES}[${h}][dc]
    END
    ${svc}=    Prometheus Query    ${VICTORIAMETRICS}    count(vyos_services_status{lab="srv6-core"} == 0) or vector(0)
    Should Be Equal As Numbers    ${svc}[0][value]    0    msg=a VyOS-reported service is down (vyos_services_status)
    ${fresh}=    Prometheus Query    ${VICTORIAMETRICS}    count(count by (host) (last_over_time(cpu_usage_idle{lab="srv6-core",cpu="cpu-total"}[2m])))
    Should Be Equal As Numbers    ${fresh}[0][value]    ${{ len($VYOS) }}    msg=not every node pushed within the last 2 minutes

Every VyOS node's syslog reaches VictoriaLogs
    ${r}=    Http Get    ${VICTORIALOGS}/select/logsql/query    query=_time:15m | stats by (hostname) count() as n
    ${hosts}=    Evaluate    sorted(__import__("json").loads(l)["hostname"] for l in $r.splitlines() if l.strip())
    FOR    ${n}    IN    @{VYOS}
        Should Contain    ${hosts}    ${n}    msg=no syslog from ${n} in the last 15 minutes
    END

Grafana is healthy and serves the provisioned dashboards
    ${health}=    Http Get    ${GRAFANA}/api/health
    Should Be Equal    ${health}[database]    ok
    ${dash}=    Http Get    ${GRAFANA}/api/search    type=dash-db
    ${uids}=    Evaluate    [d["uid"] for d in $dash]
    FOR    ${uid}    IN    srv6-core-overview    lab-node-detail    labs-fleet    vyos-telegraf
        Should Contain    ${uids}    ${uid}    msg=dashboard ${uid} not provisioned
    END
    ${d}=    Http Get    ${GRAFANA}/api/dashboards/uid/srv6-core-overview
    ${panels}=    Evaluate    [p for p in $d["dashboard"]["panels"] if p["type"] != "row"]
    Should Be True    len($panels) >= 15

*** Keywords ***
Portal Metrics Show Everything Up
    ${text}=    Http Get    ${PORTAL}/metrics
    ${health}=    Metric Samples    ${text}    lab_tenant_health
    Length Should Be    ${health}    ${{ len($TENANTS) }}
    FOR    ${h}    IN    @{health}
        Should Be Equal As Numbers    ${h}[value]    2    msg=${h}[labels][tenant] is not healthy (2 = up)
    END
    ${bgp}=    Metric Samples    ${text}    lab_tenant_site_bgp_up
    ${sites}=    Evaluate    sum(len(s) for s in $SITES.values()) + len($EXT_SITES)    # host sites + external CEs
    Length Should Be    ${bgp}    ${sites}
    FOR    ${b}    IN    @{bgp}
        Should Be Equal As Numbers    ${b}[value]    1    msg=${b}[labels][tenant] ${b}[labels][dc]: PE-CE eBGP down
    END
    ${up}=    Metric Samples    ${text}    lab_isis_adjacencies_up
    ${exp}=    Metric Samples    ${text}    lab_isis_adjacencies_expected
    Length Should Be    ${up}    ${{ len($CORE) }}
    FOR    ${u}    ${e}    IN ZIP    ${up}    ${exp}
        Should Be Equal As Numbers    ${u}[value]    ${e}[value]    msg=${u}[labels][node]: ${u}[value] IS-IS adjacencies up, ${e}[value] expected
        ${links}=    Evaluate    len($ISIS_NEIGHBORS[$u["labels"]["node"]])
        Should Be Equal As Numbers    ${e}[value]    ${links}    msg=${u}[labels][node]: expected count disagrees with the inventory
    END
    ${hosts}=    Metric Samples    ${text}    lab_host_reachable
    Length Should Be    ${hosts}    ${{ len($HOSTS) }}
    FOR    ${h}    IN    @{hosts}
        Should Be Equal As Numbers    ${h}[value]    1    msg=${h}[labels][host] unreachable
    END

No Lab Alert Firing
    ${rules}=    Http Get    ${PROMETHEUS}/api/v1/rules
    ${firing}=    Evaluate    [r["name"] for g in $rules["data"]["groups"] for r in g["rules"] if r.get("state") == "firing" and any(a["labels"].get("lab") == "srv6-core" for a in r.get("alerts", []))]
    Should Be Empty    ${firing}    msg=alerts firing for srv6-core: ${firing}
