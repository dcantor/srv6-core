*** Settings ***
Documentation     Internet breakout: a small VyOS firewall (fw-inet) is a CE of every tenant on one PE — VRF-lite, one attachment
...               circuit per tenant, each in the tenant's VRF on the firewall too — announcing nothing but a default route; its
...               uplink is the host's libvirt NAT network (DHCP) in its default VRF, where outbound tenant traffic is masqueraded.
...               The default route travels as a VPNv4 route with the PE's End.DT46 SID for that tenant, so every site of every
...               tenant reaches the internet; the firewall's stateful policy allows tenant -> internet only, so the tenants still
...               never reach each other (the cross-tenant matrix of suite 04 stays all-fail) and nothing new comes in.
Resource          ../resources/common.resource
Suite Setup       Require Breakout
Suite Teardown    Suite Teardown Close Connections

*** Variables ***
${PORTAL}         http://127.0.0.1:8091

*** Keywords ***
Require Breakout
    Skip If    not $INTERNET    no internet breakout in lab.conf (INTERNET_FW)
    Tcp Port Should Be Open    ${NODES}[${INTERNET}[fw]][mgmt_ip]    22

*** Test Cases ***
The firewall has an address from the host's NAT network on its uplink and its own default route through it
    ${fw}=    Set Variable    ${INTERNET}[fw]
    ${ifs}=    Vyos    ${fw}    show interfaces
    Should Match Regexp    ${ifs}    (?m)^${INTERNET_UPLINK}\\s+(\\d+\\.){3}\\d+/\\d+\\s.*u/u    msg=${fw}: no DHCP address on ${INTERNET_UPLINK}
    ${rt}=    Vyos    ${fw}    show ip route 0.0.0.0/0
    Should Match Regexp    ${rt}    (?s)Known via "static".*(\\d+\\.){3}\\d+, via ${INTERNET_UPLINK}    msg=${fw}: default route is not DHCP's static via ${INTERNET_UPLINK}
    ${out}=    Shell    ${fw}    ping -c 3 -W 2 ${INTERNET_PROBE}; true
    ${lost}=    Ping Loss    ${out}
    Should Be True    ${lost} < 3    msg=${fw} itself cannot reach ${INTERNET_PROBE}

The firewall is a CE of every tenant on the breakout PE, Established in the tenant VRF, sending exactly one prefix — the default route
    FOR    ${t}    IN    @{TENANTS}
        ${c}=    Set Variable    ${INTERNET_CIRCUITS}[${t}]
        ${sum}=    Vyos    ${c}[pe]    show bgp vrf ${t} summary
        ${line}=    Get Lines Containing String    ${sum}    ${c}[fw_ip]
        Should Match Regexp    ${line}    ^${c}[fw_ip]\\s+4\\s+${c}[asn]\\s+.*\\s1\\s+\\d+\\s+${c}[fw]    msg=${c}[pe]: session to ${c}[fw] (${c}[fw_ip], AS ${c}[asn]) in ${t} not Established with 1 prefix
        ${d}=    Vyos    ${c}[pe]    show bgp vrf ${t} ipv4 0.0.0.0/0
        Should Contain    ${d}    ${c}[fw_ip] from ${c}[fw_ip]    msg=${c}[pe]: the ${t} default route is not from ${c}[fw]
        ${fsum}=    Vyos    ${c}[fw]    show bgp vrf ${t} summary
        Should Match Regexp    ${fsum}    (?m)^${c}[pe_ip]\\s+4\\s+${CORE_AS}\\s+.*\\s\\d+\\s+\\d+\\s+${c}[pe]    msg=${c}[fw]: session to ${c}[pe] in ${t} not Established
    END

