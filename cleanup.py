#!/usr/bin/env python3
"""cleanup <openrc> <tag> <ssh_key>

Stage 3 of the NSO project lifecycle.

Releases every cloud resource we can attribute to ``<tag>``:

  servers (bastion, proxy, all nodes) -> security groups -> router
  -> subnet -> network -> keypair -> floating IPs we allocated (kept tagged
  at create time so we don't accidentally delete a pre-existing IP we
  merely reused).

The pass is tag-driven, so it works even when ``state/<tag>.json`` is
missing. The third argument (``ssh_key``) is accepted but unused; the
spec defines the CLI as ``cleanup <openrc> <tag> <ssh_key>`` and we
accept it for symmetry with install/operate.
"""

from __future__ import annotations

import sys
import time

import nso
from nso import log


def usage() -> None:
    sys.exit("usage: cleanup <openrc> <tag> <ssh_key>")


def main() -> None:
    if len(sys.argv) not in (3, 4):
        usage()
    openrc, tag = sys.argv[1], sys.argv[2]
    log(f"Cleaning up {tag} using {openrc}")

    nso.source_openrc(openrc)
    conn = nso.connect()

    # ---- servers -------------------------------------------------------------
    servers = nso.list_tagged_servers(conn, tag)
    log(f"We have {len(servers)} nodes releasing them")
    for s in servers:
        log(f"Releasing {s.name}")
        conn.compute.delete_server(s, ignore_missing=True)

    log("Waiting for nodes to disapear.......")
    deadline = time.time() + 180
    while time.time() < deadline:
        leftover = nso.list_tagged_servers(conn, tag)
        if not leftover:
            break
        time.sleep(5)
    log("Nodes are gone.")

    # ---- floating IPs we allocated ------------------------------------------
    state = nso.Deployment.load(tag)
    targets = set(state.floating_ips_allocated or [])
    for fip in conn.network.ips():
        if tag in (fip.tags or []) and fip.floating_ip_address not in (
            state.floating_ips_reused or []
        ):
            targets.add(fip.floating_ip_address)
    for addr in targets:
        fip = conn.network.find_ip(addr)
        if fip:
            log(f"Releasing floating IP {addr}")
            try:
                if fip.port_id:
                    conn.network.update_ip(fip, port_id=None)
                conn.network.delete_ip(fip)
            except Exception as e:
                log(f"  could not delete {addr}: {e}")

    # IPs we only reused stay in the project but lose our tag so cleanup
    # is idempotent and safe.
    for addr in (state.floating_ips_reused or []):
        fip = conn.network.find_ip(addr)
        if fip and tag in (fip.tags or []):
            try:
                conn.network.set_tags(
                    fip, [t for t in fip.tags if t != tag],
                )
            except Exception:
                pass

    # ---- security groups -----------------------------------------------------
    log("Removing Security Groups")
    for sg in conn.network.security_groups():
        if sg.name and sg.name.startswith(f"{tag}_"):
            try:
                conn.network.delete_security_group(sg, ignore_missing=True)
            except Exception as e:
                log(f"  sg {sg.name}: {e}")

    # ---- router (gateway + interfaces first) ---------------------------------
    rtr = conn.network.find_router(f"{tag}_router")
    if rtr:
        log(f"Removing {tag}_router")
        try:
            for port in conn.network.ports(device_id=rtr.id):
                for fip_info in (port.fixed_ips or []):
                    try:
                        conn.network.remove_interface_from_router(
                            rtr, subnet_id=fip_info["subnet_id"],
                        )
                    except Exception:
                        pass
            conn.network.update_router(rtr, external_gateway_info=None)
            conn.network.delete_router(rtr, ignore_missing=True)
        except Exception as e:
            log(f"  router: {e}")

    # ---- subnet --------------------------------------------------------------
    sub = conn.network.find_subnet(f"{tag}_subnet")
    if sub:
        log(f"Removing {tag}_subnet")
        try:
            conn.network.delete_subnet(sub, ignore_missing=True)
        except Exception as e:
            log(f"  subnet: {e}")

    # ---- network -------------------------------------------------------------
    net = conn.network.find_network(f"{tag}_network")
    if net:
        log(f"Removing {tag}_network")
        try:
            conn.network.delete_network(net, ignore_missing=True)
        except Exception as e:
            log(f"  network: {e}")

    # ---- keypair -------------------------------------------------------------
    kp = conn.compute.find_keypair(f"{tag}_key")
    if kp:
        log(f"Removing {tag}_key")
        conn.compute.delete_keypair(kp, ignore_missing=True)

    # ---- final report --------------------------------------------------------
    log(f"Checking for {tag} in project.")
    parts = []
    if conn.network.find_network(f"{tag}_network"):
        parts.append("(network)")
    if conn.network.find_subnet(f"{tag}_subnet"):
        parts.append("(subnet)")
    if conn.network.find_router(f"{tag}_router"):
        parts.append("(router)")
    if any(sg.name and sg.name.startswith(f"{tag}_")
           for sg in conn.network.security_groups()):
        parts.append("(security groups)")
    if conn.compute.find_keypair(f"{tag}_key"):
        parts.append("(keypairs)")
    log("".join(parts) if parts else "(none — clean)")

    try:
        state.state_path.unlink()
    except FileNotFoundError:
        pass

    log("Cleanup done.")


if __name__ == "__main__":
    main()
