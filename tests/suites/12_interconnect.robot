*** Settings ***
Documentation     The IPsec lab attached to the SRv6 core: every C8000v headend of cat8000v-ipsec is a CE of tenant-a on its
...               data centre's PE (headend Gi3 <-> PE eth5, 172.19.n.0/30, eBGP). The headends export their site LAN and
...               the branch LANs they learn over IPsec; the core carries them as VPNv4 routes with SRv6 SIDs to every PE;
...               data-centre hosts and branches reach each other through SRv6 -> headend -> IPsec; tenant-b stays isolated.
...               Needs the IPsec lab up (all headends and spokes); the suite is skipped when lab.conf has no EXT_NODES.
Resource          ../resources/common.resource
Suite Setup       Require External Lab
Suite Teardown    Suite Teardown Close Connections

*** Variables ***
${DC_HOST}        dc1-h1
${DC_HOST_B}      dc1-h2
${FAR_LAN}        192.168.17.0/24      # spoke5: reachable only through central-headend or west-headend, never east

*** Keywords ***
Require External Lab
    Skip If    not $EXT_CES    no external CEs in lab.conf (EXT_NODES)
    FOR    ${ce}    IN    @{EXT_CES}
        Tcp Port Should Be Open    ${EXT_SITES}[${ce}][mgmt_ip]    22
    END

*** Test Cases ***
Every PE has an Established eBGP session with its headend in tenant-a with the headend's AS
    FOR    ${ce}    IN    @{EXT_CES}
        ${s}=    Set Variable    ${EXT_SITES}[${ce}]
        ${sum}=    Run Vyos Command    ${MGMT}[${s}[pe]]    show ip bgp vrf ${s}[tenant] summary
        ${line}=    Get Lines Containing String    ${sum}    ${s}[ce_wan_ip]
        Should Match Regexp    ${line}    ^${s}[ce_wan_ip]\\s+4\\s+${s}[asn]\\s+.*\\s\\d+\\s+\\d+\\s+${ce}    msg=${s}[pe]: session to ${ce} (${s}[ce_wan_ip], AS ${s}[asn]) not Established
    END

The headend LANs and every branch LAN are tenant-a VPN routes on every PE, with an SRv6 SID from the attaching PE's locator
    FOR    ${pe}    IN    @{PES}
        ${rt}=    Vyos Shell    ${MGMT}[${pe}]    ip route show vrf tenant-a
        FOR    ${lan}    IN    @{EXT_LANS}
            Should Contain    ${rt}    ${lan}    msg=${pe}: ${lan} missing from VRF tenant-a
        END
    END
    FOR    ${ce}    IN    @{EXT_CES}
        ${s}=    Set Variable    ${EXT_SITES}[${ce}]
        ${lan}=    Set Variable    ${EXT_LAB_ROUTERS}[${ce}][lan]
        FOR    ${pe}    IN    @{PES}
            IF    '${pe}' == '${s}[pe]'    CONTINUE
            ${rt}=    Vyos Shell    ${MGMT}[${pe}]    ip route show vrf tenant-a ${lan}
            Should Match Regexp    ${rt}    encap seg6 mode encap segs 1 \\[ (\\S+) \\]    msg=${pe}: ${lan} (${ce}) is not an SRv6 route
            ${sid}=    Get Regexp Matches    ${rt}    segs 1 \\[ (\\S+) \\]    1
            Should Be True    $sid and __import__("ipaddress").ip_address($sid[0]) in __import__("ipaddress").ip_network($LOCATOR[$s["pe"]])    msg=${pe}: SID ${sid} for ${lan} is not inside ${s}[pe]'s locator
        END
    END

The route reflectors carry the external LANs under the attaching PE's tenant-a route distinguisher
    FOR    ${rr}    IN    @{RRS}
        ${vpn}=    Run Vyos Command    ${MGMT}[${rr}]    show bgp ipv4 vpn
        FOR    ${ce}    IN    @{EXT_CES}
            ${s}=    Set Variable    ${EXT_SITES}[${ce}]
            ${rd}=    Set Variable    ${CORE_AS}:${{ $VRF_TABLE['tenant-a'] + $NODES[$s['pe']]['idx'] }}
            ${block}=    Evaluate    $vpn.split("Route Distinguisher: " + $rd, 1)[1].split("Route Distinguisher:", 1)[0] if ("Route Distinguisher: " + $rd) in $vpn else ""
            Should Contain    ${block}    ${EXT_LAB_ROUTERS}[${ce}][lan]    msg=${rr}: ${ce}'s LAN not under RD ${rd}
        END
    END

The external LANs are not in tenant-b anywhere
    FOR    ${pe}    IN    @{PES}
        ${rt}=    Vyos Shell    ${MGMT}[${pe}]    ip route show vrf tenant-b
        FOR    ${lan}    IN    @{EXT_LANS}
            Should Not Contain    ${rt}    ${lan}    msg=${pe}: ${lan} leaked into tenant-b
        END
    END

