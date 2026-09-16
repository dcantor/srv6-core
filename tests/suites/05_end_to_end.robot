*** Settings ***
Documentation     The point of it all: every host reaches every other host across the SRv6 core, the traffic really
...               crosses the P routers as SRv6-encapsulated IPv6, and the return path works for forwarded packets.
Resource          ../resources/common.resource
Suite Teardown    Suite Teardown Close Connections

*** Variables ***
${TRANSIT_P}      p2       # every west<->east shortest path crosses p2 (pe1/pe2 hang off p1+p2, pe3/pe4 off p2+p3)

*** Test Cases ***
Every host pings every other host across the core
    FOR    ${src}    IN    @{HOSTS}
        FOR    ${dst}    IN    @{HOSTS}
            IF    '${src}' != '${dst}'
                ${out}=    Host    ${src}    ping -c 3 -W 2 ${HOST_IP}[${dst}]
                Should Contain    ${out}    0% packet loss    msg=${src} -> ${dst} (${HOST_IP}[${dst}]) failed
            END
        END
    END

Host traffic between the west and east data centres crosses the transit P router as SRv6-encapsulated IPv6
    [Documentation]    dc1 -> dc3: pe1 encapsulates towards pe3's locator, the packet transits p2 (its interface to pe3),
    ...    and the reply comes back the same way towards pe1's locator.
    ${west}=    Set Variable    ${DCS}[dc1]
    ${east}=    Set Variable    ${DCS}[dc3]
    ${if_to_east}=    Evaluate    [i for i, peer in $ISIS_NEIGHBORS[$TRANSIT_P].items() if peer == $east["pe"]][0]
    ${if_to_west}=    Evaluate    [i for i, peer in $ISIS_NEIGHBORS[$TRANSIT_P].items() if peer == $west["pe"]][0]
    ${tx_before}=    Shell    ${TRANSIT_P}    cat /sys/class/net/${if_to_east}/statistics/tx_bytes
    ${cap}=    Start Background    ${MGMT}[${TRANSIT_P}]    sudo timeout 20 tcpdump -c 4 -nni ${if_to_east} 'ip6 and dst net ${LOCATOR}[${east}[pe]]'
    ${cap_back}=    Start Background    ${MGMT}[${TRANSIT_P}]    sudo timeout 20 tcpdump -c 4 -nni ${if_to_west} 'ip6 and dst net ${LOCATOR}[${west}[pe]]'
    ${ping}=    Host    ${west}[host]    ping -c 20 -i 0.2 -s 1000 -W 2 ${east}[host_ip]
    Should Contain    ${ping}    0% packet loss
    ${tx_after}=    Shell    ${TRANSIT_P}    cat /sys/class/net/${if_to_east}/statistics/tx_bytes
    ${delta}=    Evaluate    int($tx_after) - int($tx_before)
    Should Be True    ${delta} >= 20 * 1028 + 20 * 40    msg=${TRANSIT_P} ${if_to_east} carried only ${delta} bytes; the pings (20 x 1028 B + encapsulation) did not transit it
    ${fwd}=    Finish Background    ${cap}
    Should Match Regexp    ${fwd}    IP6 ${LOOPBACK}[${west}[pe]] > ${{ $LOCATOR[$east['pe']].split('/')[0].rstrip(':') }}    msg=no SRv6 packets from ${west}[pe] towards ${east}[pe]'s locator on ${TRANSIT_P}
    Should Contain    ${fwd}    4 packets captured
    ${back}=    Finish Background    ${cap_back}
    Should Match Regexp    ${back}    IP6 ${LOOPBACK}[${east}[pe]] > ${{ $LOCATOR[$west['pe']].split('/')[0].rstrip(':') }}    msg=no SRv6 replies from ${east}[pe] towards ${west}[pe]'s locator on ${TRANSIT_P}

The tenant traffic is invisible to the P routers as IPv4: P routers carry no VRF and no IPv4 tenant routes
    FOR    ${p}    IN    @{PS}
        ${vrfs}=    Vyos    ${p}    show configuration commands | match 'vrf name'
        Should Be Empty    ${vrfs.strip()}    msg=${p} has a VRF configured
        ${v4}=    Vyos    ${p}    show ip route
        Should Not Contain    ${v4}    172.20.    msg=${p} knows tenant prefixes
    END
