*** Settings ***
Documentation     BGP L3VPN over SRv6, per tenant: the route reflector has every PE as an established VPNv4 client and
...               every data-centre LAN of every tenant; every PE imports the other LANs of a tenant into that tenant's VRF
...               only, with an SRv6 SID from the right locator, and the kernel VRF carries them as seg6-encapsulated routes;
...               every CE learns the other LANs of each tenant in that tenant's own VRF, over that VRF's eBGP session.
Resource          ../resources/common.resource
Suite Teardown    Suite Teardown Close Connections

*** Test Cases ***
Every route reflector has every PE established in the VPNv4 address family with prefixes received
    FOR    ${rr}    IN    @{RRS}
        ${sum}=    Vyos    ${rr}    show bgp ipv4 vpn summary
        FOR    ${pe}    IN    @{PES}
            Should Match Regexp    ${sum}    (?m)^${LOOPBACK}[${pe}]\\s+4\\s+${CORE_AS}\\s+\\d+\\s+\\d+\\s+\\d+\\s+\\d+\\s+\\d+\\s+\\S+\\s+[1-9]\\d*\\s    msg=${rr}: ${pe} is not Established with prefixes in VPNv4
        END
    END
    FOR    ${pe}    IN    @{PES}
        ${sum}=    Vyos    ${pe}    show bgp ipv4 vpn summary
        FOR    ${rr}    IN    @{RRS}
            Should Match Regexp    ${sum}    (?m)^${LOOPBACK}[${rr}]\\s+4\\s+${CORE_AS}\\s.*\\s[1-9]\\d*\\s+\\d+\\s    msg=${pe}: session to reflector ${rr} not Established with prefixes
        END
    END

The route reflector holds every data-centre LAN of every tenant under its PE's route distinguisher with the PE as next hop
    ${vpn}=    Vyos    ${RR}    show bgp ipv4 vpn
    FOR    ${t}    IN    @{TENANTS}
        FOR    ${dc}    IN    @{SITES}[${t}]
            ${d}=    Set Variable    ${SITES}[${t}][${dc}]
            Should Match Regexp    ${vpn}    (?s)Route Distinguisher: ${d}[rd]\\n.*?\\*>i\\s*${d}[lan]\\s+${LOOPBACK}[${d}[pe]]    msg=${t} ${dc}: ${d}[lan] not at ${RR} under RD ${d}[rd] via ${d}[pe]
        END
    END

Every PE imports the other LANs of a tenant into that tenant's VRF with an SRv6 SID from the originating PE's locator
    FOR    ${pe}    IN    @{PES}
        FOR    ${t}    IN    @{TENANTS}
        ${rib}=    Vyos    ${pe}    show ip route vrf ${t}
        FOR    ${dc}    IN    @{SITES}[${t}]
            ${d}=    Set Variable    ${SITES}[${t}][${dc}]
            IF    '${d}[pe]' != '${pe}'
                ${detail}=    Shell    ${pe}    sudo vtysh -c 'show bgp vrf ${t} ipv4 unicast ${d}[lan]'
                Should Contain    ${detail}    Imported from ${d}[rd]:${d}[lan]    msg=${pe}: ${d}[lan] not imported from RD ${d}[rd]
                Should Match Regexp    ${detail}    (?m)^\\s*${LOOPBACK}[${d}[pe]] \\(metric    msg=${pe}: ${d}[lan] next hop is not ${d}[pe]
                ${sid}=    Regex Findall    ${detail}    (?m)^\\s*Remote SID: ([0-9a-f:]+), sid structure=\\[40 24 16
                Should Not Be Empty    ${sid}    msg=${pe}: no SRv6 SID with the lab's structure on ${d}[lan]
                Ip In Network    ${sid}[0]    ${LOCATOR}[${d}[pe]]
                Should Match Regexp    ${rib}    (?m)^B>\\s+${d}[lan] \\[200/0\\] via ${LOOPBACK}[${d}[pe]] \\(vrf default\\) \\(recursive\\), label \\d+, seg6 ([0-9a-f:]+)    msg=${pe}: ${d}[lan] is not a recursive SRv6 route via ${d}[pe] in the VRF RIB
            END
        END
        END
    END

The kernel VRF on every PE carries the remote LANs as SRv6-encapsulated routes, the local LAN via the CE, and nothing of the other tenant
    FOR    ${pe}    IN    @{PES}
        FOR    ${t}    IN    @{TENANTS}
        ${rt}=    Shell    ${pe}    sudo ip -c=never route show vrf ${t}
        FOR    ${other_t}    IN    @{TENANTS}
            IF    '${other_t}' != '${t}'
                FOR    ${dc}    IN    @{SITES}[${other_t}]
                    Should Not Contain    ${rt}    ${SITES}[${other_t}][${dc}][lan]    msg=${pe}: ${other_t} prefix ${SITES}[${other_t}][${dc}][lan] leaked into VRF ${t}
                END
            END
        END
        FOR    ${dc}    IN    @{SITES}[${t}]
            ${d}=    Set Variable    ${SITES}[${t}][${dc}]
            IF    '${d}[pe]' != '${pe}'
                ${sids}=    Regex Findall    ${rt}    (?s)^${d}[lan].*?encap seg6 mode encap segs 1 \\[ ([0-9a-f:]+) \\]
                Should Not Be Empty    ${sids}    msg=${pe}: ${d}[lan] is not an SRv6 encap route in VRF ${t}
                Ip In Network    ${sids}[0]    ${LOCATOR}[${d}[pe]]
            ELSE
                Should Match Regexp    ${rt}    (?m)^${d}[lan] .*via ${d}[ce_wan_ip]    msg=${pe}: local LAN ${d}[lan] should point at ${d}[ce]
            END
        END
        END
    END

Every CE learns the other three data-centre LANs of each tenant from its PE, in that tenant's own VRF
    FOR    ${ce}    IN    @{CES}
        ${dflt}=    Vyos    ${ce}    show ip route
        Should Not Match Regexp    ${dflt}    (?m)^B    msg=${ce}: the default VRF must carry no BGP routes (tenants live in their VRFs)
        ${bgp}=    Vyos    ${ce}    show ip bgp summary
        Should Contain    ${bgp}    No BGP neighbors found in VRF default    msg=${ce}: a BGP session in the default VRF
    END
    FOR    ${t}    IN    @{TENANTS}
        FOR    ${dc}    IN    @{SITES}[${t}]
            ${d}=    Set Variable    ${SITES}[${t}][${dc}]
            ${v}=    Set Variable    vrf ${t} ${SPACE}
            ${sum}=    Vyos    ${d}[ce]    show ip bgp ${v}summary
            Should Match Regexp    ${sum}    (?m)^${d}[pe_wan_ip]\\s+4\\s+${CORE_AS}\\s.*\\s[1-9]\\d*\\s    msg=${d}[ce]: eBGP to ${d}[pe] (${t}) not Established with prefixes
            ${rib}=    Vyos    ${d}[ce]    show ip route ${v}bgp
            FOR    ${other}    IN    @{SITES}[${t}]
                IF    '${other}' != '${dc}'
                    Should Match Regexp    ${rib}    (?m)^B>\\*\\s+${SITES}[${t}][${other}][lan] .*via ${d}[pe_wan_ip]    msg=${d}[ce]: no ${t} route to ${other} (${SITES}[${t}][${other}][lan])
                END
            END
        END
    END
