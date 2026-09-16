*** Settings ***
Documentation     The IPv6-only IS-IS level-2 underlay: adjacencies on every core link, every loopback reachable,
...               and enough MTU headroom for the SRv6 encapsulation.
Resource          ../resources/common.resource
Suite Teardown    Suite Teardown Close Connections

*** Test Cases ***
Every core link has an IS-IS level-2 adjacency in state Up
    FOR    ${n}    IN    @{CORE}
        ${out}=    Vyos    ${n}    show isis neighbor
        ${expected}=    Get Dictionary Keys    ${ISIS_NEIGHBORS}[${n}]
        FOR    ${if}    IN    @{expected}
            Should Match Regexp    ${out}    (?m)^\\s*\\S+\\s+${if}\\s+2\\s+Up    msg=${n} ${if}: no level-2 adjacency Up (peer ${ISIS_NEIGHBORS}[${n}][${if}])
        END
        ${ups}=    Regex Findall    ${out}    (?m)^\\s*\\S+\\s+(eth\\d+)\\s+2\\s+Up
        ${n_up}=    Get Length    ${ups}
        ${n_exp}=    Get Length    ${expected}
        Should Be Equal As Integers    ${n_up}    ${n_exp}    msg=${n}: unexpected number of adjacencies
    END

Every core node knows every other loopback through IS-IS
    FOR    ${n}    IN    @{CORE}
        ${rib}=    Vyos    ${n}    show ipv6 route isis
        FOR    ${other}    IN    @{CORE}
            IF    '${other}' != '${n}'
                Should Contain    ${rib}    ${LOOPBACK}[${other}]/128    msg=${n} has no IS-IS route to ${other}'s loopback
            END
        END
    END

Every PE reaches every other PE's loopback, including with a 1600-byte packet that must not be fragmented
    FOR    ${a}    IN    @{PES}
        FOR    ${b}    IN    @{PES}
            IF    '${a}' != '${b}'
                ${out}=    Vyos    ${a}    ping ${LOOPBACK}[${b}] count 2 interval 0.3
                Should Contain    ${out}    0% packet loss    msg=${a} cannot ping ${b} (${LOOPBACK}[${b}])
                ${big}=    Vyos    ${a}    ping ${LOOPBACK}[${b}] count 1 size 1600 do-not-fragment
                Should Contain    ${big}    1 received    msg=${a} -> ${b}: 1600-byte DF ping failed (no headroom for the SRv6 encapsulation)
            END
        END
    END
