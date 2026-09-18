# Ansible over the same rendered configurations

The lab's third way to push configuration (after the serial console for first boot and `tools/vyos_push.py` over SSH):
the `vyos.vyos` collection — what most people already run against VyOS. Nothing is modelled twice: the playbooks push
`nodes/<node>/vyos_config.txt`, the file `tools/render.py` renders from `lab.conf` (or Nautobot), and the inventory is
`lab.sh inventory` (dynamic: groups `pe` / `p` / `ce` / `hosts`, `dc1..4`, `tenant_a` / `tenant_b`; every lab fact as a host var).

```bash
./lab.sh ansible site.yml --check --diff          # what every node is missing, as a VyOS config diff — nothing applied
./lab.sh ansible site.yml -l pe4                  # push (commit + save) to one node; "in sync" when nothing changes
./lab.sh ansible show.yml -e cmd="show bfd peers brief"
./lab.sh ansible facts.yml                        # vyos_facts (version, interfaces, running config) -> ansible/facts/<node>.json
cd ansible && ../tests/.venv/bin/ansible-inventory --graph
```

`site.yml` uses `vyos_config` with `lines` and `match: line`: **additive and idempotent**, like `vyos_push.py`. It shows
and restores lines that are *missing* from a node (a deleted locator leak shows up as a `+` diff on the right VRF), but
it cannot see lines that were *added* by hand (a `neighbor … shutdown`): that needs a full compare of running vs intended,
which the `vyos.vyos` resource modules (`state: overridden`) only offer for the features they model — none of the SRv6 / VRF /
BFD / IS-IS parts of this lab. `tools/frr_logging.py` runs as a handler after a change (the FRR log level is not a CLI setting).

Transport is `network_cli` over SSH with the lab's default credentials (paramiko; install `ansible-pylibssh` for speed).
Collections install into `ansible/collections/` (ignored by git) on first use.
