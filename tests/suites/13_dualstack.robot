*** Settings ***
Documentation     Dual-stack tenants over the same SRv6 core: every tenant site also has an IPv6 attachment circuit (fd00:16/18:n::/64)
...               and LAN (fd00:20/21:n::/64), an IPv6 eBGP session per VRF, VPNv6 to both reflectors, and ONE End.DT46 SID per VRF
...               that decapsulates both families. IPv6 hosts reach every host of their tenant and none of the other's; the packet on
...               the P router is IPv6 inside IPv6 to the same SID the IPv4 traffic uses.
Resource          ../resources/common.resource
Suite Teardown    Suite Teardown Close Connections

*** Test Cases ***
Every PE has an Established IPv6 eBGP session with its CE in every tenant VRF, and the CE announces its IPv6 LAN
    FOR    ${t}    IN    @{TENANTS}
        FOR    ${dc}    IN    @{SITES}[${t}]
            ${s}=    Set Variable    ${SITES}[${t}][${dc}]
            ${sum}=    Vyos    ${s}[pe]    show bgp vrf ${t} ipv6 summary
            Should Match Regexp    ${sum}    (?m)^${s}[ce_wan_ip6]\\s+4\\s+${NODES}[${s}[ce]][asn]\\s+.*\\s\\d+\\s+\\d+\\s+${s}[ce]    msg=${s}[pe]: IPv6 session to ${s}[ce] (${s}[ce_wan_ip6]) in ${t} not Established
            ${rib}=    Vyos    ${s}[pe]    show bgp vrf ${t} ipv6 ${s}[lan6]
            Should Contain    ${rib}    ${{ str($NODES[$s["ce"]]["asn"]) }}    msg=${s}[pe]: ${s}[lan6] not learned from ${s}[ce]
        END
    END

The reflectors carry every IPv6 LAN under the attaching PE's RD, and every PE holds every remote IPv6 LAN once per reflector
    FOR    ${rr}    IN    @{RRS}
        ${sum}=    Vyos    ${rr}    show bgp ipv6 vpn summary
        FOR    ${pe}    IN    @{PES}
            Should Match Regexp    ${sum}    (?m)^${LOOPBACK}[${pe}]\\s+4\\s+${CORE_AS}\\s+.*\\s\\d+\\s+\\d+\\s+${pe}    msg=${rr}: VPNv6 client ${pe} not Established
        END
        ${vpn}=    Vyos    ${rr}    show bgp ipv6 vpn
        FOR    ${t}    IN    @{TENANTS}
            FOR    ${dc}    IN    @{SITES}[${t}]
                ${s}=    Set Variable    ${SITES}[${t}][${dc}]
                ${blk}=    Evaluate    $vpn.split("Route Distinguisher: " + $s["rd"], 1)[1].split("Route Distinguisher:", 1)[0] if ("Route Distinguisher: " + $s["rd"]) in $vpn else ""
                Should Contain    ${blk}    ${s}[lan6]    msg=${rr}: ${s}[lan6] not under RD ${s}[rd]
            END
        END
    END
    FOR    ${pe}    IN    @{PES}
        FOR    ${t}    IN    @{TENANTS}
            FOR    ${dc}    IN    @{SITES}[${t}]
                ${d}=    Set Variable    ${SITES}[${t}][${dc}]
                IF    '${d}[pe]' == '${pe}'    CONTINUE
                ${detail}=    Vyos    ${pe}    show bgp ipv6 vpn ${d}[lan6]
                ${from}=    Regex Findall    ${detail}    (?m)from (fd00:a::\\d+)
                ${want}=    Evaluate    sorted([$LOOPBACK[r] for r in $RRS])
                Lists Should Be Equal    ${{ sorted(set($from)) }}    ${want}    msg=${pe}: ${d}[lan6] not learned from every reflector
                ${sid}=    Regex Findall    ${detail}    (?m)^\\s*Remote SID: ([0-9a-f:]+), sid structure=
                Ip In Network    ${sid}[0]    ${LOCATOR}[${d}[pe]]
            END
        END
    END

