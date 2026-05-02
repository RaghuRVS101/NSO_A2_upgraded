#!/usr/bin/env python3
"""check_cloud.py <openrc-or-clouds.yaml>

Sanity check before running ``install``. Prints:

  * the project we authenticated into
  * the available external networks (so you can pick one for
    ``NSO_EXTERNAL_NETWORK``)
  * the available flavors (so you can pick one for ``NSO_FLAVOR``)
  * Ubuntu images that match ``NSO_IMAGE_REGEX``
  * how many floating IPs are unattached (the deployment will reuse them)

If any of these come back empty, fix it before running install — install
will fail in obvious ways otherwise.
"""

from __future__ import annotations

import re
import sys

import nso


def main() -> None:
    if len(sys.argv) != 2:
        sys.exit("usage: check_cloud.py <openrc-or-clouds.yaml>")
    nso.source_openrc(sys.argv[1])
    conn = nso.connect()

    print("== Authenticated ==")
    sess = conn.session
    auth = getattr(sess, "auth", None)
    print(f"  auth_url     : {getattr(auth, 'auth_url', '?')}")
    print(f"  project name : {getattr(auth, 'project_name', '?')}")
    print(f"  region       : {getattr(sess, 'region_name', '?')}")
    print()

    print("== External networks (pick one for NSO_EXTERNAL_NETWORK) ==")
    found_ext = False
    for net in conn.network.networks():
        if getattr(net, "is_router_external", False):
            print(f"  - {net.name}")
            found_ext = True
    if not found_ext:
        print("  (none — ask the lab admin which network is external)")
    print()

    print(f"== Flavors (pick one for NSO_FLAVOR; current default = {nso.FLAVOR}) ==")
    flavors = sorted(conn.compute.flavors(), key=lambda f: (f.vcpus or 0, f.ram or 0))
    for f in flavors[:20]:
        marker = "  *" if f.name == nso.FLAVOR else "   "
        print(f"{marker} {f.name:<20} vcpus={f.vcpus} ram={f.ram}MB disk={f.disk}GB")
    print()

    print(f"== Images matching /{nso.IMAGE_REGEX}/ ==")
    pat = re.compile(nso.IMAGE_REGEX, re.IGNORECASE)
    matches = [im for im in conn.compute.images() if pat.search(im.name or "")]
    for im in matches:
        print(f"  - {im.name}")
    if not matches:
        print("  (none — broaden NSO_IMAGE_REGEX, e.g. 'Ubuntu')")
    print()

    print("== Floating IPs (unattached can be reused by install) ==")
    free = [ip for ip in conn.network.ips() if ip.port_id is None]
    used = [ip for ip in conn.network.ips() if ip.port_id is not None]
    print(f"  free  : {len(free)}")
    print(f"  used  : {len(used)}")
    print()

    print("== Keypairs ==")
    for kp in conn.compute.keypairs():
        print(f"  - {kp.name}")
    print()

    print("All checks done. If everything above looks sane, you're ready to run ./install")


if __name__ == "__main__":
    main()
