*** Settings ***
Documentation     Operations features of the portal: tenant SLA probes (every pair of a tenant's sites, host to host, delay and
...               loss against targets, exported to Prometheus with alert rules), the capacity view (what runs out first), steering
...               on the map (a policy's path beside the IGP's, both measured), and backup / restore (the whole lab in one file; a
...               restore puts the steering back as the backup had it). The restore run is skipped when this suite itself runs
...               inside a portal run (runs are one at a time); `./lab.sh test` from a shell runs it.
Resource          ../resources/common.resource
Suite Teardown    Remove The Policy And Close Connections

*** Variables ***
${PORTAL}         http://127.0.0.1:8091
${PROMETHEUS}     http://10.0.0.10:9090
${SRC_PE}         pe1
${TENANT}         tenant-b
${VIA1}           p1
${VIA2}           p3
${DST_LAN}        ${None}

*** Test Cases ***
Every pair of every tenant's sites is probed and meets its SLA
    ${d}=    Http Post    ${PORTAL}/api/sla/probe    timeout=120
    Should Be Equal As Integers    ${d}[status]    200
    ${expected}=    Evaluate    sum(len(s) * (len(s) - 1) for s in $SITES.values())
    Length Should Be    ${d}[json][pairs]    ${expected}    msg=every ordered pair of a tenant's sites is probed
    Should Be Empty    ${d}[json][errors]    msg=every host answered the prober
    FOR    ${p}    IN    @{d}[json][pairs]
        Should Be Equal    ${p}[state]    ok    msg=${p}[tenant] ${p}[src] -> ${p}[dst]: ${p}[rtt_ms] ms, loss ${p}[loss]
        Should Be Equal    ${HOST_TENANT}[${p}[src]]    ${HOST_TENANT}[${p}[dst]]    msg=a probe crosses tenants
    END

The SLA results are on /metrics, in the pair's history, and in Prometheus with their alert rules
    ${text}=    Http Get    ${PORTAL}/metrics
    ${rtt}=    Metric Samples    ${text}    lab_tenant_rtt_ms
    ${ok}=    Metric Samples    ${text}    lab_tenant_sla_ok
    ${expected}=    Evaluate    sum(len(s) * (len(s) - 1) for s in $SITES.values())
    Length Should Be    ${rtt}    ${expected}
    Length Should Be    ${ok}    ${expected}
    ${a}=    Set Variable    ${rtt}[0][labels]
    ${h}=    Http Get    ${PORTAL}/api/sla/history    tenant=${a}[tenant]    src=${a}[src]    dst=${a}[dst]
    Should Not Be Empty    ${h}[points]
    ${rules}=    Http Get    ${PROMETHEUS}/api/v1/rules
    ${names}=    Evaluate    [r["name"] for g in $rules["data"]["groups"] for r in g["rules"]]
    FOR    ${n}    IN    TenantSlaBreach    TenantSlaDown    LabNoRoomForTenant
        Should Contain    ${names}    ${n}    msg=alert rule ${n} not loaded in Prometheus
    END

