*** Settings ***
Documentation     Shared settings for the SRv6 core lab suites.
Library           LabLib.py
Variables         lab_vars.py
Library           Collections
Library           String

*** Keywords ***
Suite Teardown Close Connections
    Close All Connections

Vyos
    [Documentation]    Run an operational command on a VyOS node by name.
    [Arguments]    ${node}    ${command}    ${timeout}=90
    ${out}=    Run Vyos Command    ${MGMT}[${node}]    ${command}    ${timeout}
    RETURN    ${out}

Shell
    [Documentation]    Run a Linux shell command on a VyOS node by name (sudo allowed).
    [Arguments]    ${node}    ${command}
    ${out}=    Vyos Shell    ${MGMT}[${node}]    ${command}
    RETURN    ${out}

Host
    [Documentation]    Run a command on a CirrOS host by name.
    [Arguments]    ${host}    ${command}
    ${out}=    Host Command    ${MGMT}[${host}]    ${command}
    RETURN    ${out}
