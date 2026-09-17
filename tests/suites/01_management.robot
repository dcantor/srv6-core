*** Settings ***
Documentation     Every node answers on its out-of-band address and identifies itself.
Resource          ../resources/common.resource
Suite Teardown    Suite Teardown Close Connections

*** Test Cases ***
Every VyOS node is reachable over the OOB network and runs VyOS with the expected host name
    FOR    ${n}    IN    @{VYOS}
        Host Ping    ${MGMT}[${n}]
        Tcp Port Should Be Open    ${MGMT}[${n}]    22
        ${v}=    Vyos    ${n}    show version | match Version
        Should Contain    ${v}    VyOS
        ${h}=    Vyos    ${n}    show configuration commands | match host-name
        Should Contain    ${h}    host-name '${n}'
    END

Every tenant host is reachable over the OOB network and carries its data-centre address
    FOR    ${h}    IN    @{HOSTS}
        Host Ping    ${MGMT}[${h}]
        Tcp Port Should Be Open    ${MGMT}[${h}]    22
        ${name}=    Host    ${h}    hostname
        Should Be Equal    ${name.strip()}    ${h}
        ${addr}=    Host    ${h}    ip -4 addr show eth1
        Should Contain    ${addr}    ${HOST_IP}[${h}]/24
    END

The core links run jumbo frames and the day-0 configuration is saved
    FOR    ${n}    IN    @{CORE}
        ${mtu}=    Vyos    ${n}    show configuration commands | match mtu
        ${count}=    Get Count    ${mtu}    mtu '9000'
        ${expected}=    Get Length    ${ISIS_NEIGHBORS}[${n}]
        Should Be Equal As Integers    ${count}    ${expected}    msg=${n}: every core link must have mtu 9000
        ${saved}=    Shell    ${n}    grep -c host-name /config/config.boot
        Should Not Be Equal    ${saved.strip()}    0    msg=${n}: /config/config.boot has no host-name (configuration never saved)
    END