Every headend learns every data-centre LAN of tenant-a through the core with the core's AS in the path
    FOR    ${ce}    IN    @{EXT_CES}
        ${s}=    Set Variable    ${EXT_SITES}[${ce}]
        FOR    ${dc}    IN    @{DCS}
            ${lan}=    Set Variable    ${DCS}[${dc}][lan]
            ${rt}=    Run Ios Command    ${s}[mgmt_ip]    show ip route ${{ $lan.split('/')[0] }}
            Should Contain    ${rt}    from ${s}[pe_wan_ip]    msg=${ce}: ${lan} is not routed via ${s}[pe] (${s}[pe_wan_ip])
            ${bgp}=    Run Ios Command    ${s}[mgmt_ip]    show ip bgp ${lan}
            Should Match Regexp    ${bgp}    (?m)^\\s+${CORE_AS} \\d+\\s*\\n\\s+${s}[pe_wan_ip] from    msg=${ce}: best path for ${lan} is not AS ${CORE_AS} via ${s}[pe_wan_ip]
        END
    END

A data-centre host reaches every headend and branch LAN through the core and the IPsec tunnels
    FOR    ${lan}    IN    @{EXT_LANS}
        ${out}=    Host Command    ${MGMT}[${DC_HOST}]    ping -c 3 -W 3 ${EXT_LAN_IP}[${lan}]
        ${lost}=    Ping Loss    ${out}
        Should Be True    ${lost} < 3    msg=${DC_HOST} -> ${EXT_LAN_IP}[${lan}] (${lan}): all pings lost
    END

The path to a far branch goes data-centre -> PE -> core -> headend -> IPsec tunnel -> branch
    ${tr}=    Host Command    ${MGMT}[${DC_HOST}]    traceroute -n -w 2 -q 1 -m 8 ${EXT_LAN_IP}[${FAR_LAN}]
    ${hops}=    Get Regexp Matches    ${tr}    (?m)^\\s*\\d+\\s+(\\d+\\.\\d+\\.\\d+\\.\\d+)    1
    ${headend_hops}=    Evaluate    [h for h in $hops if h.startswith("172.19.")]
    Length Should Be    ${headend_hops}    1    msg=expected exactly one headend hop (172.19.x.1) in ${tr}
    Should Not Be Equal    ${headend_hops}[0]    ${EXT_SITES}[east-headend][ce_wan_ip]    msg=${FAR_LAN} has no tunnel to east-headend, yet the path used it
    ${tunnel_hops}=    Evaluate    [h for h in $hops if h.startswith("172.17.")]
    Should Be True    len($tunnel_hops) >= 1    msg=expected the IPsec tunnel hop (172.17.x.2, the branch's tunnel end) in ${tr}
    Should Be True    $hops[-1] == $EXT_LAN_IP[$FAR_LAN] or $hops[-1] in $tunnel_hops    msg=the trace did not end at the branch: ${tr}

A branch reaches every data-centre host of tenant-a
    ${spoke}=    Evaluate    next(r for r in sorted($EXT_LAB_ROUTERS) if r.startswith("spoke"))
    ${spoke_mgmt}=    Set Variable    ${EXT_LAB_ROUTERS}[${spoke}][mgmt_ip]
    FOR    ${dc}    IN    @{DCS}
        ${out}=    Run Ios Command    ${spoke_mgmt}    ping ${DCS}[${dc}][host_ip] source Loopback10 repeat 5 timeout 2
        Should Match Regexp    ${out}    Success rate is (?:[2-9]\\d|100) percent    msg=${spoke} -> ${DCS}[${dc}][host_ip] (${dc}): ${out.strip().splitlines()[-1]}
    END

A tenant-b host cannot reach a branch
    ${out}=    Host Command    ${MGMT}[${DC_HOST_B}]    ping -c 2 -W 2 ${EXT_LAN_IP}[${FAR_LAN}]; true
    ${lost}=    Ping Loss    ${out}
    Should Be Equal As Integers    ${lost}    2    msg=tenant-b host reached ${FAR_LAN}

Traffic to a branch crosses the core SRv6-encapsulated towards the attaching PE
    ${s}=    Set Variable    ${EXT_SITES}[central-headend]
    ${h}=    Start Background    ${MGMT}[p2]    sudo timeout 20 tcpdump -ni any -c 3 'ip6 and dst net ${LOCATOR}[${s}[pe]]' 2>/dev/null
    Sleep    2s
    Host Command    ${MGMT}[${DC_HOST}]    ping -c 5 -i 0.3 ${EXT_LAN_IP}[192.168.14.0/24] >/dev/null; true
    ${cap}=    Finish Background    ${h}
    Should Match Regexp    ${cap}    IP6 \\S+ > ${{ $LOCATOR[$s['pe']].split('::')[0] }}:    msg=no SRv6-encapsulated packet for ${s}[pe]'s locator seen on p2:\n${cap}