Every PE holds the default route in every tenant VRF as an SRv6 route to the breakout PE's End.DT46 SID for that tenant
    FOR    ${t}    IN    @{TENANTS}
        ${c}=    Set Variable    ${INTERNET_CIRCUITS}[${t}]
        ${sid}=    Shell    ${c}[pe]    sudo ip -6 route show | grep 'End.DT46 vrftable ${t}' | cut -d' ' -f1
        FOR    ${pe}    IN    @{PES}
            ${rt}=    Shell    ${pe}    sudo ip -c=never route show vrf ${t} default
            IF    '${pe}' == '${c}[pe]'
                Should Match Regexp    ${rt}    ^default .*via ${c}[fw_ip] dev ${c}[pe_port]    msg=${pe}: ${t} default route is not via ${c}[fw] on ${c}[pe_port]
            ELSE
                Should Match Regexp    ${rt}    (?s)^default .*encap seg6 mode encap segs 1 \\[ ${sid.strip()} \\]    msg=${pe}: ${t} default route is not SRv6-encapsulated to ${sid.strip()} (${c}[pe] ${t})
            END
        END
    END

The reflectors carry one default route per tenant, under the breakout PE's RD for that tenant
    FOR    ${rr}    IN    @{RRS}
        ${vpn}=    Vyos    ${rr}    show bgp ipv4 vpn 0.0.0.0/0
        FOR    ${t}    IN    @{TENANTS}
            ${c}=    Set Variable    ${INTERNET_CIRCUITS}[${t}]
            ${rd}=    Set Variable    ${NODES}[${c}[pe]][rd][${t}]
            Should Contain    ${vpn}    BGP routing table entry for ${rd}:0.0.0.0/0    msg=${rr}: no default route under RD ${rd} (${c}[pe] ${t})
        END
        ${n}=    Get Count    ${vpn}    BGP routing table entry for
        Should Be Equal As Integers    ${n}    ${{ len($TENANTS) }}    msg=${rr}: ${n} RDs carry a default route, expected one per tenant
    END

Every host of every tenant reaches the internet: ICMP to a public address and HTTP over the breakout
    FOR    ${t}    IN    @{TENANTS}
        FOR    ${dc}    IN    @{SITES}[${t}]
            ${s}=    Set Variable    ${SITES}[${t}][${dc}]
            ${out}=    Host    ${s}[host]    ping -c 3 -W 2 ${INTERNET_PROBE}; true
            ${lost}=    Ping Loss    ${out}
            Should Be True    ${lost} < 3    msg=${s}[host] (${t}, ${dc}): ${INTERNET_PROBE} unreachable through the breakout
            ${page}=    Host    ${s}[host]    wget -q -O- --timeout=10 ${INTERNET_URL} | head -c 200; true
            Should Match Regexp    ${page}    (?i)<(!doctype|html)    msg=${s}[host] (${t}, ${dc}): no HTML from ${INTERNET_URL} through the breakout:\n${page}
        END
    END

Tenant traffic leaves the firewall masqueraded with its uplink address, and the path from a remote site goes through the breakout PE and the firewall
    ${t}=    Set Variable    ${TENANTS}[0]
    ${c}=    Set Variable    ${INTERNET_CIRCUITS}[${t}]
    ${remote}=    Evaluate    next(s for s in $SITES[$t].values() if s["pe"] != "${c}[pe]")
    ${fw_up}=    Shell    ${c}[fw]    ip -4 -o addr show ${INTERNET_UPLINK} | awk '{print $4}' | cut -d/ -f1
    ${h}=    Start Background    ${MGMT}[${c}[fw]]    sudo timeout 20 tcpdump -ni ${INTERNET_UPLINK} -c 2 icmp and dst host ${INTERNET_PROBE} 2>/dev/null
    Sleep    2s
    Host    ${remote}[host]    ping -c 4 -i 0.3 -W 2 ${INTERNET_PROBE} >/dev/null; true
    ${cap}=    Finish Background    ${h}
    Should Match Regexp    ${cap}    IP ${fw_up.strip()} > ${INTERNET_PROBE}: ICMP echo request    msg=${c}[fw]: outbound packets are not masqueraded with ${fw_up.strip()}:\n${cap}
    Should Not Contain    ${cap}    ${remote}[host_ip] >    msg=${c}[fw]: a tenant address leaked onto the uplink un-NATed
    ${tr}=    Host    ${remote}[host]    traceroute -n -w 1 -m 6 ${INTERNET_PROBE} 2>&1; true
    Should Match Regexp    ${tr}    (?m)^\\s*\\d+\\s+${c}[fw_ip]\\s    msg=${remote}[host]: the path to the internet does not cross ${c}[fw] (${c}[fw_ip]):\n${tr}

