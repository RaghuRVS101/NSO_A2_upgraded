#!/usr/bin/env python3
"""operate <openrc> <tag> <ssh_key>

Stage 2 of the NSO project lifecycle.

Loops forever (until Ctrl-C) and re-converges the deployment every 30s:

  * read the wanted node count from servers.conf
  * ask the BASTION's alive.py how many nodes are reachable (single request
    that returns one line per node — see the assignment hint about using a
    single bastion request to learn the state of every node)
  * if too few    -> boot replacement(s) and reconfigure everything
  * if too many   -> delete the surplus and reconfigure everything
  * if anything changed -> regenerate inventory + ssh config + nodes.yaml
    on the bastion, then re-run the playbook so HAProxy / NGINX pick up
    the new backend set

Designed to be safe to restart: the live state is rebuilt from OpenStack
each loop, so a missing or stale ``state/<tag>.json`` is recovered
automatically.
"""

from __future__ import annotations

import signal
import sys
import time
from pathlib import Path

import requests

import nso
from nso import log

INTERVAL = 30


def usage() -> None:
    sys.exit("usage: operate <openrc> <tag> <ssh_key>")


def list_alive_from_bastion(bastion_ip: str) -> set[str]:
    """alive.py prints one line per node; lines that end in 'ms' indicate
    the node responded to ICMP."""
    try:
        r = requests.get(
            f"http://{bastion_ip}:{nso.ALIVE_PORT}/", timeout=10,
        )
    except Exception as e:
        log(f"bastion alive endpoint unreachable: {e}")
        return set()
    alive: set[str] = set()
    for line in r.text.splitlines():
        parts = line.split()
        if len(parts) >= 4 and parts[-1] == "ms":
            alive.add(parts[-3])
    return alive


def reconcile(conn, tag: str, state: nso.Deployment) -> bool:
    wanted = nso.read_required_node_count()
    log(f"Reading servers.conf, we need {wanted} nodes.")

    nodes = nso.list_tagged_servers(conn, tag, role="node")
    name_to_ip = {s.name: nso.get_internal_ip(s) for s in nodes}
    alive_ips = list_alive_from_bastion(state.bastion_ip)
    alive_names = {n for n, ip in name_to_ip.items() if ip in alive_ips}

    have = len(alive_names) if alive_ips else len(nodes)
    log(f"Checking solution, we have: {have} nodes.")

    changed = False

    dead_servers = (
        [s for s in nodes if s.name not in alive_names]
        if alive_ips else []
    )

    if len(nodes) < wanted or dead_servers:
        for s in dead_servers:
            log(f"Detecting lost node; {s.name}")
            conn.compute.delete_server(s, ignore_missing=True)
            changed = True

        survivors = [s for s in nodes if s not in dead_servers]
        survivor_names = [s.name for s in survivors]
        new_names: list[str] = []
        while len(survivor_names) + len(new_names) < wanted:
            idx = nso.next_node_index(
                survivor_names + new_names + state.node_names, tag,
            )
            new_names.append(f"{tag}_node_{idx}")
        if new_names:
            log(f"Launching new node/s; {','.join(new_names)}, waiting for completion.")
        for name in new_names:
            nso.boot_server(
                conn, name, state.image_id, state.flavor_id,
                state.network_id, state.keypair_name, f"{tag}_sg", tag,
            )
            changed = True

    elif len(nodes) > wanted:
        surplus = sorted([s.name for s in nodes], reverse=True)[:len(nodes) - wanted]
        for name in surplus:
            log(f"Removing surplus node {name}")
            srv = conn.compute.find_server(name)
            if srv:
                conn.compute.delete_server(srv, ignore_missing=True)
                changed = True

    if changed:
        log("Done, updating playbook and SSH config.")
        time.sleep(15)
        proxy = conn.compute.find_server(f"{tag}_proxy")
        nodes = nso.list_tagged_servers(conn, tag, role="node")
        state.node_names = sorted(s.name for s in nodes)
        state.save()
        proxy_internal = nso.get_internal_ip(proxy)
        node_map = {s.name: nso.get_internal_ip(s) for s in nodes}
        ssh_hosts = {f"{tag}_proxy": proxy_internal, **node_map}
        ssh_cfg = nso.write_ssh_config(
            tag, state.ssh_key_path, state.bastion_ip, ssh_hosts,
        )
        inventory = nso.write_inventory(
            tag, state.bastion_ip, state.proxy_ip,
            proxy_internal, node_map,
        )
        nso.run_ansible(tag, ssh_cfg, inventory)

        log("Validates operation.")
        try:
            for i in range(1, max(4, wanted + 1)):
                r = requests.get(
                    f"http://{state.proxy_ip}:{nso.SERVICE_PORT}/", timeout=5,
                )
                log(f"Request{i}: ...{r.text.strip()[-60:]}")
        except Exception as e:
            log(f"smoke-test request failed: {e}")
        log("OK")
    else:
        log("Sleeping.")

    return changed


def recover_state(conn, tag: str, ssh_key: str) -> nso.Deployment:
    """Rebuild a Deployment dataclass from live OpenStack metadata so that
    operate can resume after a restart even when the JSON state is gone."""
    state = nso.Deployment.load(tag)
    if state.bastion_ip and state.network_id:
        return state

    log("State file empty; rebuilding from OpenStack metadata.")
    bastion = conn.compute.find_server(f"{tag}_bastion")
    proxy = conn.compute.find_server(f"{tag}_proxy")
    if not bastion or not proxy:
        sys.exit("Cannot find bastion/proxy; run install first.")
    state.bastion_ip = nso.get_floating_ip(bastion)
    state.proxy_ip = nso.get_floating_ip(proxy)
    net = conn.network.find_network(f"{tag}_network")
    state.network_id = net.id if net else ""
    state.keypair_name = f"{tag}_key"
    state.image_id = (
        bastion.image["id"] if isinstance(bastion.image, dict) else ""
    )
    state.flavor_id = (
        bastion.flavor["id"] if isinstance(bastion.flavor, dict) else ""
    )
    state.ssh_key_path = str(Path(ssh_key).expanduser().resolve())
    state.save()
    return state


def main() -> None:
    if len(sys.argv) != 4:
        usage()
    openrc, tag, ssh_key = sys.argv[1], sys.argv[2], sys.argv[3]

    nso.source_openrc(openrc)
    conn = nso.connect()
    state = recover_state(conn, tag, ssh_key)

    stop = {"flag": False}

    def _sigint(*_):
        stop["flag"] = True
        log("Caught SIGINT, exiting after this iteration.")

    signal.signal(signal.SIGINT, _sigint)

    while not stop["flag"]:
        try:
            reconcile(conn, tag, state)
        except Exception as e:
            log(f"reconcile error: {e}")
        for _ in range(INTERVAL):
            if stop["flag"]:
                break
            time.sleep(1)


if __name__ == "__main__":
    main()
