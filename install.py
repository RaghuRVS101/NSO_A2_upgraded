#!/usr/bin/env python3
"""install <openrc> <tag> <ssh_key>

Stage 1 of the NSO project lifecycle.

Provisions (or recovers) the entire deployment in OpenStack:

  * 2 floating IPs (reusing unattached project IPs first)
  * keypair, network, subnet, router, security group  (all tagged)
  * a BASTION VM (public IP, runs alive.py + acts as ssh jump host)
  * a PROXY  VM (public IP, runs HAProxy on TCP/5000 and NGINX on UDP/6000)
  * N SERVICE VMs (private only, run service.py + snmpd)

Then writes a per-deployment SSH config and Ansible inventory and runs the
playbook to configure every host. State is persisted in
``state/<tag>.json`` for ``operate``, but cleanup is tag-driven and
survives loss of the state file.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

import nso
from nso import log


def usage() -> None:
    sys.exit("usage: install <openrc> <tag> <ssh_key>")


def main() -> None:
    if len(sys.argv) != 4:
        usage()
    openrc, tag, ssh_key = sys.argv[1], sys.argv[2], sys.argv[3]

    log(f"Starting deployment of {tag} using {Path(openrc).name} for credentials.")
    nso.source_openrc(openrc)
    conn = nso.connect()

    state = nso.Deployment(
        tag=tag,
        ssh_key_path=str(Path(ssh_key).expanduser().resolve()),
    )

    # ---- keypair -------------------------------------------------------------
    state.keypair_name = nso.get_or_create_keypair(conn, tag, ssh_key)

    # ---- network + subnet + router ------------------------------------------
    state.network_id, state.subnet_id, state.router_id = \
        nso.get_or_create_network(conn, tag)

    # ---- security group ------------------------------------------------------
    state.security_group_id = nso.get_or_create_security_group(conn, tag)

    # ---- image + flavor ------------------------------------------------------
    state.image_id, _ = nso.pick_image(conn)
    state.flavor_id = nso.pick_flavor(conn)

    # ---- bastion -------------------------------------------------------------
    bastion_name = f"{tag}_bastion"
    bastion = conn.compute.find_server(bastion_name)
    if bastion is None:
        log(f"Did not detect {bastion_name}, launching it.")
        # Allocate floating IP for bastion (reuse unattached ones first).
        free = sum(1 for f in conn.network.ips() if f.port_id is None)
        log(f"Checking if we have floating IPs availible, we have {free} availible.")
        bastion_ip = nso.reuse_or_allocate_floating_ip(conn, tag, state)
        bastion = nso.boot_server(
            conn, bastion_name, state.image_id, state.flavor_id,
            state.network_id, state.keypair_name, f"{tag}_sg", tag,
        )
        nso.attach_floating_ip(conn, bastion, bastion_ip)
    else:
        log(f"{bastion_name} already present.")
        bastion_ip = nso.get_floating_ip(bastion)
        if not bastion_ip:
            # VM exists but has no FIP yet (partial previous run).
            free = sum(1 for f in conn.network.ips() if f.port_id is None)
            log(f"Checking if we have floating IPs availible, we have {free} availible.")
            bastion_ip = nso.reuse_or_allocate_floating_ip(conn, tag, state)
            nso.attach_floating_ip(conn, bastion, bastion_ip)
        else:
            log(f"  bastion already has floating IP {bastion_ip}.")
    state.bastion_ip = bastion_ip

    # ---- proxy ---------------------------------------------------------------
    proxy_name = f"{tag}_proxy"
    proxy = conn.compute.find_server(proxy_name)
    if proxy is None:
        log(f"Did not detect {proxy_name}, launching it.")
        free = sum(1 for f in conn.network.ips() if f.port_id is None)
        log(f"Checking if we have floating IPs availible, we have {free} availible.")
        proxy_ip = nso.reuse_or_allocate_floating_ip(
            conn, tag, state, exclude={bastion_ip},
        )
        proxy = nso.boot_server(
            conn, proxy_name, state.image_id, state.flavor_id,
            state.network_id, state.keypair_name, f"{tag}_sg", tag,
        )
        nso.attach_floating_ip(conn, proxy, proxy_ip)
    else:
        log(f"{proxy_name} already present.")
        proxy_ip = nso.get_floating_ip(proxy)
        if not proxy_ip:
            free = sum(1 for f in conn.network.ips() if f.port_id is None)
            log(f"Checking if we have floating IPs availible, we have {free} availible.")
            proxy_ip = nso.reuse_or_allocate_floating_ip(
                conn, tag, state, exclude={bastion_ip},
            )
            nso.attach_floating_ip(conn, proxy, proxy_ip)
        else:
            log(f"  proxy already has floating IP {proxy_ip}.")
    state.proxy_ip = proxy_ip

    # ---- service nodes -------------------------------------------------------
    wanted = nso.read_required_node_count()
    existing_nodes = nso.list_tagged_servers(conn, tag, role="node")
    existing_names = [s.name for s in existing_nodes]

    to_launch = max(0, wanted - len(existing_nodes))
    log(f"Will need {wanted} nodes (servers.conf), launching them.")
    new_names: list[str] = []
    for _ in range(to_launch):
        idx = nso.next_node_index(existing_names + new_names, tag)
        new_names.append(f"{tag}_node_{idx}")
    for name in new_names:
        nso.boot_server(
            conn, name, state.image_id, state.flavor_id,
            state.network_id, state.keypair_name, f"{tag}_sg", tag,
        )
    log(f"{', '.join(new_names) if new_names else 'no new nodes'} Done.")

    # ---- wait for cloud-init / sshd warm-up ---------------------------------
    log("Waiting for nodes to complete their installation.")
    time.sleep(20)

    # ---- refresh server list / build per-host IP map ------------------------
    bastion = conn.compute.find_server(bastion_name)
    proxy = conn.compute.find_server(proxy_name)
    nodes = nso.list_tagged_servers(conn, tag, role="node")
    state.node_names = sorted(s.name for s in nodes)

    proxy_internal = nso.get_internal_ip(proxy)
    node_map = {s.name: nso.get_internal_ip(s) for s in nodes}
    ssh_hosts = {f"{tag}_proxy": proxy_internal, **node_map}

    # ---- write artefacts -----------------------------------------------------
    ssh_cfg = nso.write_ssh_config(tag, state.ssh_key_path, bastion_ip, ssh_hosts)
    inventory = nso.write_inventory(
        tag, bastion_ip, proxy_ip, proxy_internal, node_map,
    )

    # ---- run ansible ---------------------------------------------------------
    nso.run_ansible(tag, ssh_cfg, inventory)

    # ---- save state + smoke test --------------------------------------------
    state.save()

    log("Validates operation.")
    try:
        import requests
        for i in range(1, max(4, wanted + 1)):
            r = requests.get(f"http://{proxy_ip}:{nso.SERVICE_PORT}/", timeout=5)
            log(f"Request{i}: ...{r.text.strip()[-60:]}")
    except Exception as e:
        log(f"smoke-test request failed: {e}")
    log("OK")


if __name__ == "__main__":
    main()