The tenants still never meet: a tenant host cannot reach the other tenant's hosts through the breakout, and the firewall drops and logs the attempt
    ${a}    ${b}=    Set Variable    ${TENANTS}[0]    ${TENANTS}[1]
    ${src}=    Set Variable    ${SITES}[${a}][dc1]
    ${dst}=    Set Variable    ${SITES}[${b}][dc2]
    ${out}=    Host    ${src}[host]    ping -c 3 -W 2 ${dst}[host_ip]; true
    ${lost}=    Ping Loss    ${out}
    Should Be Equal As Integers    ${lost}    3    msg=${src}[host] (${a}) reached ${dst}[host] (${b}) — the breakout leaks between tenants
    ${fwlog}=    Vyos    ${INTERNET}[fw]    show log firewall | tail -50
    Should Match Regexp    ${fwlog}    FWD-filter-8-D\\]IN=${a} .*SRC=${src}[host_ip] DST=${dst}[host_ip]    msg=${INTERNET}[fw] did not log the ${a} -> ${b} packets as dropped (rule 8: tenant space is never forwarded)
    ${fwd}=    Vyos    ${INTERNET}[fw]    show firewall ipv4 forward filter
    Should Match Regexp    ${fwd}    (?m)^default\\s+drop    msg=${INTERNET}[fw]: the forward policy does not default to drop

The firewall accepts nothing new from the internet side: its input policy drops by default and only management, BGP from the PE and DHCP are open
    ${inp}=    Vyos    ${INTERNET}[fw]    show firewall ipv4 input filter
    Should Match Regexp    ${inp}    (?m)^default\\s+drop    msg=${INTERNET}[fw]: the input policy does not default to drop
    Should Match Regexp    ${inp}    iifname "eth0"    msg=${INTERNET}[fw]: management from the OOB network is not allowed explicitly
    Should Match Regexp    ${inp}    udp sport 67 iifname "${INTERNET_UPLINK}"    msg=${INTERNET}[fw]: DHCP from the host is not allowed explicitly
    FOR    ${t}    IN    @{TENANTS}
        ${c}=    Set Variable    ${INTERNET_CIRCUITS}[${t}]
        Should Match Regexp    ${inp}    tcp dport 179 iifname "${c}[fw_port]"    msg=${INTERNET}[fw]: BGP from ${c}[pe] on ${c}[fw_port] is not allowed explicitly
    END
    ${nat}=    Vyos    ${INTERNET}[fw]    show nat source rules
    Should Match Regexp    ${nat}    172\\.16\\.0\\.0/12\\s+0\\.0\\.0\\.0/0\\s+any\\s+${INTERNET_UPLINK}\\s+masquerade    msg=${INTERNET}[fw]: no masquerade rule for the tenant space on ${INTERNET_UPLINK}

The portal and Prometheus see the breakout: firewall reachable, both sessions up, a default route in every tenant VRF on every PE
    ${text}=    Http Get    ${PORTAL}/metrics
    ${fw}=    Metric Samples    ${text}    lab_internet_fw_reachable
    Length Should Be    ${fw}    1
    Should Be Equal As Numbers    ${fw}[0][value]    1
    ${bgp}=    Metric Samples    ${text}    lab_internet_bgp_up
    Length Should Be    ${bgp}    ${{ len($TENANTS) }}
    FOR    ${b}    IN    @{bgp}
        Should Be Equal As Numbers    ${b}[value]    1    msg=${b}[labels][tenant]: breakout session down according to the portal
    END
    ${dr}=    Metric Samples    ${text}    lab_internet_default_route
    Length Should Be    ${dr}    ${{ len($TENANTS) * len($PES) }}
    FOR    ${d}    IN    @{dr}
        Should Be Equal As Numbers    ${d}[value]    1    msg=${d}[labels][pe] ${d}[labels][tenant]: no default route according to the portal
    END
