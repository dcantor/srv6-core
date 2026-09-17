*** Settings ***
Documentation     Throughput across the SRv6 core, measured with iperf3 between the Alpine tenant hosts: the shortest path, a
...               steered path (one uSID carrier segment) and the same steered path as an uncompressed three-segment SRH.
...               The numbers are what a 1 vCPU VyOS software data plane over UDP-tunnelled links gives (~100-150 Mbit/s);
...               the test asserts a floor and that steering / uSID do not break or collapse throughput. Policies are removed.
Resource          ../resources/common.resource
Suite Teardown    Cleanup

*** Variables ***
${SRC}            dc1-h1
${DST}            dc3-h1
${SRC_B}          dc1-h2
${DST_B}          dc3-h2
${FLOOR_MBPS}     30

*** Test Cases ***
TCP throughput across the core on the shortest path is above the floor
    ${r}=    Iperf    ${SRC}    ${DST}    5
    Should Be True    ${r}[mbps_received] >= ${FLOOR_MBPS}    msg=${SRC} -> ${DST}: only ${r}[mbps_received] Mbit/s
    Log    ${SRC} -> ${DST} shortest path: ${r}[mbps_received] Mbit/s TCP (${r}[retransmits] retransmits)    console=True

UDP at a fixed rate crosses the core with negligible jitter
    ${r}=    Iperf    ${SRC}    ${DST}    5    udp=${True}    rate=20M
    Should Be True    ${r}[mbps] >= 18    msg=UDP rate ${r}[mbps] Mbit/s below the 20 Mbit/s offered
    Should Be True    ${r}[loss_pct] < 2    msg=${r}[loss_pct] % loss at 20 Mbit/s
    Should Be True    ${r}[jitter_ms] < 5
    Log    ${SRC} -> ${DST} UDP 20 Mbit/s: loss ${r}[loss_pct] %, jitter ${r}[jitter_ms] ms    console=True

A steered path (uSID carrier) and its uncompressed equivalent carry comparable TCP throughput
    ${lan}=    Set Variable    ${SITES}[tenant-b][dc3][lan]
    ${base}=    Iperf    ${SRC_B}    ${DST_B}    5
    Steer    add    pe1    tenant-b    ${lan}    p1    p3
    Sleep    2s
    ${usid}=    Iperf    ${SRC_B}    ${DST_B}    5
    Steer    add    pe1    tenant-b    ${lan}    p1    p3    --uncompressed
    Sleep    2s
    ${plain}=    Iperf    ${SRC_B}    ${DST_B}    5
    Steer    del    pe1    tenant-b    ${lan}
    Log    tenant-b dc1 -> dc3: shortest ${base}[mbps_received] · steered uSID ${usid}[mbps_received] · steered uncompressed ${plain}[mbps_received] Mbit/s    console=True
    Should Be True    ${usid}[mbps_received] >= ${FLOOR_MBPS}    msg=steered (uSID) path only ${usid}[mbps_received] Mbit/s
    Should Be True    ${plain}[mbps_received] >= ${FLOOR_MBPS}    msg=steered (uncompressed) path only ${plain}[mbps_received] Mbit/s
    Should Be True    ${usid}[mbps_received] >= 0.5 * ${base}[mbps_received]    msg=steering halved the throughput (${usid}[mbps_received] vs ${base}[mbps_received])

*** Keywords ***
Cleanup
    Run Keyword And Ignore Error    Steer    del    pe1    tenant-b    ${SITES}[tenant-b][dc3][lan]
    Close All Connections
