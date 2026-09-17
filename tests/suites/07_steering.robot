*** Settings ***
Documentation     Explicit-path SRv6 steering (traffic engineering): a tenant prefix on pe1 is pinned to the path
...               p1 -> p3 -> pe3 — the long way round the triangle — while the IGP shortest path stays p2. With uSID locators
...               the whole path is ONE segment (the carrier <block>:<uN p1>:<uN p3>:<uN pe3>:<uDT4>::) and every hop shifts the
...               destination address; with uncompressed locators it is a three-segment SRH. Captures prove the packets carry
...               the path and transit p1/p3, that p2 never sees them, and that the return traffic keeps the shortest path.
Resource          ../resources/common.resource
Suite Setup       Set Suite Variables
Suite Teardown    Remove The Policy And Close Connections

*** Variables ***
${SRC_PE}         pe1
${TENANT}         tenant-b
${VIA1}           p1
${VIA2}           p3

*** Test Cases ***
The policy installs an SRv6 encapsulation route in the tenant VRF: one compressed uSID carrier, or three segments
    ${out}=    Steer    add    ${SRC_PE}    ${TENANT}    ${DST}[lan]    ${VIA1}    ${VIA2}
    ${sid}=    Steer    sid    ${DST}[pe]    ${TENANT}
    ${sid}=    Strip String    ${sid}
    Set Suite Variable    ${SID}    ${sid}
    ${rt}=    Shell    ${SRC_PE}    sudo ip -c=never route show vrf ${TENANT} ${DST}[lan]
    IF    $USID
        ${hx}=    Evaluate    __import__("ipaddress").IPv6Address($SID).exploded.split(":")
        ${carrier}=    Evaluate    str(__import__("ipaddress").IPv6Address(":".join($hx[:2] + [$LOC1.split(":")[2], $LOC2.split(":")[2]] + $hx[2:4] + ["0", "0"])))
        Set Suite Variable    ${CARRIER}    ${carrier}
        Should Match Regexp    ${rt}    encap seg6 mode encap segs 1 \\[ ${CARRIER} \\] dev eth\\d+ proto static    msg=${SRC_PE}: expected one compressed segment ${CARRIER} for ${DST}[lan]
        Should Contain    ${out}    one compressed segment
    ELSE
        Should Match Regexp    ${rt}    encap seg6 mode encap segs 3 \\[ ${LOC1} ${LOC2} ${SID} \\] dev eth\\d+ proto static    msg=${SRC_PE}: no 3-segment static SRv6 route for ${DST}[lan]
    END