The capacity view adds up: used within total, and every answer names the limit that runs out first
    ${c}=    Http Get    ${PORTAL}/api/capacity    refresh=true    timeout=120
    Length Should Be    ${c}[per_dc]    ${{len($DCS)}}
    ${all}=    Evaluate    [x for d in $c["per_dc"] for x in (d["pe_ports"], d["ce_ports"])] + $c["lab"] + $c["host"]["resources"]
    FOR    ${r}    IN    @{all}
        Should Be True    ${r}[total] is None or 0 <= ${r}[used] <= ${r}[total] or '${r}[name]' in ('vCPUs allocated',)    msg=${r}[name]: ${r}[used] of ${r}[total]
    END
    ${letters}=    Evaluate    next(r for r in $c["lab"] if r["name"] == "Tenant letters")
    Should Be Equal As Integers    ${letters}[used]    ${{len($TENANTS)}}
    ${room}=    Set Variable    ${c}[room]
    Should Be True    ${room}[every_dc][tenants] == min(${room}[every_dc][limits].values())
    Should Be True    ${room}[every_dc][tenants] <= min(r["tenants"] for r in ${room}[per_dc].values())    msg=every DC cannot fit more than the tightest one
    FOR    ${d}    IN    @{c}[per_dc]
        Should Be Equal As Integers    ${d}[sites_fit]    ${{min(len($d["pe_free"]), len($d["ce_free"]) // 2)}}
        FOR    ${t}    IN    @{TENANTS}
            IF    '${d}[dc]' in $SITES['${t}']    List Should Contain Value    ${d}[tenants_here]    ${t}
        END
    END

The map draws a policy's steered path beside the IGP shortest path, and both are measured
    ${dst}=    Set Variable    ${SITES}[${TENANT}][dc3]
    Should Be Equal    ${dst}[pe]    pe3    msg=the steered-to site is behind pe3
    ${plan}=    Http Get    ${PORTAL}/api/steering/plan    pe=${SRC_PE}    tenant=${TENANT}    prefix=${dst}[lan]    via=${VIA1} ${VIA2}
    Should Be Equal    ${plan}[steered]    ${{[$SRC_PE, $VIA1, $VIA2, "pe3"]}}
    Should Not Contain    ${plan}[igp]    ${plan}[steered]    msg=the steered path is not a shortest path
    Should Be True    ${plan}[hops_steered] > ${plan}[hops_igp]
    Steer    add    ${SRC_PE}    ${TENANT}    ${dst}[lan]    ${VIA1}    ${VIA2}
    Set Suite Variable    ${DST_LAN}    ${dst}[lan]
    ${m}=    Http Get    ${PORTAL}/api/steering/map    timeout=120
    ${p}=    Evaluate    [p for p in $m["policies"] if p["pe"] == "${SRC_PE}" and p["prefix"] == "${DST_LAN}"][0]
    Should Be Equal    ${p}[via]    ${{[$VIA1, $VIA2]}}    msg=the segment list read back from the PE names the P routers
    Should Be Equal    ${p}[steered]    ${plan}[steered]
    ${meas}=    Http Get    ${PORTAL}/api/steering/measure    pe=${SRC_PE}    tenant=${TENANT}    prefix=${dst}[lan]    timeout=90
    FOR    ${k}    IN    steered    igp
        Should Be True    ${meas}[${k}][rtt_ms] is not None and ${meas}[${k}][rtt_ms] < 25    msg=${k}: ${meas}[${k}]
        Should Be True    ${meas}[${k}][loss] < 0.2    msg=${k}: ${meas}[${k}]
    END

A backup holds the lab and checks itself: the plan against the running lab is empty, a damaged file is refused
    ${b}=    Http Post    ${PORTAL}/api/backups    timeout=300
    Should Be Equal As Integers    ${b}[status]    200
    Should Be Empty    ${b}[json][unread]    msg=every router's running configuration was read
    Set Suite Variable    ${BACKUP}    ${b}[json][file]
    Dictionary Should Contain Key    ${b}[json][files]    lab.conf
    FOR    ${n}    IN    @{VYOS}
        Dictionary Should Contain Key    ${b}[json][files]    running/${n}.txt
    END
    Length Should Be    ${b}[json][steering]    1    msg=the steering policy is in the backup
    ${plan}=    Http Get    ${PORTAL}/api/backups/${BACKUP}/plan    timeout=180
    Should Be True    ${plan}[same]    msg=a fresh backup matches the lab: ${plan}
    ${data}=    Http Get Bytes    ${PORTAL}/api/backups/${BACKUP}
    ${bad}=    Evaluate    $data[:len($data) // 2] + bytes(64) + $data[len($data) // 2 + 64:]
    ${r}=    Http Post Raw    ${PORTAL}/api/backups/upload    ${bad}    application/gzip
    Should Be Equal As Integers    ${r}[status]    422    msg=a damaged backup is refused

A restore puts the steering back as the backup had it
    Steer    del    ${SRC_PE}    ${TENANT}    ${DST_LAN}
    ${plan}=    Http Get    ${PORTAL}/api/backups/${BACKUP}/plan    timeout=180
    Should Be Empty    ${plan}[problems]
    Should Be Empty    ${plan}[add]
    Should Be Empty    ${plan}[remove]
    Length Should Be    ${plan}[steering_add]    1    msg=the backup's policy is missing from the lab
    Should Be Equal    ${plan}[steering_add][0][via]    ${{[$VIA1, $VIA2]}}
    ${runs}=    Http Get    ${PORTAL}/api/runs
    ${busy}=    Evaluate    any(r["status"] in ("queued", "running") for r in $runs)
    Skip If    ${busy}    a portal run is active (this suite runs inside it): the plan is checked, the restore run is not started
    ${r}=    Http Post    ${PORTAL}/api/runs    mode=restore    name=${BACKUP}    options=${{{"test": False}}}
    Should Be Equal As Integers    ${r}[status]    200
    Wait For Portal Run    ${PORTAL}    ${r}[json][id]
    ${show}=    Steer    show    ${SRC_PE}
    Should Contain    ${show}    ${DST_LAN}    msg=the restore put the policy back

*** Keywords ***
Remove The Policy And Close Connections
    IF    $DST_LAN is not None    Run Keyword And Ignore Error    Steer    del    ${SRC_PE}    ${TENANT}    ${DST_LAN}
    Close All Connections
