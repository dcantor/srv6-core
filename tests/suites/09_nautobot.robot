*** Settings ***
Documentation     Nautobot as the source of truth: the lab is modelled in the shared Nautobot (devices, interfaces, cables,
...               addresses, VRFs with route targets and per-PE RDs, BGP instances and peerings, config context), and the
...               VyOS configurations rendered from that model are identical to the ones rendered from lab.conf and present
...               on the routers.
Resource          ../resources/common.resource
Suite Teardown    Suite Teardown Close Connections

*** Variables ***
${NB_ROLE_MAP}    {"pe": "srv6-pe", "p": "srv6-p", "ce": "srv6-ce", "fw": "srv6-fw", "host": "host"}

*** Test Cases ***
Every device is in Nautobot with its role, location, platform and primary address
    ${roles}=    Evaluate    json.loads($NB_ROLE_MAP)    modules=json
    FOR    ${n}    IN    @{NODES}
        ${d}=    Nautobot Get    dcim/devices/    name=${n}    depth=1
        Should Be Equal As Integers    ${d}[count]    1    msg=${n} missing in Nautobot
        ${dev}=    Set Variable    ${d}[results][0]
        IF    '${NODES}[${n}][role]' == 'ext-ce'    CONTINUE    # another lab's router: its seed owns role / location / platform
        Should Be Equal    ${dev}[role][name]    ${roles}[${NODES}[${n}][role]]
        ${want_loc}=    Set Variable If    '${NODES}[${n}][dc]' == 'core'    srv6-core    ${NODES}[${n}][dc]
        Should Be Equal    ${dev}[location][name]    ${want_loc}
        Should Be Equal    ${dev}[primary_ip4][address]    ${MGMT}[${n}]/24
        ${plat}=    Set Variable If    '${NODES}[${n}][role]' == 'host'    linux    vyos
        Should Be Equal    ${dev}[platform][name]    ${plat}
    END

Every link is a cable between the right interfaces, with both addresses in the right prefix
    FOR    ${l}    IN    @{LINKS}
        ${d}=    Nautobot Graphql    { interfaces(device:["${l}[a]"], name:"${l}[a_port]") { ip_addresses { address parent { prefix } } connected_interface { name device { name } } } }
        ${i}=    Set Variable    ${d}[interfaces][0]
        Should Be Equal    ${i}[connected_interface][device][name]    ${l}[b]    msg=${l}[a] ${l}[a_port] is not cabled to ${l}[b]
        Should Be Equal    ${i}[connected_interface][name]    ${l}[b_port]
        Should Be Equal    ${i}[ip_addresses][0][address]    ${l}[a_ip]
        Should Be Equal    ${i}[ip_addresses][0][parent][prefix]    ${l}[prefix]
    END

Core nodes carry their IS-IS NET, SRv6 locator, loopback and router-id
    FOR    ${n}    IN    @{CORE}
        ${d}=    Nautobot Graphql    { devices(name:["${n}"]) { cf_isis_net cf_srv6_locator interfaces(name:["lo","dum0"]) { name ip_addresses { address } } bgp_routing_instances { router_id { address } autonomous_system { asn } } } }
        ${dev}=    Set Variable    ${d}[devices][0]
        Should Be Equal    ${dev}[cf_isis_net]    ${NODES}[${n}][isis_net]
        Should Be Equal    ${dev}[cf_srv6_locator]    ${LOCATOR}[${n}]
        ${lo}=    Evaluate    [i for i in $dev["interfaces"] if i["name"] == "lo"][0]["ip_addresses"][0]["address"]
        Should Be Equal    ${lo}    ${LOOPBACK}[${n}]/128
        ${pf}=    Nautobot Get    ipam/prefixes/    prefix=${LOCATOR}[${n}]    depth=1
        Should Be Equal    ${pf}[results][0][role][name]    srv6-locator
        IF    $NODES[$n]["asn"]
            Should Be Equal    ${dev}[bgp_routing_instances][0][router_id][address]    ${NODES}[${n}][router_id]/32
            Should Be Equal As Integers    ${dev}[bgp_routing_instances][0][autonomous_system][asn]    ${NODES}[${n}][asn]
        END
    END

Tenants are VRFs with their route targets, prefixes, and a route distinguisher per PE
    FOR    ${t}    IN    @{TENANTS}
        ${d}=    Nautobot Graphql    { vrfs(name:["${t}"]) { tenant { name } import_targets { name } export_targets { name } prefixes { prefix } } }
        ${v}=    Set Variable    ${d}[vrfs][0]
        Should Be Equal    ${v}[tenant][name]    ${t}
        Should Be Equal    ${v}[import_targets][0][name]    ${VRF_RT}[${t}]
        Should Be Equal    ${v}[export_targets][0][name]    ${VRF_RT}[${t}]
        ${pfx}=    Evaluate    sorted([p["prefix"] for p in $v["prefixes"]])
        ${want}=    Evaluate    sorted([l["prefix"] for l in $LINKS if l["tenant"] == $t] + [l["prefix6"] for l in $LINKS if l["tenant"] == $t and l.get("prefix6")])
        Lists Should Be Equal    ${pfx}    ${want}    msg=${t}: VRF prefixes differ from the tenant links (IPv4 and their IPv6 twins)
        FOR    ${dc}    IN    @{SITES}[${t}]
            ${s}=    Set Variable    ${SITES}[${t}][${dc}]
            ${va}=    Nautobot Get    ipam/vrf-device-assignments/    vrf=${t}    device=${s}[pe]
            Should Be Equal    ${va}[results][0][rd]    ${s}[rd]    msg=${s}[pe]: RD for ${t}
        END
    END

