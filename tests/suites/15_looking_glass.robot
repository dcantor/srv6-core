*** Settings ***
Documentation     BGP looking glass: a small Alpine VM (lg) holds the core's whole VPNv4 / VPNv6 table with the attributes the
...               PEs originated: route distinguisher, route targets, SRv6 SID and label, originator and cluster list, and the
...               PE's loopback as the next hop. With LG_FEED=bmp (the default) every route reflector streams it over **BMP**
...               (RFC 7854) — its Loc-RIB, and each PE's Adj-RIB-In before policy, with every session's up / down — decoded by
...               lgd itself; with LG_FEED=session FRR on the VM holds an iBGP session to every reflector instead (the original
...               design, which needs extended next-hop encoding to keep the PE's loopback as the next hop). On top of that
...               the lgd service keeps a history in SQLite — every announce, every attribute change with the fields that changed,
...               every withdraw — polls each PE's and CE's per-VRF table over SSH for the tenant view after import, and serves
...               both through an API and a web page. It also reads **every router directly**: its RIB, its own VPN table and
...               its IS-IS adjacencies through the router's HTTPS API (VyOS `service https api`, JSON), and its per-VRF BGP
...               tables with vtysh over SSH — VyOS's op-mode has no `json` for those. Every row it stores says which of the
...               ways it came in (`bmp-loc-rib`, `bmp-pre-policy`, `rr-session`, `router-api`, `router-ssh`), so the same prefix can be held against
...               itself. These tests check the collector against the reflectors it peers with, the API against the routers it
...               describes, the paths it draws against the routers' own forwarding state, and the history against a change
...               this suite makes and undoes.
Resource          ../resources/common.resource
Library           OperatingSystem
Suite Setup       Require Looking Glass
Suite Teardown    Run Keywords    Restore Ce Session    AND    Suite Teardown Close Connections

*** Variables ***
${FLAP_CE}        ce3
${FLAP_TENANT}    tenant-b

*** Keywords ***
Require Looking Glass
    Skip If    not $LG    no looking glass in lab.conf (role lg)
    Tcp Port Should Be Open    ${NODES}[${LG}][mgmt_ip]    ${LG_PORT}

Lg
    [Documentation]    GET a looking-glass API path (query parameters as named arguments).
    [Arguments]    ${path}    &{params}
    ${d}=    Http Get    ${LG_URL}${path}    &{params}
    RETURN    ${d}

Capture Has Packets While Running
    [Arguments]    ${id}
    ${v}=    Lg    /api/capture/${id}
    Should Be Equal    ${v}[status]    running
    Should Be True    ${v}[count] > 0    msg=no packets have arrived yet

Capture Has Ended
    [Arguments]    ${id}
    ${v}=    Lg    /api/capture/${id}
    Should Not Contain    ${{["running", "starting"]}}    ${v}[status]

Flap Lan
    [Documentation]    The LAN of ${FLAP_CE} in ${FLAP_TENANT} — the prefix this suite withdraws and brings back.
    ${lan}=    Set Variable    ${SITES}[${FLAP_TENANT}][${NODES}[${FLAP_CE}][dc]][lan]
    RETURN    ${lan}

Restore Ce Session
    [Documentation]    Always put the session this suite shut back (teardown runs even when a test failed halfway).
    Run Keyword And Ignore Error    Configure    ${FLAP_CE}
    ...    delete vrf name ${FLAP_TENANT} protocols bgp neighbor ${SITES}[${FLAP_TENANT}][${NODES}[${FLAP_CE}][dc]][pe_wan_ip] shutdown

Run Lg Command
    [Documentation]    A shell command on the looking-glass VM (Alpine, the same credentials as the tenant hosts).
    [Arguments]    ${command}
    ${out}=    Host Command    ${MGMT}[${LG}]    ${command}
    RETURN    ${out}

Prefix Should Be Gone
    [Arguments]    ${prefix}    ${vrf}
    ${paths}=    Collector Paths For    ${prefix}    ${vrf}
    Should Be Empty    ${paths}    msg=${prefix} is still in the collector's table

Rib Should Be Recorded
    [Documentation]    Wait until the collector has stored a RIB entry of the given protocol for the prefix.
    [Arguments]    ${prefix}    ${vrf}    ${node}    ${protocol}
    ${d}=    Lg    /api/prefixes    source=${node}    safi=rib    vrf=${vrf}    prefix=${prefix}
    ${protos}=    Evaluate    [p["attrs"].get("protocol") for p in $d["paths"] if p["attrs"].get("installed")]
    Should Contain    ${protos}    ${protocol}    msg=${node}: no installed ${protocol} route for ${prefix} recorded yet

Path Should Be Steered
    [Arguments]    ${prefix}    ${vrf}    ${from}
    ${d}=    Lg    /api/path    prefix=${prefix}    vrf=${vrf}    from=${from}
    Should Be True    ${d}[steered]    msg=the looking glass does not see the steering policy yet

Path Should Not Be Steered
    [Arguments]    ${prefix}    ${vrf}    ${from}
    ${d}=    Lg    /api/path    prefix=${prefix}    vrf=${vrf}    from=${from}
    Should Not Be True    ${d}[steered]    msg=the looking glass still shows the prefix as steered

Prefix Should Be Back
    [Arguments]    ${prefix}    ${vrf}
    ${paths}=    Collector Paths For    ${prefix}    ${vrf}
    Should Not Be Empty    ${paths}    msg=${prefix} has not come back

Collector Paths For
    [Documentation]    The collector's paths for a prefix, as the API returns them.
    [Arguments]    ${prefix}    ${vrf}=${EMPTY}
    ${d}=    Lg    /api/prefixes    source=collector    prefix=${prefix}    vrf=${vrf}    limit=50
    RETURN    ${d}[paths]

BMP Monitors Should Be Back
    [Arguments]    ${rr}
    ${cfg}=    Shell    ${rr}    sudo vtysh -c 'show running-config bgpd'
    FOR    ${m}    IN    ipv4 vpn pre-policy    ipv4 vpn loc-rib    ipv6 vpn pre-policy    ipv6 vpn loc-rib
        Should Contain    ${cfg}    bmp monitor ${m}    msg=${rr}: bmp monitor ${m} is missing after the commit
    END

Feed Should Be Synced And Whole
    [Arguments]    ${rr}    ${paths}
    ${s}=    Lg    /api/status
    ${feed}=    Evaluate    [x for x in $s["collector"]["peers"] if x["name"] == "${rr}"][0]
    Should Be Equal    ${feed}[state]    up
    Should Be True    ${feed}[synced]
    ${now}=    Lg    /api/prefixes    source=collector    peer=${rr}    limit=2000
    Should Be Equal As Integers    ${now}[total]    ${paths}    msg=${rr}: ${now}[total] paths in the core's table after the commit, ${paths} before

*** Test Cases ***
The looking glass is up, and says which lab and which collector it is
    ${s}=    Lg    /api/status
    Should Be Equal    ${s}[lab]    srv6-core
    Should Be Equal    ${s}[node]    ${LG}
    Should Be Equal As Integers    ${s}[collector][asn]    ${CORE_AS}
    Should Be Equal    ${s}[collector][router_id]    ${NODES}[${LG}][router_id]
    Length Should Be    ${s}[collector][peers]    ${{len($LG_PEERS)}}
    Should Be True    ${s}[db][events] > 0    msg=the collector has recorded no events at all

Every route reflector feeds it, and nothing flows back
    [Documentation]    BMP: each reflector's connection is up, it has finished replaying its tables (End-of-RIB), the
    ...                reflector itself reports the connection Up, and it no longer holds a BGP session to the collector.
    ...                Session: the iBGP session is Established, and the collector has announced nothing to the reflector.
    ${s}=    Lg    /api/status
    IF    $LG_FEED == "bmp"
        FOR    ${rr}    IN    @{LG_PEERS}
            ${p}=    Set Variable    ${LG_PEERS}[${rr}]
            ${feed}=    Evaluate    [x for x in $s["collector"]["peers"] if x["name"] == "${rr}"][0]
            Should Be Equal    ${feed}[state]    up    msg=${rr}'s BMP connection is ${feed}[state]
            Should Be True    ${feed}[synced]    msg=${rr} has not finished replaying its tables
            Should Contain    ${feed}[eor]    loc-rib ipv4 vpn    msg=no End-of-RIB for ${rr}'s Loc-RIB: ${feed}[eor]
            ${bmp}=    Shell    ${rr}    sudo vtysh -c 'show bmp'
            Should Match Regexp    ${bmp}    ${p}[lg_ip]:\\d+\\s+Up    msg=${rr} does not report its BMP connection to the collector as Up
            ${sum}=    Vyos    ${rr}    show bgp ipv4 vpn summary
            Should Not Contain    ${sum}    ${p}[lg_ip]    msg=${rr} still has a BGP session to the collector
        END
    ELSE
        FOR    ${rr}    IN    @{LG_PEERS}
            ${p}=    Set Variable    ${LG_PEERS}[${rr}]
            ${peer}=    Evaluate    [x for x in $s["collector"]["peers"] if x["ip"] == "${p}[rr_ip]"][0]
            Should Be Equal    ${peer}[state]    Established    msg=the collector's session to ${rr} is ${peer}[state]
            Should Be Equal As Integers    ${peer}[remote_as]    ${CORE_AS}
            # ...and from the reflector's side: the session is up and the collector has sent it nothing (PfxSnt is ours, State/PfxRcd theirs)
            ${sum}=    Vyos    ${rr}    show bgp ipv4 vpn summary
            ${line}=    Get Lines Containing String    ${sum}    ${p}[lg_ip]
            Should Match Regexp    ${line}    ^${p}[lg_ip]\\s+4\\s+${CORE_AS}\\s+\\d+\\s+\\d+\\s+\\d+\\s+\\d+\\s+\\d+\\s+\\S+\\s+0\\s
            ...    msg=${rr}: the collector is not Established with 0 prefixes received from it — ${line}
        END
    END

It holds exactly what the reflectors hold: every VPNv4 and VPNv6 prefix, once per reflector
    ${rr}=    Set Variable    ${RRS}[0]
    FOR    ${afi}    IN    ipv4    ipv6
        ${table}=    Vyos    ${rr}    show bgp ${afi} vpn
        ${on_rr}=    Regex Findall    ${table}    (?m)^\\s*\\*>?i?\\s*(\\S+/\\d+)
        ${d}=    Lg    /api/prefixes    source=collector    afi=${afi}    safi=vpn    limit=2000
        ${in_lg}=    Evaluate    sorted({p["prefix"] for p in $d["paths"]})
        Should Be Equal    ${in_lg}    ${{sorted(set($on_rr))}}    msg=${afi} vpn: the collector and ${rr} do not hold the same prefixes
        # ...and one path per reflector for every route, counted per (RD, prefix): the same prefix can live in two VRFs
        # (0.0.0.0/0 from the breakout does, once per tenant), which is one route each and not one prefix
        ${per_route}=    Evaluate    sorted({c for c in collections.Counter((p["rd"], p["prefix"]) for p in $d["paths"]).values()})    modules=collections
        Should Be Equal    ${per_route}    ${{[len($LG_PEERS)]}}
        ...    msg=${afi} vpn: every route should be held once per reflector, path counts seen: ${per_route}
    END

Every tenant LAN carries the attributes of the PE that originated it: RD, route target, SRv6 SID and the PE's loopback as next hop
    FOR    ${t}    IN    @{TENANTS}
        FOR    ${dc}    IN    @{SITES}[${t}]
            ${s}=    Set Variable    ${SITES}[${t}][${dc}]
            ${paths}=    Collector Paths For    ${s}[lan]    ${t}
            Should Not Be Empty    ${paths}    msg=${s}[lan] (${t}) is not in the collector's table at all
            FOR    ${p}    IN    @{paths}
                Should Be Equal    ${p}[rd]    ${s}[rd]                                  msg=${s}[lan]: wrong route distinguisher
                Should Be Equal    ${p}[vrf]    ${t}                                     msg=${s}[lan]: the RD was not resolved to ${t}
                Should Be Equal    ${p}[nexthop]    ${LOOPBACK}[${s}[pe]]                msg=${s}[lan]: next hop is not ${s}[pe]'s loopback (extended next hop lost?)
                Should Be Equal    ${p}[origin_node]    ${s}[pe]                         msg=${s}[lan]: not attributed to ${s}[pe]
                Should Be Equal As Integers    ${p}[origin_as]    ${NODES}[${s}[ce]][asn]    msg=${s}[lan]: origin AS is not the CE's
                Should Be Equal    ${p}[attrs][ext_communities]    RT:${VRF_RT}[${t}]    msg=${s}[lan]: wrong route target
                Should Be True    $p["attrs"].get("sid", "").startswith("${{$LOCATOR[$s['pe']].split('::')[0]}}")
                ...    msg=${s}[lan]: the SRv6 SID ${p}[attrs][sid] does not come from ${s}[pe]'s locator ${LOCATOR}[${s}[pe]]
                Should Be True    $p["attrs"].get("label") is not None                   msg=${s}[lan]: no VPN label
            END
        END
    END

The tenants stay apart in the looking glass too: no prefix appears under two VRFs
    ${d}=    Lg    /api/prefixes    source=collector    limit=2000
    ${by_prefix}=    Evaluate    {p["prefix"]: set() for p in $d["paths"]}
    FOR    ${p}    IN    @{d}[paths]
        Evaluate    $by_prefix[$p["prefix"]].add($p["vrf"])
    END
    ${shared}=    Evaluate    {k: sorted(v) for k, v in $by_prefix.items() if len(v) > 1 and k != "0.0.0.0/0"}
    Should Be Empty    ${shared}    msg=these prefixes appear in more than one tenant VRF: ${shared}

The per-VRF views match the routers they were polled from
    FOR    ${pe}    IN    @{PES}
        FOR    ${t}    IN    @{TENANTS}
            ${table}=    Vyos    ${pe}    show bgp vrf ${t} ipv4 unicast
            ${on_pe}=    Regex Findall    ${table}    (?m)^\\s*\\*[>=]?\\s*(\\S+/\\d+)
            ${d}=    Lg    /api/prefixes    source=${pe}    vrf=${t}    afi=ipv4    safi=unicast    limit=2000
            ${in_lg}=    Evaluate    sorted({p["prefix"] for p in $d["paths"]})
            Should Be Equal    ${in_lg}    ${{sorted(set($on_pe))}}    msg=${pe} ${t}: the looking glass and the router disagree
        END
    END
    ${polls}=    Lg    /api/peers
    FOR    ${p}    IN    @{polls}[polls]
        Should Be Equal As Integers    ${p}[ok]    1    msg=the ${p}[source] collection is failing: ${p}[error]
        Should Be True    ${p}[ts] > ${{time.time() - 600}}    msg=the ${p}[source] view is stale (last collected ${p}[ts])
    END

The filters answer the questions a looking glass is asked: by VRF, by RD, by origin AS and by free text
    ${a}=    Lg    /api/prefixes    source=collector    vrf=${TENANTS}[0]    limit=2000
    Should Be True    all(p["vrf"] == "${TENANTS}[0]" for p in $a["paths"])    msg=the VRF filter let another tenant through
    ${rd}=    Set Variable    ${SITES}[${TENANTS}[0]][${NODES}[${CES}[0]][dc]][rd]
    ${b}=    Lg    /api/prefixes    source=collector    rd=${rd}    limit=2000
    Should Not Be Empty    ${b}[paths]
    Should Be True    all(p["rd"] == "${rd}" for p in $b["paths"])            msg=the RD filter let another RD through
    ${as}=    Set Variable    ${NODES}[${CES}[0]][asn]
    ${c}=    Lg    /api/prefixes    source=collector    origin_as=${as}    limit=2000
    Should Not Be Empty    ${c}[paths]
    Should Be True    all(p["origin_as"] == ${as} for p in $c["paths"])       msg=the origin-AS filter let another AS through
    ${d}=    Lg    /api/prefixes    q=RT:${VRF_RT}[${TENANTS}[0]]    source=collector    limit=2000
    Should Not Be Empty    ${d}[paths]                                        msg=searching for a route target found nothing

A live query reaches the router and refuses anything that is not a show, a ping or a traceroute
    ${pe}=    Set Variable    ${PES}[0]
    ${r}=    Http Post    ${LG_URL}/api/query    device=${pe}    command=show bgp vrf ${TENANTS}[0] ipv4 unicast
    Should Be Equal As Integers    ${r}[status]    200    msg=the live query failed: ${r}[json]
    Should Contain    ${r}[json][output]    BGP table version                  msg=that is not the router's answer
    ${direct}=    Vyos    ${pe}    show bgp vrf ${TENANTS}[0] ipv4 unicast
    ${lan}=    Set Variable    ${SITES}[${TENANTS}[0]][${NODES}[${CES}[0]][dc]][lan]
    Should Contain    ${r}[json][output]    ${lan}
    Should Contain    ${direct}    ${lan}
    ${bad}=    Http Post    ${LG_URL}/api/query    device=${pe}    command=configure terminal
    Should Be Equal As Integers    ${bad}[status]    400    msg=the looking glass accepted a configuration command
    ${nodev}=    Http Post    ${LG_URL}/api/query    device=nowhere    command=show bgp summary
    Should Be Equal As Integers    ${nodev}[status]    400    msg=the looking glass accepted an unknown device

A packet capture on a link catches its traffic, decodes it and hands over the pcap, and refuses anything outside the model
    [Documentation]    Clicking a link on the map runs tcpdump on one of its ends over SSH, bounded; here a PE's core link,
    ...                pinging its far end, keeping only pings.
    ${links}=    Lg    /api/capture/links
    ${link}=    Evaluate    next(l for l in $links["links"] if any(e["device"] == "${PES}[0]" for e in l["ends"]) and not l["tenant"])
    ${end}=    Evaluate    next(e for e in $link["ends"] if e["device"] == "${PES}[0]")
    ${r}=    Http Post    ${LG_URL}/api/capture    timeout=120    device=${end}[device]    interface=${end}[interface]
    ...    packets=${10}    seconds=${15}    preset=icmp    ping_peer=${True}
    Should Be Equal As Integers    ${r}[status]    200    msg=the capture failed: ${r}[json]
    Should Be True    ${r}[json][count] >= 2    msg=the pings across ${end}[device] ${end}[interface] were not captured
    Should Be True    any("echo request" in p["info"] for p in $r["json"]["packets"])    msg=no echo request among the packets
    ${pcap}=    Evaluate    requests.get("${LG_URL}/api/capture/${r}[json][id].pcap", timeout=30).content    modules=requests
    Should Be True    $pcap[:4] == bytes.fromhex("d4c3b2a1")    msg=the download is not a pcap file
    ${bad}=    Http Post    ${LG_URL}/api/capture    device=${end}[device]    interface=eth99
    Should Be Equal As Integers    ${bad}[status]    400    msg=a capture was accepted on an interface that is not in the model
    ${inj}=    Http Post    ${LG_URL}/api/capture    device=${end}[device]    interface=${end}[interface]    filter=icmp; reboot
    Should Be Equal As Integers    ${inj}[status]    400    msg=a filter outside the allowed alphabet was accepted

A live capture streams its packets while it runs, stops on request, and still hands over the pcap
    ${links}=    Lg    /api/capture/links
    ${link}=    Evaluate    next(l for l in $links["links"] if any(e["device"] == "${PES}[0]" for e in l["ends"]) and not l["tenant"])
    ${end}=    Evaluate    next(e for e in $link["ends"] if e["device"] == "${PES}[0]")
    ${r}=    Http Post    ${LG_URL}/api/capture    device=${end}[device]    interface=${end}[interface]    packets=${1000}
    ...    seconds=${40}    preset=all    stream=${True}
    Should Be Equal As Integers    ${r}[status]    200    msg=the capture did not start: ${r}[json]
    ${id}=    Set Variable    ${r}[json][id]
    Wait Until Keyword Succeeds    20 s    1 s    Capture Has Packets While Running    ${id}
    ${stop}=    Http Post    ${LG_URL}/api/capture/${id}/stop
    Should Be Equal As Integers    ${stop}[status]    200
    Wait Until Keyword Succeeds    30 s    1 s    Capture Has Ended    ${id}
    ${v}=    Lg    /api/capture/${id}
    Should Be Equal    ${v}[status]    stopped    msg=Stop did not end the capture
    Should Be True    ${v}[took] < 30    msg=the capture ran on after Stop (${v}[took] s of a 40 s capture)
    ${pcap}=    Evaluate    requests.get("${LG_URL}/api/capture/${id}.pcap", timeout=30).content    modules=requests
    Should Be True    $pcap[:4] == bytes.fromhex("d4c3b2a1") and len($pcap) > 24    msg=the stopped capture's pcap is empty

A capture along a prefix's path sees each ping at every hop: plain on the access links, SRv6-encapsulated in the core
    ${lan}=    Set Variable    ${SITES}[${TENANTS}[0]][${NODES}[${CES}[2]][dc]][lan]
    ${g}=    Lg Path Capture    ${LG_URL}    ${lan}    ${TENANTS}[0]    ${CES}[0]
    Should Contain    ${g}[ping_result]    0% packet loss    msg=the pings along the path failed: ${g}[ping_result]
    FOR    ${c}    IN    @{g}[captures]
        Continue For Loop If    $c["peer"].endswith("-h1") and $c["device"] == "${CES}[0]"    # the pings start at this CE
        ${pings}=    Evaluate    [p for p in $c["packets"] if "echo request" in (p["info"] or "")]
        Should Not Be Empty    ${pings}    msg=${c}[device] ${c}[interface] did not see the pings
        IF    $c["tenant"] is None
            Should Be True    all(p["proto"].startswith("SRv6") for p in $pings)    msg=${c}[device] ${c}[interface] is a core link but the pings were not encapsulated
        ELSE
            Should Be True    all(p["proto"] == "ICMP" for p in $pings)    msg=${c}[device] ${c}[interface] is an access link but the pings were encapsulated
        END
    END

A prefix that goes away is recorded as withdrawn, and the table can still be read as it was before
    ${lan}=    Flap Lan
    ${site}=    Set Variable    ${SITES}[${FLAP_TENANT}][${NODES}[${FLAP_CE}][dc]]
    ${before}=    Collector Paths For    ${lan}    ${FLAP_TENANT}
    Should Not Be Empty    ${before}    msg=${lan} is not in the table to begin with
    ${t0}=    Evaluate    time.time()
    Grafana Annotate    srv6-core: looking glass — ${FLAP_CE} stops announcing ${lan} (${FLAP_TENANT})    srv6-core    looking-glass
    Configure    ${FLAP_CE}    set vrf name ${FLAP_TENANT} protocols bgp neighbor ${site}[pe_wan_ip] shutdown
    Wait Until Keyword Succeeds    90 s    10 s    Prefix Should Be Gone    ${lan}    ${FLAP_TENANT}
    ${ev}=    Lg    /api/history    prefix=${lan}    kind=withdraw    limit=20
    Should Not Be Empty    ${ev}[events]    msg=no withdraw was recorded for ${lan}
    Should Be True    ${ev}[events][0][ts] >= ${t0}    msg=the withdraw recorded is older than the change
    # ...and the moment before the change still holds the path, with the attributes it had then
    ${then}=    Lg    /api/state    at=${t0}    prefix=${lan}    source=collector
    Should Not Be Empty    ${then}[paths]    msg=the state before the withdraw no longer shows ${lan}
    Should Be Equal    ${then}[paths][0][nexthop]    ${LOOPBACK}[${site}[pe]]
    # Compare: from just before the change to now, the collector's path for the LAN is gone
    ${t1}=    Evaluate    ${t0} - 1
    ${gone}=    Lg    /api/diff    from=${t1}    source=collector    prefix=${lan}
    Should Be True    any(i["kind"] == "removed" and i["vrf"] == "${FLAP_TENANT}" for i in $gone["items"])    msg=Compare does not show ${lan} removed
    Restore Ce Session
    Wait Until Keyword Succeeds    120 s    10 s    Prefix Should Be Back    ${lan}    ${FLAP_TENANT}
    ${back}=    Lg    /api/history    prefix=${lan}    kind=announce    limit=20
    Should Be True    ${back}[events][0][ts] > ${ev}[events][0][ts]    msg=the prefix came back but no announce was recorded
    # ...and across the whole flap it moved and came back (the same attributes, or changed ones), with events in between
    ${round}=    Lg    /api/diff    from=${t1}    source=collector    prefix=${lan}
    Should Be True    $round["items"] and all(i["kind"] in ("flapped", "changed") and i["events"] >= 2 for i in $round["items"] if i["vrf"] == "${FLAP_TENANT}")
    ...    msg=Compare across the flap: ${round}[counts]

Every router is read, and each part of it through the transport that can express it
    ${d}=    Lg    /api/routers
    ${by}=    Evaluate    {r["name"]: r for r in $d["routers"]}
    FOR    ${n}    IN    @{VYOS}
        Dictionary Should Contain Key    ${by}    ${n}    msg=${n} is not among the routers the looking glass reads
        ${r}=    Set Variable    ${by}[${n}]
        Should Be Equal As Integers    ${r}[poll][ok]    1    msg=${n}: the last collection failed: ${r}[poll][error]
        Should Contain    ${r}[poll][via]    router-api    msg=${n}: nothing was read through the router's own API
        Should Be True    ${r}[poll][detail][rib] > 0    msg=${n}: no RIB entries were collected
        IF    $r["role"] in ("pe", "ce", "fw")
            Should Contain    ${r}[poll][via]    router-ssh    msg=${n}: the per-VRF BGP tables were not read
            Should Be True    ${r}[poll][detail][bgp] > 0    msg=${n}: no BGP paths were collected
        END
        IF    $r["role"] in ("pe", "p")
            ${up}=    Evaluate    [a for a in ($r["adjacencies"] or []) if (a.get("state") or "").lower() == "up"]
            Should Be Equal As Integers    ${{len($up)}}    ${{len($ISIS_NEIGHBORS["${n}"])}}
            ...    msg=${n}: the looking glass sees ${{len($up)}} IS-IS adjacencies, the topology has ${{len($ISIS_NEIGHBORS["${n}"])}}
        END
    END

The routers' own tables agree with the routers, and the RIB carries the SRv6 encapsulation
    ${pe}=    Set Variable    ${PES}[0]
    ${t}=    Set Variable    ${TENANTS}[0]
    ${dc}=    Evaluate    [d for d in $SITES["${t}"] if $SITES["${t}"][d]["pe"] != "${pe}"][0]
    ${lan}=    Set Variable    ${SITES}[${t}][${dc}][lan]
    # the RIB, read through the router's API: same route, and it shows the encapsulation the router installed
    ${rib}=    Lg    /api/prefixes    source=${pe}    safi=rib    vrf=${t}    prefix=${lan}
    Should Not Be Empty    ${rib}[paths]                                     msg=${pe}: ${lan} is not in the collected RIB
    Should Be Equal    ${rib}[paths][0][via]    router-api
    ${attrs}=    Set Variable    ${rib}[paths][0][attrs]
    Should Be Equal    ${attrs}[protocol]    bgp
    ${segs}=    Evaluate    [n.get("seg6", {}).get("segs") for n in $attrs["nexthops"] if n.get("seg6")]
    Should Not Be Empty    ${segs}                                           msg=${pe}: the RIB entry carries no SRv6 segment
    Should Be True    $segs[0].startswith("${{$LOCATOR[$SITES[$TENANTS[0]][$dc]['pe']].split('::')[0]}}")
    ...    msg=${pe}: ${lan} is encapsulated to ${segs}[0], which is not out of ${SITES}[${t}][${dc}][pe]'s locator
    ${live}=    Shell    ${pe}    sudo ip -c=never route show vrf ${t} ${lan}
    Should Contain    ${live}    ${segs}[0]                                  msg=${pe}: the router does not install ${segs}[0] for ${lan}
    # the reflector's own VPN table, also read through its API, holds what the session delivered
    ${rr}=    Set Variable    ${RRS}[0]
    ${own}=    Lg    /api/prefixes    source=${rr}    safi=vpn    prefix=${lan}
    Should Not Be Empty    ${own}[paths]                                     msg=${rr}: ${lan} is missing from its own VPN table
    Should Be Equal    ${own}[paths][0][via]    router-api
    ${core}=    Lg    /api/prefixes    source=collector    safi=vpn    prefix=${lan}    best_only=1
    Should Be Equal    ${core}[paths][0][via]    ${LG_VIA}
    Should Be Equal    ${own}[paths][0][rd]    ${core}[paths][0][rd]      msg=the two views disagree about the RD of ${lan}
    Should Be Equal    ${own}[paths][0][nexthop]    ${core}[paths][0][nexthop]    msg=the two views disagree about the next hop of ${lan}

The path it draws for a prefix is the one the routers actually take
    ${t}=    Set Variable    ${TENANTS}[0]
    ${dst}=    Evaluate    [d for d in $SITES["${t}"] if $SITES["${t}"][d]["pe"] != "${PES}[0]"][0]
    ${site}=    Set Variable    ${SITES}[${t}][${dst}]
    ${d}=    Lg    /api/path    prefix=${site}[lan]    vrf=${t}    from=${PES}[0]
    ${nodes}=    Evaluate    [h["node"] for h in $d["hops"]]
    Should Contain    ${nodes}    ${PES}[0]
    Should Contain    ${nodes}    ${site}[pe]                                msg=the path never reaches ${site}[pe], which owns ${site}[lan]
    Should Contain    ${nodes}    ${site}[ce]
    Should Be Equal    ${d}[egress]    ${site}[pe]
    Should Be Equal    ${d}[sources][control_plane]    ${LG_VIA}            msg=the attributes should come from the collector's own feed
    Should Be Equal    ${d}[sources][forwarding]    router-api               msg=the forwarding decision should come from the router's own API
    # the P routers on the drawn path are the ones the ingress PE really forwards through
    ${crossed}=    Evaluate    [h["node"] for h in $d["hops"] if h["role"] == "p"]
    Should Not Be Empty    ${crossed}                                       msg=the path crosses no P router
    ${dev}=    Set Variable    ${d}[live][dev]
    ${peer}=    Evaluate    [l["b"] if l["a"] == "${PES}[0]" else l["a"] for l in $LINKS if ("${PES}[0]", "${dev}") in ((l["a"], l["a_port"]), (l["b"], l["b_port"]))]
    Should Be Equal    ${crossed}[0]    ${peer}[0]                          msg=${PES}[0] forwards out of ${dev} (to ${peer}[0]) but the path starts with ${crossed}[0]
    Should Be Equal As Integers    ${d}[steered]    ${0}

A steered prefix is drawn along the segment list the policy installed
    ${t}=    Set Variable    ${TENANTS}[0]
    ${dst}=    Evaluate    [d for d in $SITES["${t}"] if $SITES["${t}"][d]["pe"] == "${PES}[2]"][0]
    ${lan}=    Set Variable    ${SITES}[${t}][${dst}][lan]
    Steer    add    ${PES}[0]    ${t}    ${lan}    ${PS}[0]    ${PS}[2]
    TRY
        Wait Until Keyword Succeeds    90 s    10 s    Path Should Be Steered    ${lan}    ${t}    ${PES}[0]
        ${d}=    Lg    /api/path    prefix=${lan}    vrf=${t}    from=${PES}[0]
        ${want}=    Create List    ${PS}[0]    ${PS}[2]
        ${crossed}=    Evaluate    [h["node"] for h in $d["hops"] if h["role"] == "p"]
        Should Be Equal    ${crossed}    ${want}    msg=the steered path crosses ${crossed}, not ${want}
        Should Contain    ${d}[notes][0]    steering policy
    FINALLY
        Steer    del    ${PES}[0]    ${t}    ${lan}
    END
    Wait Until Keyword Succeeds    90 s    10 s    Path Should Not Be Steered    ${lan}    ${t}    ${PES}[0]

The table can be read as it was at any moment, and so can the path
    ${t}=    Set Variable    ${TENANTS}[0]
    ${dst}=    Evaluate    [d for d in $SITES["${t}"] if $SITES["${t}"][d]["pe"] != "${PES}[0]"][0]
    ${site}=    Set Variable    ${SITES}[${t}][${dst}]
    ${lan}=    Set Variable    ${site}[lan]
    ${now}=    Evaluate    time.time()
    # now: the prefix is there, and the path is drawn from the routers' current state
    ${live}=    Lg    /api/path    prefix=${lan}    vrf=${t}    from=${PES}[0]
    Should Not Be True    ${live}[steered]
    # a moment the collector cannot know about yet (before it ever ran) holds nothing
    ${before}=    Evaluate    $now - 86400 * 365
    ${empty}=    Lg    /api/state    at=${before}    prefix=${lan}
    Should Be Equal As Integers    ${empty}[count]    0    msg=the looking glass claims to know ${lan} a year ago
    # steer the prefix, then read both moments back: before the change and after it
    ${mark}=    Evaluate    time.time()
    Steer    add    ${PES}[0]    ${t}    ${lan}    ${PS}[0]    ${PS}[2]
    TRY
        Wait Until Keyword Succeeds    3 min    10 s    Rib Should Be Recorded    ${lan}    ${t}    ${PES}[0]    static
        ${when}=    Evaluate    time.time()
        ${then}=    Lg    /api/path    prefix=${lan}    vrf=${t}    from=${PES}[0]    at=${when}
        Should Be True    ${then}[steered]    msg=the moment ${when} should show the steering policy
        ${crossed}=    Evaluate    [h["node"] for h in $then["hops"] if h["role"] == "p"]
        ${want}=    Create List    ${PS}[0]    ${PS}[2]
        Should Be Equal    ${crossed}    ${want}
        # ...and the moment before the policy was applied still shows the shortest path
        ${earlier}=    Lg    /api/path    prefix=${lan}    vrf=${t}    from=${PES}[0]    at=${mark}
        Should Not Be True    ${earlier}[steered]    msg=the moment before the policy was applied should not be steered
        ${state}=    Lg    /api/state    at=${mark}    prefix=${lan}
        Should Be True    ${state}[count] > 0    msg=the table at ${mark} holds no path for ${lan}
    FINALLY
        Steer    del    ${PES}[0]    ${t}    ${lan}
    END

The numbers are exported for Prometheus, and the portal points the scraper at them
    ${text}=    Http Get    ${LG_URL}/metrics
    ${up}=    Metric Samples    ${text}    lg_up
    Should Be Equal As Numbers    ${up}[0][value]    1
    ${sessions}=    Metric Samples    ${text}    lg_session_up
    Should Be True    all(s["value"] == 1 for s in $sessions) and len($sessions) == ${{len($LG_PEERS)}}    msg=lg_session_up: ${sessions}
    IF    $LG_FEED == "bmp"
        ${peers}=    Metric Samples    ${text}    lg_bmp_peer_up
        Should Be True    len($peers) == len($PES) * len($LG_PEERS) and all(s["value"] == 1 for s in $peers)
        ...    msg=lg_bmp_peer_up should report every PE up at every reflector: ${peers}
    END
    ${paths}=    Metric Samples    ${text}    lg_paths
    ${core}=    Evaluate    [s for s in $paths if s["labels"]["source"] == "collector"]
    Should Not Be Empty    ${core}                                          msg=no lg_paths for the collector's own table
    ${sd}=    Http Get    http://127.0.0.1:8091/api/sd
    ${lgt}=    Evaluate    [g for g in $sd if g["labels"].get("job") == "lookingglass"]
    Length Should Be    ${lgt}    1                                         msg=the portal does not advertise the looking glass to Prometheus
    Should Be Equal    ${lgt}[0][targets][0]    ${NODES}[${LG}][mgmt_ip]:${LG_PORT}

Its own configuration is rendered from the model, not kept by hand
    ${want}=    Get File    ${LAB_DIR}/nodes/${LG}/frr.conf
    ${running}=    Run Lg Command    doas cat /etc/frr/frr.conf
    Should Be Equal    ${running.strip()}    ${want.strip()}    msg=/etc/frr/frr.conf on ${LG} is not what tools/render.py renders
    ${cfg}=    Run Lg Command    doas cat /etc/lgd/lgd.json
    ${json}=    Evaluate    json.loads(r'''${cfg}''')
    Should Be Equal    ${json}[node]    ${LG}
    Should Be Equal As Integers    ${json}[collector][asn]    ${CORE_AS}
    Should Be Equal    ${{sorted($json["rd_map"])}}    ${{sorted($RD_MAP)}}    msg=the collector's RD map is not the lab's

Over BMP it also holds what each PE sent each reflector, before any policy
    [Documentation]    The pre-policy view (source adj-in): per reflector and per PE, exactly as many routes as the reflector
    ...                says it received from that PE, and each one names the PE that sent it.
    Skip If    $LG_FEED != "bmp"    the looking glass is fed by the iBGP session (LG_FEED=session): no pre-policy view
    ${d}=    Lg    /api/prefixes    source=adj-in    limit=5000
    FOR    ${rr}    IN    @{LG_PEERS}
        FOR    ${afi}    IN    ipv4    ipv6
            ${sum}=    Vyos    ${rr}    show bgp ${afi} vpn summary
            FOR    ${pe}    IN    @{PES}
                ${line}=    Get Lines Containing String    ${sum}    ${LOOPBACK}[${pe}]${SPACE}
                ${rcvd}=    Evaluate    int(r"""${line}""".split()[9])
                ${held}=    Evaluate    len([p for p in $d["paths"] if p["afi"] == "${afi}" and p["peer_name"] == "${pe}" and p["attrs"].get("reflector") == "${rr}"])
                Should Be Equal As Integers    ${held}    ${rcvd}    msg=${rr} received ${rcvd} ${afi} VPN routes from ${pe}, the pre-policy view holds ${held}
            END
        END
    END

The SID decoded from BMP is the one the originating PE installed
    [Documentation]    lgd decodes the Prefix-SID itself (RFC 9252) and puts the transposed function bits back from the label
    ...                field: the result must be an End.DT46 the originating PE really has.
    Skip If    $LG_FEED != "bmp"    the SID is decoded by FRR on the collector with LG_FEED=session
    ${t}=    Set Variable    ${TENANTS}[0]
    FOR    ${dc}    IN    @{SITES}[${t}]
        ${site}=    Set Variable    ${SITES}[${t}][${dc}]
        ${core}=    Lg    /api/prefixes    source=collector    vrf=${t}    prefix=${site}[lan]    best_only=1
        ${a}=    Set Variable    ${core}[paths][0][attrs]
        Should Be Equal    ${core}[paths][0][via]    bmp-loc-rib
        Should Be True    $a.get("transposed_sid")    msg=${site}[lan]: no transposed SID in ${a}
        ${local}=    Shell    ${site}[pe]    sudo ip -c=never -6 route show ${a}[transposed_sid]
        Should Contain    ${local}    End.DT46    msg=${site}[pe] has no End.DT46 at ${a}[transposed_sid] (${site}[lan]): ${local}
        Should Contain    ${local}    vrftable ${t}    msg=${a}[transposed_sid] on ${site}[pe] does not decapsulate into ${t}
    END

A commit on a reflector does not cost the looking glass its BMP feed
    [Documentation]    VyOS re-renders bgpd on every commit and drops the VPN monitors it cannot express; the commit hook
    ...                (tools/bmp_hook.py) puts them back and makes the reflector replay, and the core's table is whole again.
    Skip If    $LG_FEED != "bmp"    no BMP with LG_FEED=session
    ${rr}=    Set Variable    ${RRS}[-1]
    ${before}=    Lg    /api/prefixes    source=collector    peer=${rr}    limit=2000
    TRY
        Configure    ${rr}    set protocols bgp neighbor ${LOOPBACK}[${PES}[0]] description '${PES}[0] (looking-glass test)'
        Wait Until Keyword Succeeds    60 s    5 s    BMP Monitors Should Be Back    ${rr}
        Wait Until Keyword Succeeds    90 s    5 s    Feed Should Be Synced And Whole    ${rr}    ${before}[total]
    FINALLY
        Configure    ${rr}    set protocols bgp neighbor ${LOOPBACK}[${PES}[0]] description '${PES}[0]'
    END

The page itself loads and is the looking glass
    ${html}=    Http Get    ${LG_URL}/
    Should Contain    ${html}    BGP Looking Glass
    Should Contain    ${html}    /api/prefixes