One End.DT46 SID per VRF serves both address families
    FOR    ${pe}    IN    @{PES}
        ${sids}=    Shell    ${pe}    sudo ip -6 route show | grep 'End.DT46'
        FOR    ${t}    IN    @{TENANTS}
            ${dt46}=    Regex Findall    ${sids}    (?m)^(\\S+)\\s.*action End\\.DT46 vrftable ${t}\\b
            Length Should Be    ${dt46}    1    msg=${pe}: expected one End.DT46 for ${t}
            FOR    ${dc}    IN    @{SITES}[${t}]
                ${d}=    Set Variable    ${SITES}[${t}][${dc}]
                IF    '${d}[pe]' != '${pe}'    CONTINUE
                FOR    ${other}    IN    @{PES}
                    IF    '${other}' == '${pe}'    CONTINUE
                    ${v4}=    Vyos    ${other}    show bgp ipv4 vpn ${d}[lan]
                    ${v6}=    Vyos    ${other}    show bgp ipv6 vpn ${d}[lan6]
                    ${s4}=    Regex Findall    ${v4}    (?m)Remote SID: ([0-9a-f:]+), sid structure=\\[\\d+ \\d+ \\d+ \\d+ \\d+ \\d+\\]
                    ${s6}=    Regex Findall    ${v6}    (?m)Remote SID: ([0-9a-f:]+), sid structure=\\[\\d+ \\d+ \\d+ \\d+ \\d+ \\d+\\]
                    ${l4}=    Regex Findall    ${v4}    (?m)Remote labels?: (\\d+)
                    ${l6}=    Regex Findall    ${v6}    (?m)Remote labels?: (\\d+)
                    Should Be Equal    ${s4}[0]    ${s6}[0]    msg=${other}: ${d}[lan] and ${d}[lan6] carry different SIDs
                    Should Be Equal    ${l4}[0]    ${l6}[0]    msg=${other}: the transposed function differs between the families
                    BREAK
                END
            END
        END
    END

Every PE installs an SRv6 encapsulation route for every remote IPv6 LAN of every tenant, in that tenant's VRF only
    FOR    ${pe}    IN    @{PES}
        FOR    ${t}    IN    @{TENANTS}
            ${rt}=    Shell    ${pe}    sudo ip -c=never -6 route show vrf ${t}
            FOR    ${other}    IN    @{TENANTS}
                FOR    ${dc}    IN    @{SITES}[${other}]
                    ${d}=    Set Variable    ${SITES}[${other}][${dc}]
                    IF    '${d}[pe]' == '${pe}'    CONTINUE
                    IF    '${other}' == '${t}'
                        ${sids}=    Regex Findall    ${rt}    (?s)^${d}[lan6].*?encap seg6 mode encap segs 1 \\[ ([0-9a-f:]+) \\]
                        Should Not Be Empty    ${sids}    msg=${pe}: ${d}[lan6] is not an SRv6 encap route in VRF ${t}
                        Ip In Network    ${sids}[0]    ${LOCATOR}[${d}[pe]]
                    ELSE
                        Should Not Contain    ${rt}    ${d}[lan6]    msg=${pe}: ${other}'s ${d}[lan6] leaked into VRF ${t}
                    END
                END
            END
        END
    END

Every IPv6 host reaches every IPv6 host of its tenant and none of the other tenant's
    FOR    ${t}    IN    @{TENANTS}
        FOR    ${dc}    IN    @{SITES}[${t}]
            ${src}=    Set Variable    ${SITES}[${t}][${dc}]
            FOR    ${other}    IN    @{TENANTS}
                FOR    ${dc2}    IN    @{SITES}[${other}]
                    ${dst}=    Set Variable    ${SITES}[${other}][${dc2}]
                    IF    '${dst}[host]' == '${src}[host]'    CONTINUE
                    ${out}=    Host Command    ${MGMT}[${src}[host]]    ping -6 -c 3 -W 2 ${dst}[host_ip6]; true
                    ${lost}=    Ping Loss    ${out}
                    IF    '${other}' == '${t}'
                        Should Be True    ${lost} < 3    msg=${src}[host] -> ${dst}[host] (${dst}[host_ip6]): IPv6 unreachable inside ${t}
                    ELSE
                        Should Be Equal As Integers    ${lost}    3    msg=${src}[host] reached ${dst}[host] (${dst}[host_ip6]) across tenants
                    END
                END
            END
        END
    END

IPv6 tenant traffic crosses the core as IPv6 inside IPv6 towards the same SID as IPv4
    ${src}=    Set Variable    ${SITES}[tenant-a][dc1]
    ${dst}=    Set Variable    ${SITES}[tenant-a][dc3]
    ${sid}=    Shell    ${dst}[pe]    sudo ip -6 route show | grep 'End.DT46 vrftable tenant-a' | cut -d' ' -f1
    ${h}=    Start Background    ${MGMT}[p2]    sudo timeout 20 tcpdump -ni any -c 2 -vv 'ip6 and dst host ${sid.strip()} and ip6 proto 43' 2>/dev/null
    Sleep    2s
    Host Command    ${MGMT}[${src}[host]]    ping -6 -c 5 -i 0.3 ${dst}[host_ip6] >/dev/null; true
    ${cap}=    Finish Background    ${h}
    Should Match Regexp    ${cap}    IP6 .*${LOOPBACK}[${src}[pe]] > ${sid.strip()}.*IP6 .*${src}[host_ip6] > ${dst}[host_ip6]    msg=no IPv6-in-IPv6 packet to ${sid.strip()} seen on p2:\n${cap}