Steered traffic transits p1 and p3 with the segment list in its SRH, and p2 never sees it
    ${if_p1_to_p3}=    Evaluate    [i for i, peer in $ISIS_NEIGHBORS[$VIA1].items() if peer == $VIA2][0]
    ${if_p3_to_pe3}=    Evaluate    [i for i, peer in $ISIS_NEIGHBORS[$VIA2].items() if peer == $DST["pe"]][0]
    ${if_p2_to_pe3}=    Evaluate    [i for i, peer in $ISIS_NEIGHBORS["p2"].items() if peer == $DST["pe"]][0]
    ${loc2net}=    Set Variable    ${LOCATOR}[${VIA2}]
    ${cap1}=    Start Background    ${MGMT}[${VIA1}]    sudo timeout 20 tcpdump -c 3 -nni ${if_p1_to_p3} 'ip6 and dst net ${loc2net}'
    ${cap2}=    Start Background    ${MGMT}[${VIA2}]    sudo timeout 20 tcpdump -c 3 -nni ${if_p3_to_pe3} 'ip6 and dst net ${LOCATOR}[${DST}[pe]]'
    ${cap3}=    Start Background    ${MGMT}[p2]    sudo timeout 12 tcpdump -c 1 -nni ${if_p2_to_pe3} 'ip6 and dst net ${LOCATOR}[${DST}[pe]]'
    ${ping}=    Host    ${SRC}[host]    ping -c 10 -i 0.3 -W 2 ${DST}[host_ip]
    Should Contain    ${ping}    0% packet loss    msg=${SRC}[host] -> ${DST}[host] fails with the policy in place
    ${p1}=    Finish Background    ${cap1}
    ${p3}=    Finish Background    ${cap2}
    IF    $USID
        ${after_p1}=    Evaluate    str(__import__("ipaddress").IPv6Address(":".join(__import__("ipaddress").IPv6Address($CARRIER).exploded.split(":")[:2] + __import__("ipaddress").IPv6Address($CARRIER).exploded.split(":")[3:] + ["0"])))
        Should Match Regexp    ${p1}    IP6 ${LOOPBACK}[${SRC_PE}] > ${after_p1}: RT6 \\(len=2, type=4, segleft=0, last-entry=0.*\\[0\\]${CARRIER}\\) IP ${SRC}[host_ip] > ${DST}[host_ip]    msg=${VIA1}: after p1's uN the destination should be ${after_p1} (p1's micro-SID consumed) with the carrier ${CARRIER} in a one-segment SRH
        Should Match Regexp    ${p3}    IP6 ${LOOPBACK}[${SRC_PE}] > ${SID}: RT6 \\(len=2, type=4, segleft=0    msg=${VIA2}: after p3's uN the destination should be ${DST}[pe]'s uDT4 ${SID}
    ELSE
        Should Match Regexp    ${p1}    IP6 ${LOOPBACK}[${SRC_PE}] > ${LOC2}: RT6 \\(len=6, type=4, segleft=1, last-entry=2.*\\[0\\]${SID}, \\[1\\]${LOC2}, \\[2\\]${LOC1}\\) IP ${SRC}[host_ip] > ${DST}[host_ip]    msg=${VIA1}: no packets with the [p1, p3, pe3-DT4] SRH towards ${VIA2}
        Should Match Regexp    ${p3}    IP6 ${LOOPBACK}[${SRC_PE}] > ${SID}: RT6 \\(len=6, type=4, segleft=0    msg=${VIA2}: the last segment (${SID}) is not being delivered to ${DST}[pe]
    END
    ${p2}=    Finish Background    ${cap3}
    Should Contain    ${p2}    0 packets captured    msg=p2 still carries the steered flow towards ${DST}[pe]

The return traffic still follows the IGP shortest path through p2
    ${if_p2_to_pe1}=    Evaluate    [i for i, peer in $ISIS_NEIGHBORS["p2"].items() if peer == $SRC_PE][0]
    ${cap}=    Start Background    ${MGMT}[p2]    sudo timeout 15 tcpdump -c 3 -nni ${if_p2_to_pe1} 'ip6 and src host ${LOOPBACK}[${DST}[pe]] and dst net ${LOCATOR}[${SRC_PE}]'
    Host    ${SRC}[host]    ping -c 6 -i 0.3 -W 2 ${DST}[host_ip]
    ${out}=    Finish Background    ${cap}
    Should Contain    ${out}    3 packets captured    msg=p2: no replies from ${DST}[pe] towards ${SRC_PE}'s locator — the return path is not the shortest path

With uSID the same path as an uncompressed list is three segments — and it still works
    IF    not $USID    Skip    uncompressed locators: the only form there is
    Steer    add    ${SRC_PE}    ${TENANT}    ${DST}[lan]    ${VIA1}    ${VIA2}    --uncompressed
    ${rt}=    Shell    ${SRC_PE}    sudo ip -c=never route show vrf ${TENANT} ${DST}[lan]
    Should Match Regexp    ${rt}    encap seg6 mode encap segs 3 \\[ ${LOC1} ${LOC2} ${SID} \\]    msg=${SRC_PE}: expected the three-segment list
    ${ping}=    Host    ${SRC}[host]    ping -c 5 -i 0.3 -W 2 ${DST}[host_ip]
    Should Contain    ${ping}    0% packet loss    msg=uncompressed three-segment policy broke reachability
    Steer    add    ${SRC_PE}    ${TENANT}    ${DST}[lan]    ${VIA1}    ${VIA2}

Removing the policy returns the prefix to the BGP route
    Steer    del    ${SRC_PE}    ${TENANT}    ${DST}[lan]
    ${rt}=    Shell    ${SRC_PE}    sudo ip -c=never route show vrf ${TENANT} ${DST}[lan]
    Should Contain    ${rt}    proto bgp
    Should Not Contain    ${rt}    proto static
    ${show}=    Steer    show    ${SRC_PE}
    Should Contain    ${show}    no steering policies

*** Keywords ***
Set Suite Variables
    Set Suite Variable    ${SRC}    ${SITES}[${TENANT}][dc1]
    Set Suite Variable    ${DST}    ${SITES}[${TENANT}][dc3]
    Set Suite Variable    ${LOC1}    ${{ $LOCATOR[$VIA1].split('/')[0] }}
    Set Suite Variable    ${LOC2}    ${{ $LOCATOR[$VIA2].split('/')[0] }}

Remove The Policy And Close Connections
    Run Keyword And Ignore Error    Steer    del    ${SRC_PE}    ${TENANT}    ${DST}[lan]
    Close All Connections