BGP is modelled: every PE peers with every reflector in VPNv4 + VPNv6 and with its CE per family in each tenant VRF
    FOR    ${pe}    IN    @{PES}
        ${d}=    Nautobot Graphql    { devices(name:["${pe}"]) { bgp_routing_instances { address_families { afi_safi vrf { name } extra_attributes } endpoints { description role { name } source_ip { address } peer { routing_instance { device { name } } source_ip { address } } address_families { afi_safi } } } } }
        ${ri}=    Set Variable    ${d}[devices][0][bgp_routing_instances][0]
        ${afs}=    Evaluate    sorted([(af["afi_safi"].lower(), af["vrf"]["name"] if af["vrf"] else "") for af in $ri["address_families"]])
        ${want}=    Evaluate    sorted([("vpnv4_unicast", ""), ("vpnv6_unicast", "")] + [(af, t) for t in $TENANTS for af in ("ipv4_unicast", "ipv6_unicast")])
        Lists Should Be Equal    ${afs}    ${want}
        FOR    ${rr}    IN    @{RRS}
            ${ep}=    Evaluate    [e for e in $ri["endpoints"] if e["peer"]["routing_instance"]["device"]["name"] == $rr][0]
            Should Be Equal    ${ep}[role][name]    rr-client
            Should Be Equal    ${ep}[source_ip][address]    ${LOOPBACK}[${pe}]/128
            Should Be Equal    ${ep}[peer][source_ip][address]    ${LOOPBACK}[${rr}]/128
            ${eafs}=    Evaluate    sorted(a["afi_safi"].lower() for a in $ep["address_families"])
            Lists Should Be Equal    ${eafs}    ${{ ["vpnv4_unicast", "vpnv6_unicast"] }}    msg=${pe} -> ${rr}: the session must carry VPNv4 and VPNv6
        END
        FOR    ${t}    IN    @{TENANTS}
            ${s}=    Evaluate    [s for s in $SITES[$t].values() if s["pe"] == $pe][0]
            ${ep}=    Evaluate    [e for e in $ri["endpoints"] if e["description"] == "eBGP %s (%s)" % ($s["ce"], $t)][0]
            Should Be Equal    ${ep}[source_ip][address]    ${s}[pe_wan_ip]/30
            Should Be Equal    ${ep}[peer][source_ip][address]    ${s}[ce_wan_ip]/30
            Should Be Equal    ${ep}[peer][routing_instance][device][name]    ${s}[ce]
            ${ep6}=    Evaluate    [e for e in $ri["endpoints"] if e["description"] == "eBGP %s (%s, IPv6)" % ($s["ce"], $t)][0]
            Should Be Equal    ${ep6}[source_ip][address]    ${s}[pe_wan_ip6]/64
            Should Be Equal    ${ep6}[peer][source_ip][address]    ${s}[ce_wan_ip6]/64
            Should Be Equal    ${{ $ep6["address_families"][0]["afi_safi"].lower() }}    ipv6_unicast
        END
    END

The configuration rendered from Nautobot is identical to the one rendered from lab.conf
    ${rc}    ${out}=    Nautobot Render    --check
    Should Be Equal As Integers    ${rc}    0    msg=${out}
    ${n}=    Get Count    ${out}    Nautobot == lab.conf
    ${expected}=    Get Length    ${VYOS}
    Should Be Equal As Integers    ${n}    ${expected}

Every rendered configuration line is present on the running routers
    ${rc}    ${out}=    Nautobot Render    --live
    Should Be Equal As Integers    ${rc}    0    msg=${out}

The internet breakout is modelled: the firewall is a CE of every tenant on its PE, with a peering per tenant and the VRF-lite import attributes
    Skip If    not $INTERNET    no internet breakout in lab.conf
    ${fw}=    Set Variable    ${INTERNET}[fw]
    ${d}=    Nautobot Graphql    { devices(name:["${fw}"]) { role { name } vrf_assignments { vrf { name } } bgp_routing_instances { autonomous_system { asn } address_families { afi_safi vrf { name } extra_attributes } endpoints { description source_ip { address } peer { routing_instance { device { name } } source_ip { address } } } } } }
    ${dev}=    Set Variable    ${d}[devices][0]
    Should Be Equal    ${dev}[role][name]    srv6-fw
    ${vrfs}=    Evaluate    sorted(v["vrf"]["name"] for v in $dev["vrf_assignments"])
    Lists Should Be Equal    ${vrfs}    ${TENANTS}    msg=${fw}: not assigned to every tenant VRF
    ${ri}=    Set Variable    ${dev}[bgp_routing_instances][0]
    Should Be Equal As Integers    ${ri}[autonomous_system][asn]    ${INTERNET}[asn]
    FOR    ${t}    IN    @{TENANTS}
        ${c}=    Set Variable    ${INTERNET_CIRCUITS}[${t}]
        ${af}=    Evaluate    [a for a in $ri["address_families"] if a["vrf"] and a["vrf"]["name"] == $t][0]
        Should Be Equal    ${af}[extra_attributes][import_vrf][0]    default    msg=${fw} ${t}: the VRF must import the default VRF (the default route)
        ${ep}=    Evaluate    [e for e in $ri["endpoints"] if e["description"] == "eBGP %s (%s) - default route only (internet)" % ($c["pe"], $t)][0]
        Should Be Equal    ${ep}[source_ip][address]    ${c}[fw_ip]/30
        Should Be Equal    ${ep}[peer][routing_instance][device][name]    ${c}[pe]
        Should Be Equal    ${ep}[peer][source_ip][address]    ${c}[pe_ip]/30
    END
    ${af0}=    Evaluate    [a for a in $ri["address_families"] if not a["vrf"]][0]
    Lists Should Be Equal    ${af0}[extra_attributes][import_vrf]    ${TENANTS}    msg=${fw}: the default VRF must import every tenant VRF (return traffic)
