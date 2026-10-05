*** Settings ***
Documentation     Operations features of the portal: tenant SLA probes (every pair of a tenant's sites, host to host, delay and
...               loss against targets, exported to Prometheus with alert rules), the capacity view (what runs out first), steering
...               on the map (a policy's path beside the IGP's, both measured), what-if failures on the model, traffic on the map (counters and sFlow), router health scores, and backup / restore (the whole lab in one file; a
...               restore puts the steering back as the backup had it). The restore run is skipped when this suite itself runs
...               inside a portal run (runs are one at a time); `./lab.sh test` from a shell runs it.
Resource          ../resources/common.resource
Library           Process
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

What-if: with nothing failed nothing is affected, and the triangle survives any one P router
    [Documentation]    The model's baseline, then each P router failed in turn: a pair may get longer or lose an
    ...                equal-cost path, but none is cut off — every PE has two P routers.
    ${w}=    Http Get    ${PORTAL}/api/whatif    refresh=true    timeout=120
    ${pairs}=    Evaluate    sum(len(s) * (len(s) - 1) // 2 for s in $SITES.values())
    Length Should Be    ${w}[pairs]    ${pairs}    msg=every unordered pair of a tenant's sites is judged
    Should Be Equal As Integers    ${w}[summary][pairs][unaffected]    ${pairs}
    Should Be Empty    ${w}[control]
    FOR    ${p}    IN    @{PS}
        ${f}=    Http Get    ${PORTAL}/api/whatif    fail=${p}
        Should Be Equal As Integers    ${f}[summary][pairs][cut]    0    msg=${p} down should cut no pair off
        Should Be Equal As Integers    ${f}[summary][internet][cut]    0    msg=${p} down should keep the internet reachable
    END

What-if judges a steering policy the way the lab builds it
    [Documentation]    The policy from the test above (${SRC_PE} → ${TENANT} at dc3 via ${VIA1} ${VIA2}) is a static route pinned to
    ...                ${SRC_PE}'s link to ${VIA1}: losing that link or a waypoint black-holes it; losing the link between the
    ...                waypoints only reroutes that leg along the IGP.
    Http Get    ${PORTAL}/api/whatif    refresh=true    timeout=120
    ${link}=    Evaluate    "~".join(sorted(["${SRC_PE}", "${VIA1}"]))
    ${legs}=    Evaluate    "~".join(sorted(["${VIA1}", "${VIA2}"]))
    FOR    ${fail}    ${want}    IN    ${link}    blackhole    ${VIA2}    blackhole    ${legs}    rerouted
        ${w}=    Http Get    ${PORTAL}/api/whatif    fail=${fail}
        ${p}=    Evaluate    [p for p in $w["policies"] if p["pe"] == "${SRC_PE}" and p["prefix"] == "${DST_LAN}"][0]
        Should Be Equal    ${p}[fate][verdict]    ${want}    msg=${fail} down: ${p}[fate]
    END
    Should Be Equal    ${p}[fate][path][0]    ${SRC_PE}
    Should Be Equal    ${p}[fate][path][-1]    pe3
    Should Be True    ${p}[fate][extra_hops] > 0    msg=the rerouted leg should be longer: ${p}[fate]

What-if: losing every route reflector cuts everything off, and losing the breakout PE takes the internet away
    ${w}=    Http Get    ${PORTAL}/api/whatif    fail=${RRS}
    Should Be Equal As Integers    ${w}[summary][pairs][cut]    ${{len($w["pairs"])}}    msg=with no reflector every VPN route expires
    Should Be Equal    ${w}[control][0][severity]    critical
    ${inet}=    Http Get    ${PORTAL}/api/whatif    fail=${SERVICE}[internet][pe]
    Should Be Equal As Integers    ${inet}[summary][internet][cut]    ${{len($inet["internet"])}}
    ${err}=    Run Keyword And Expect Error    *422*    Http Get    ${PORTAL}/api/whatif    fail=p9

Traffic: every direction of every core link has its load, and a share for what it carries
    ${t}=    Http Get    ${PORTAL}/api/traffic    window=5m    timeout=60
    ${core}=    Evaluate    [l for l in $LINKS if $NODES[l["a"]]["role"] in ("p", "pe") and $NODES[l["b"]]["role"] in ("p", "pe")]
    Length Should Be    ${t}[links]    ${{len($core)}}
    FOR    ${l}    IN    @{t}[links]
        FOR    ${lane}    IN    @{l}[lanes]
            Should Be True    ${lane}[bps] is not None and ${lane}[bps] > 0    msg=${lane}[from] → ${lane}[to]: no load from its counter
            Should Not Be Empty    ${lane}[parts]    msg=${lane}[from] → ${lane}[to]: no sFlow samples in 5 minutes
        END
    END

Traffic: a steered tenant's packets are named on every link of the policy's path
    [Documentation]    iperf from the tenant's dc1 host to its dc3 host, while the policy from the steering test above pins
    ...                ${SRC_PE} → dc3 to ${VIA1} ${VIA2}: the traffic map must name it on the first lane, put it in the matrix
    ...                between the right PEs, and see the policy's packets on exactly the policy's path.
    ${a}=    Set Variable    ${SITES}[${TENANT}][dc1]
    ${z}=    Set Variable    ${SITES}[${TENANT}][dc3]
    Http Get    ${PORTAL}/api/whatif    refresh=true    timeout=120
    Http Get    ${PORTAL}/api/iperf    src=${a}[host]    dst=${z}[host]    seconds=15    timeout=120
    Wait Until Keyword Succeeds    90 s    10 s    Steered Traffic Should Be Seen    ${a}[pe]    ${z}[pe]

The portal refuses to change the lab while a test run holds it, but not the suites' own requests
    [Documentation]    A steering change or a tenant run from the portal during a test run fails tests that are not broken
    ...                (CI run 16 lost seven cases to one), so the portal answers 409 while /tmp/srv6-core-test.lock is held.
    ...                Run by tests/run.sh, this suite holds it already; run on its own, it takes it for a moment.
    ${busy}=    Http Get    ${PORTAL}/api/lab/busy
    IF    not ${busy}[test_run]
        Start Process    flock    /tmp/srv6-core-test.lock    sleep    20    alias=lock
        Wait Until Keyword Succeeds    10 s    1 s    Lab Should Be Busy
    END
    TRY
        ${add}=    Evaluate    requests.post("${PORTAL}/api/steering", json={"pe": "${SRC_PE}", "tenant": "${TENANT}", "prefix": "192.0.2.0/24", "via": ["${VIA1}"]}, timeout=30).status_code    modules=requests
        Should Be Equal As Integers    ${add}    409    msg=adding a policy during a test run should be refused
        ${del}=    Evaluate    requests.delete("${PORTAL}/api/steering", params={"pe": "${SRC_PE}", "tenant": "${TENANT}", "prefix": "192.0.2.0/24"}, timeout=30).status_code    modules=requests
        Should Be Equal As Integers    ${del}    409    msg=removing a policy during a test run should be refused
        ${run}=    Evaluate    requests.post("${PORTAL}/api/runs", json={"mode": "remove", "name": "no-such-tenant"}, timeout=30).status_code    modules=requests
        Should Be Equal As Integers    ${run}    409    msg=a remove run during a test run should be refused
        # the suites mark their own requests: the guard steps aside and the request reaches validation
        ${own}=    Http Post    ${PORTAL}/api/runs    mode=remove    name=no-such-tenant
        Should Be Equal As Integers    ${own}[status]    422    msg=the suites' own request should get past the guard: ${own}
    FINALLY
        IF    not ${busy}[test_run]    Terminate Process    lock
    END

Health: every router is scored, and every point taken off has a reason
    ${h}=    Http Get    ${PORTAL}/api/health    window=1h    timeout=60
    ${routers}=    Evaluate    sorted(n for n, v in $NODES.items() if v["role"] in ("p", "pe", "ce", "fw"))
    Should Be Equal    ${{sorted(r["node"] for r in $h["routers"])}}    ${routers}
    FOR    ${r}    IN    @{h}[routers]
        Should Be Equal As Integers    ${r}[score]    ${{max(0, 100 - sum(d["points"] for d in $r["deductions"]))}}    msg=${r}[node]: the score is not 100 minus its deductions
        FOR    ${d}    IN    @{r}[deductions]
            Should Be True    ${d}[points] > 0 and len("${d}[what]") > 5    msg=${r}[node]: a deduction without a reason: ${d}
        END
    END
    ${text}=    Http Get    ${PORTAL}/metrics
    ${g}=    Metric Samples    ${text}    lab_router_health
    Length Should Be    ${g}    ${{len($routers)}}

Health: a BGP session shut now costs both ends their points, with the reason, until it is back
    [Documentation]    ce3's tenant-b session to its PE is shut for a minute (as suite 15 does): the PE and the CE each lose
    ...                points for it — the PE 20 for a session not Established, the CE 10 for one it shut itself — as a fault
    ...                *now*, whatever the window, and get them back.
    ${pe}=    Set Variable    ${SITES}[tenant-b][${NODES}[ce3][dc]][pe]
    ${peer}=    Set Variable    ${SITES}[tenant-b][${NODES}[ce3][dc]][pe_wan_ip]
    TRY
        Configure    ce3    set vrf name tenant-b protocols bgp neighbor ${peer} shutdown
        Wait Until Keyword Succeeds    3 min    10 s    Session Down Should Cost Points    ${pe}    ce3
    FINALLY
        Configure    ce3    delete vrf name tenant-b protocols bgp neighbor ${peer} shutdown
    END
    Wait Until Keyword Succeeds    3 min    10 s    No Session Should Be Down    ${pe}    ce3

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
Steered Traffic Should Be Seen
    [Arguments]    ${ingress}    ${egress}
    ${t}=    Http Get    ${PORTAL}/api/traffic    window=1m    timeout=60
    ${p}=    Evaluate    [p for p in $t["policies"] if p["pe"] == "${SRC_PE}" and p["prefix"] == "${DST_LAN}"][0]
    Should Be True    ${p}[matches] and not ${p}[missing]    msg=the policy's packets: seen on ${p}[seen], missing on ${p}[missing]
    ${m}=    Evaluate    [m for m in $t["matrix"] if m["tenant"] == "${TENANT}" and m["ingress"] == "${ingress}" and m["egress"] == "${egress}"]
    Should Not Be Empty    ${m}    msg=no ${TENANT} ${ingress} → ${egress} in the matrix: ${t}[matrix]
    ${lane}=    Evaluate    [x for l in $t["links"] for x in l["lanes"] if x["from"] == "${SRC_PE}" and x["to"] == "${VIA1}"][0]
    Should Be Equal    ${lane}[parts][0][what]    ${TENANT} → ${egress} (steered)    msg=${SRC_PE} → ${VIA1} carries ${lane}[parts]

Lab Should Be Busy
    ${b}=    Http Get    ${PORTAL}/api/lab/busy
    Should Be True    ${b}[test_run]

Session Down Should Cost Points
    [Arguments]    @{nodes}
    ${h}=    Http Get    ${PORTAL}/api/health    window=1h    timeout=60
    FOR    ${n}    IN    @{nodes}
        ${r}=    Evaluate    [r for r in $h["routers"] if r["node"] == "${n}"][0]
        ${d}=    Evaluate    [d for d in $r["deductions"] if d.get("now") and d["what"].startswith(("BGP not Established", "BGP shut down"))]
        Should Not Be Empty    ${d}    msg=${n}: no deduction for the shut session yet: ${r}[deductions]
        Should Be True    ${r}[score] <= 90
    END

No Session Should Be Down
    [Arguments]    @{nodes}
    ${h}=    Http Get    ${PORTAL}/api/health    window=1h    timeout=60
    FOR    ${n}    IN    @{nodes}
        ${r}=    Evaluate    [r for r in $h["routers"] if r["node"] == "${n}"][0]
        Should Be Equal As Integers    ${r}[sessions][bgp_down]    0    msg=${n}: still a session down: ${r}[deductions]
    END

Remove The Policy And Close Connections
    IF    $DST_LAN is not None    Run Keyword And Ignore Error    Steer    del    ${SRC_PE}    ${TENANT}    ${DST_LAN}
    Close All Connections
