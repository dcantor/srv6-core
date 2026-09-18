*** Settings ***
Documentation     Route-reflector redundancy: every PE holds each VPN route from every reflector; shutting the sessions
...               of one reflector leaves every tenant working (routes and SIDs from the other), and the sessions come
...               back once the reflector is restored. The suite always restores the reflector in its teardown.
Resource          ../resources/common.resource
Suite Teardown    Restore The Reflector And Close Connections

*** Variables ***
${VICTIM}         ${RRS}[0]

*** Test Cases ***
Every PE holds every remote VPN route once per reflector
    ${n_rr}=    Get Length    ${RRS}
    FOR    ${pe}    IN    @{PES}
        FOR    ${t}    IN    @{TENANTS}
            FOR    ${dc}    IN    @{SITES}[${t}]
                ${d}=    Set Variable    ${SITES}[${t}][${dc}]
                IF    '${d}[pe]' != '${pe}'
                    ${r}=    Vyos    ${pe}    show bgp ipv4 vpn ${d}[lan]
                    ${from}=    Regex Findall    ${r}    (?m)^\\s+\\S+ \\(metric \\d+\\) from (fd00:a::\\d+)
                    ${n}=    Get Length    ${from}
                    Should Be Equal As Integers    ${n}    ${n_rr}    msg=${pe}: ${d}[lan] (${t}) should be learned from ${n_rr} reflectors, saw ${from}
                    FOR    ${rr}    IN    @{RRS}
                        List Should Contain Value    ${from}    ${LOOPBACK}[${rr}]    msg=${pe}: ${d}[lan] not learned from ${rr}
                    END
                END
            END
        END
    END

Losing a route reflector changes nothing for the tenants
    [Documentation]    Shut every client session on ${VICTIM} (peer-group shutdown), wait for the PEs to drop it, then
    ...    prove that every VRF still has every remote LAN (now with a single path, via the surviving reflector), that the
    ...    SRv6 encapsulation routes are still in the kernel, and that every host still reaches its tenant peers.
    ${ann}=    Grafana Annotate    srv6-core: reflector redundancy test — every client session on ${VICTIM} shut    rr-redundancy    ${VICTIM}
    Set Suite Variable    ${ann}
    Configure    ${VICTIM}    set protocols bgp peer-group RR-CLIENTS shutdown
    Wait Until Keyword Succeeds    90s    5s    Reflector Sessions Should Be Down On Every PE    ${VICTIM}
    ${survivors}=    Evaluate    [r for r in $RRS if r != $VICTIM]
    FOR    ${pe}    IN    @{PES}
        FOR    ${t}    IN    @{TENANTS}
            ${rt}=    Shell    ${pe}    sudo ip -c=never route show vrf ${t}
            FOR    ${dc}    IN    @{SITES}[${t}]
                ${d}=    Set Variable    ${SITES}[${t}][${dc}]
                IF    '${d}[pe]' != '${pe}'
                    Should Match Regexp    ${rt}    (?ms)^${d}[lan].*?encap seg6    msg=${pe}: ${d}[lan] (${t}) lost its SRv6 route without ${VICTIM}
                    ${r}=    Vyos    ${pe}    show bgp ipv4 vpn ${d}[lan]
                    ${from}=    Regex Findall    ${r}    (?m)^\\s+\\S+ \\(metric \\d+\\) from (fd00:a::\\d+)
                    Should Not Contain    ${from}    ${LOOPBACK}[${VICTIM}]    msg=${pe}: still holds ${d}[lan] from the shut reflector
                    List Should Contain Value    ${from}    ${LOOPBACK}[${survivors}[0]]    msg=${pe}: ${d}[lan] not learned from the surviving reflector
                END
            END
        END
    END
    FOR    ${src}    IN    @{HOSTS}
        FOR    ${dst}    IN    @{HOSTS}
            IF    '${src}' != '${dst}' and $HOST_TENANT[$src] == $HOST_TENANT[$dst]
                ${out}=    Host    ${src}    ping -c 2 -W 2 ${HOST_IP}[${dst}]
                Should Contain    ${out}    0% packet loss    msg=${src} -> ${dst} failed while ${VICTIM} was down
            END
        END
    END

The reflector's sessions come back after it is restored
    Configure    ${VICTIM}    delete protocols bgp peer-group RR-CLIENTS shutdown
    Grafana Annotation End    ${ann}    srv6-core: reflector redundancy test — ${VICTIM} shut and restored; tenants unaffected
    Wait Until Keyword Succeeds    120s    5s    Reflector Sessions Should Be Up On Every PE    ${VICTIM}

*** Keywords ***
Reflector Sessions Should Be Down On Every PE
    [Arguments]    ${rr}
    FOR    ${pe}    IN    @{PES}
        ${sum}=    Vyos    ${pe}    show bgp ipv4 vpn summary
        Should Match Regexp    ${sum}    (?m)^${LOOPBACK}[${rr}]\\s+4\\s+${CORE_AS}\\s.*\\s(Active|Idle|Connect|OpenSent|OpenConfirm)    msg=${pe}: session to ${rr} still Established
    END

Reflector Sessions Should Be Up On Every PE
    [Arguments]    ${rr}
    FOR    ${pe}    IN    @{PES}
        ${sum}=    Vyos    ${pe}    show bgp ipv4 vpn summary
        Should Match Regexp    ${sum}    (?m)^${LOOPBACK}[${rr}]\\s+4\\s+${CORE_AS}\\s.*\\s[1-9]\\d*\\s+\\d+\\s    msg=${pe}: session to ${rr} not Established with prefixes
    END

Restore The Reflector And Close Connections
    Run Keyword And Ignore Error    Configure    ${VICTIM}    delete protocols bgp peer-group RR-CLIENTS shutdown
    Close All Connections
