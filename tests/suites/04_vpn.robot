*** Settings ***
Documentation     BGP L3VPN over SRv6: the route reflector has every PE as an established VPNv4 client and every
...               data-centre LAN; every PE imports the other LANs with an SRv6 SID from the right locator and the
...               kernel VRF carries them as seg6-encapsulated routes; every CE learns the other LANs over eBGP.
Resource          ../resources/common.resource
Suite Teardown    Suite Teardown Close Connections

*** Test Cases ***
The route reflector has every PE established in the VPNv4 address family with prefixes received
    ${sum}=    Vyos    ${RR}    show bgp ipv4 vpn summary
    FOR    ${pe}    IN    @{PES}
        Should Match Regexp    ${sum}    (?m)^${LOOPBACK}[${pe}]\\s+4\\s+${CORE_AS}\\s+\\d+\\s+\\d+\\s+\\d+\\s+\\d+\\s+\\d+\\s+\\S+\\s+[1-9]\\d*\\s    msg=${RR}: ${pe} is not Established with prefixes in VPNv4
    END

The route reflector holds every data-centre LAN under its PE's route distinguisher with the PE as next hop
    ${vpn}=    Vyos    ${RR}    show bgp ipv4 vpn
    FOR    ${dc}    IN    @{DCS}
        ${d}=    Set Variable    ${DCS}[${dc}]
        Should Match Regexp    ${vpn}    (?s)Route Distinguisher: ${d}[rd]\\n.*?\\*>i\\s*${d}[lan]\\s+${LOOPBACK}[${d}[pe]]    msg=${dc}: ${d}[lan] not at ${RR} under RD ${d}[rd] via ${d}[pe]
    END

Every PE imports the other LANs with an SRv6 SID from the originating PE's locator
    FOR    ${pe}    IN    @{PES}
        ${rib}=    Vyos    ${pe}    show ip route vrf ${VRF}
        FOR    ${dc}    IN    @{DCS}
            ${d}=    Set Variable    ${DCS}[${dc}]
            IF    '${d}[pe]' != '${pe}'
                ${detail}=    Shell    ${pe}    sudo vtysh -c 'show bgp vrf ${VRF} ipv4 unicast ${d}[lan]'
                Should Contain    ${detail}    Imported from ${d}[rd]:${d}[lan]    msg=${pe}: ${d}[lan] not imported from RD ${d}[rd]
                Should Match Regexp    ${detail}    (?m)^\\s*${LOOPBACK}[${d}[pe]] \\(metric    msg=${pe}: ${d}[lan] next hop is not ${d}[pe]
                ${sid}=    Regex Findall    ${detail}    (?m)^\\s*Remote SID: ([0-9a-f:]+), sid structure=\\[40 24 16
                Should Not Be Empty    ${sid}    msg=${pe}: no SRv6 SID with the lab's structure on ${d}[lan]
                Ip In Network    ${sid}[0]    ${LOCATOR}[${d}[pe]]
                Should Match Regexp    ${rib}    (?m)^B>\\s+${d}[lan] \\[200/0\\] via ${LOOPBACK}[${d}[pe]] \\(vrf default\\) \\(recursive\\), label \\d+, seg6 ([0-9a-f:]+)    msg=${pe}: ${d}[lan] is not a recursive SRv6 route via ${d}[pe] in the VRF RIB
            END
        END
    END

The kernel VRF on every PE carries the remote LANs as SRv6-encapsulated routes and the local LAN via the CE
    FOR    ${pe}    IN    @{PES}
        ${rt}=    Shell    ${pe}    sudo ip -c=never route show vrf ${VRF}
        FOR    ${dc}    IN    @{DCS}
            ${d}=    Set Variable    ${DCS}[${dc}]
            IF    '${d}[pe]' != '${pe}'
                ${sids}=    Regex Findall    ${rt}    (?s)^${d}[lan].*?encap seg6 mode encap segs 1 \\[ ([0-9a-f:]+) \\]
                Should Not Be Empty    ${sids}    msg=${pe}: ${d}[lan] is not an SRv6 encap route in VRF ${VRF}
                Ip In Network    ${sids}[0]    ${LOCATOR}[${d}[pe]]
            ELSE
                Should Match Regexp    ${rt}    (?m)^${d}[lan] .*via ${d}[ce_wan_ip]    msg=${pe}: local LAN ${d}[lan] should point at ${d}[ce]
            END
        END
    END

Every CE learns the other three data-centre LANs from its PE over eBGP
    FOR    ${dc}    IN    @{DCS}
        ${d}=    Set Variable    ${DCS}[${dc}]
        ${sum}=    Vyos    ${d}[ce]    show ip bgp summary
        Should Match Regexp    ${sum}    (?m)^${d}[pe_wan_ip]\\s+4\\s+${CORE_AS}\\s.*\\s[1-9]\\d*\\s    msg=${d}[ce]: eBGP to ${d}[pe] not Established with prefixes
        ${rib}=    Vyos    ${d}[ce]    show ip route bgp
        FOR    ${other}    IN    @{DCS}
            IF    '${other}' != '${dc}'
                Should Match Regexp    ${rib}    (?m)^B>\\*\\s+${DCS}[${other}][lan] .*via ${d}[pe_wan_ip]    msg=${d}[ce]: no BGP route to ${other} (${DCS}[${other}][lan])
            END
        END
    END
