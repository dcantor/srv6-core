*** Settings ***
Documentation     Core link failure: the p2-pe3 link is cut silently (a firewall drop on p2's interface — carrier stays up,
...               exactly like a UDP-tunnel link that has gone dead), BFD detects it in under a second, pe3 reroutes every
...               tenant through p3, a continuous ping across the core loses at most a handful of packets, and everything
...               returns to the shortest path once the link is restored. The suite always restores the link in its teardown.
Resource          ../resources/common.resource
Suite Teardown    Restore The Link And Close Connections

*** Variables ***
${P}              p2
${PE}             pe3
@{CUT}            set firewall ipv6 input filter rule 10 action drop        set firewall ipv6 output filter rule 10 action drop
...               set firewall ipv6 forward filter rule 10 action drop      set firewall ipv6 forward filter rule 11 action drop

*** Test Cases ***
BFD runs on every core adjacency
    FOR    ${n}    IN    @{CORE}
        ${bfd}=    Vyos    ${n}    show bfd peers brief
        ${up}=    Regex Findall    ${bfd}    (?m)^\\d+\\s+fe80\\S+\\s+fe80\\S+\\s+up\\b
        ${n_exp}=    Get Length    ${ISIS_NEIGHBORS}[${n}]
        Length Should Be    ${up}    ${n_exp}    msg=${n}: expected ${n_exp} BFD sessions up
        ${nb}=    Vyos    ${n}    show isis neighbor detail
        ${active}=    Get Count    ${nb}    BFD is active, status Up
        Should Be Equal As Integers    ${active}    ${n_exp}    msg=${n}: every IS-IS adjacency must be BFD-protected
    END

A silent core link failure is detected by BFD and traffic reconverges in under two seconds
    ${if}=    Evaluate    [i for i, peer in $ISIS_NEIGHBORS[$P].items() if peer == $PE][0]
    ${alt}=    Evaluate    [i for i, peer in $ISIS_NEIGHBORS[$PE].items() if peer != $P][0]
    ${src}=    Set Variable    ${SITES}[tenant-a][dc1]
    ${dst}=    Set Variable    ${SITES}[tenant-a][dc3]
    ${rt}=    Shell    ${PE}    sudo ip -c=never route show vrf tenant-a ${src}[lan]
    Should Not Contain    ${rt}    dev ${alt}    msg=${PE}: ${src}[lan] already avoids ${P} before the failure
    ${ping}=    Start Background    ${MGMT}[${src}[host]]    ping -c 400 -i 0.2 -W 1 ${dst}[host_ip]    cirros    gocubsgo
    Configure    ${P}    set firewall ipv6 input filter rule 10 inbound-interface name ${if}    set firewall ipv6 output filter rule 10 outbound-interface name ${if}
    ...    set firewall ipv6 forward filter rule 10 inbound-interface name ${if}    set firewall ipv6 forward filter rule 11 outbound-interface name ${if}    @{CUT}
    Wait Until Keyword Succeeds    20s    2s    Route Should Use Interface    ${PE}    ${src}[lan]    ${alt}
    ${nb}=    Vyos    ${PE}    show isis neighbor detail
    Should Match Regexp    ${nb}    (?s)${P}.*?BFD is active, status Down    msg=${PE}: BFD to ${P} not reported Down
    FOR    ${t}    IN    @{TENANTS}
        FOR    ${dc}    IN    @{SITES}[${t}]
            ${d}=    Set Variable    ${SITES}[${t}][${dc}]
            IF    '${d}[pe]' != '${PE}'
                Route Should Use Interface    ${PE}    ${d}[lan]    ${alt}    ${t}
            END
        END
    END
    Configure    ${P}    delete firewall ipv6
    Wait Until Keyword Succeeds    60s    3s    Route Should Use Interface    ${PE}    ${src}[lan]    eth1
    ${out}=    Finish Background    ${ping}    120
    ${lost}=    Ping Loss    ${out}
    Should Be True    ${lost} <= 10    msg=${src}[host] -> ${dst}[host] lost ${lost} packets (0.2 s apart) across the failure and the repair — more than 2 s of outage
    Log    ${src}[host] -> ${dst}[host]: ${lost} packets lost across the link failure and its repair    console=True

After the repair every BFD session and IS-IS adjacency is back
    FOR    ${n}    IN    ${P}    ${PE}
        Wait Until Keyword Succeeds    30s    3s    All Sessions Up    ${n}
    END

*** Keywords ***
Route Should Use Interface
    [Arguments]    ${node}    ${prefix}    ${iface}    ${vrf}=tenant-a
    ${rt}=    Shell    ${node}    sudo ip -c=never route show vrf ${vrf} ${prefix}
    Should Match Regexp    ${rt}    dev ${iface}\\b    msg=${node}: ${prefix} (${vrf}) is not via ${iface}: ${rt.strip()}
    ${others}=    Regex Findall    ${rt}    dev (eth\\d+)
    FOR    ${o}    IN    @{others}
        Should Be Equal    ${o}    ${iface}    msg=${node}: ${prefix} (${vrf}) still has a next hop via ${o}
    END

All Sessions Up
    [Arguments]    ${n}
    ${bfd}=    Vyos    ${n}    show bfd peers brief
    ${up}=    Regex Findall    ${bfd}    (?m)^\\d+\\s+fe80\\S+\\s+fe80\\S+\\s+up\\b
    ${n_exp}=    Get Length    ${ISIS_NEIGHBORS}[${n}]
    Length Should Be    ${up}    ${n_exp}    msg=${n}: not every BFD session is up again
    ${nb}=    Vyos    ${n}    show isis neighbor
    ${adj}=    Regex Findall    ${nb}    (?m)^\\s*\\S+\\s+(eth\\d+)\\s+2\\s+Up
    Length Should Be    ${adj}    ${n_exp}

Restore The Link And Close Connections
    Run Keyword And Ignore Error    Configure    ${P}    delete firewall ipv6
    Close All Connections
