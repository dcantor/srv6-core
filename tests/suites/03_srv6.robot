*** Settings ***
Documentation     SRv6: every core node has its locator, IS-IS carries every node's SRv6 capability and locator,
...               and every PE has the local SIDs installed in the Linux data plane (End, End.X, End.DT46 into the VRF).
Resource          ../resources/common.resource
Suite Teardown    Suite Teardown Close Connections

*** Test Cases ***
Every core node has its SRv6 locator up with the lab's SID structure
    FOR    ${n}    IN    @{CORE}
        ${out}=    Vyos    ${n}    show segment-routing srv6 locator
        Should Match Regexp    ${out}    (?m)^main\\s+\\d+\\s+${LOCATOR}[${n}]\\s+Up    msg=${n}: locator main ${LOCATOR}[${n}] is not Up
        ${cfg}=    Vyos    ${n}    show configuration commands | match 'srv6 locator main'
        Should Contain    ${cfg}    block-len '${SRV6}[block_len]'
        Should Contain    ${cfg}    node-len '${SRV6}[node_len]'
        Should Contain    ${cfg}    func-bits '${SRV6}[func_bits]'
        Should Contain    ${cfg}    format '${SRV6}[format]'
        IF    $USID    Should Contain    ${cfg}    behavior-usid
        ${sids}=    Shell    ${n}    sudo vtysh -c 'show segment-routing srv6 sid'
        ${want}=    Set Variable If    $USID    uN    End
        Should Match Regexp    ${sids}    (?m)^\\s*${{ $LOCATOR[$n].split('/')[0] }}\\s+${want}\\b    msg=${n}: no ${want} SID for the locator
    END

IS-IS advertises the SRv6 capability of every core node and every locator is in every routing table
    ${nodes}=    Vyos    ${RR}    show isis segment-routing srv6 node
    FOR    ${n}    IN    @{CORE}
        Should Contain    ${nodes}    ${SYSID}[${n}]    msg=${RR} does not see ${n} (${SYSID}[${n}]) as an SRv6 node in IS-IS
    END
    FOR    ${n}    IN    @{CORE}
        ${rib}=    Vyos    ${n}    show ipv6 route
        FOR    ${other}    IN    @{CORE}
            IF    '${other}' != '${n}'
                Should Match Regexp    ${rib}    (?m)^I>\\*?\\s*${LOCATOR}[${other}]    msg=${n} has no IS-IS route to ${other}'s locator ${LOCATOR}[${other}]
            END
        END
    END

Every PE has one End.DT46 SID per tenant VRF (serving IPv4 and IPv6), an End SID, and End.X SIDs installed in the Linux data plane
    FOR    ${pe}    IN    @{PES}
        ${sids}=    Shell    ${pe}    sudo ip -6 route show | grep seg6local
        ${bgp}=    Vyos    ${pe}    show bgp segment-routing srv6
        FOR    ${t}    IN    @{TENANTS}
            ${dt4}=    Regex Findall    ${sids}    (?m)^(\\S+)\\s.*action End\\.DT46 vrftable ${t}\\b
            Length Should Be    ${dt4}    1    msg=${pe}: expected exactly one End.DT46 SID for VRF ${t}
            Ip In Network    ${dt4}[0]    ${LOCATOR}[${pe}]
            Should Match Regexp    ${bgp}    (?s)name: ${t}.*?per-vrf tovpn_sid: ${dt4}[0]    msg=${pe}: BGP does not export ${dt4}[0] as the per-VRF SID for ${t}
        END
        ${all_dt4}=    Regex Findall    ${sids}    (?m)action End\\.DT46 vrftable
        ${n_t}=    Get Length    ${TENANTS}
        Length Should Be    ${all_dt4}    ${n_t}    msg=${pe}: a tenant VRF without its own SID, or a stray one
        Should Match Regexp    ${sids}    (?m)^\\S+\\s.*action End (dev dum0|flavors next-csid)    msg=${pe}: no End / uN SID from IS-IS
        IF    $USID    Should Match Regexp    ${sids}    (?m)^${{ $LOCATOR[$pe] }}\\s.*action End flavors next-csid lblen ${SRV6}[block_len] nflen ${SRV6}[node_len]    msg=${pe}: the uN SID must carry the NEXT-C-SID flavour
        ${endx}=    Regex Findall    ${sids}    (?m)action End\\.X nh6
        ${n_core}=    Get Length    ${ISIS_NEIGHBORS}[${pe}]
        Length Should Be    ${endx}    ${n_core}    msg=${pe}: expected one End.X SID per core adjacency
    END

The SRv6 encapsulation is enabled on every core interface and uses the loopback as source
    FOR    ${n}    IN    @{CORE}
        ${ifs}=    Get Dictionary Keys    ${ISIS_NEIGHBORS}[${n}]
        FOR    ${if}    IN    @{ifs}
            ${v}=    Shell    ${n}    cat /proc/sys/net/ipv6/conf/${if}/seg6_enabled
            Should Be Equal    ${v.strip()}    1    msg=${n} ${if}: seg6 not enabled
        END
        ${src}=    Vyos    ${n}    show configuration commands | match 'encapsulation source-address'
        Should Contain    ${src}    '${LOOPBACK}[${n}]'
    END
