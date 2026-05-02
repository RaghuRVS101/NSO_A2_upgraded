"""Shared helpers for install / operate / cleanup.

Everything that talks to OpenStack lives here so the three top-level
programs stay short and focused on orchestration. Every cloud resource we
create is named ``<tag>_<role>`` and tagged with ``<tag>`` so that
``cleanup`` can wipe a deployment purely from the tag, even if the local
state file is missing.

Cloud-specific knobs (external network name, default flavor, image name
pattern, subnet, ports) are configurable via environment variables so the
same code runs against different OpenStack flavours without code edits.

Tested against openstacksdk >= 2.1.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import sys
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Iterable

import openstack
from openstack.connection import Connection


# --------------------------------------------------------------------------- #
# Tunables (overridable via environment variables)
# --------------------------------------------------------------------------- #

EXTERNAL_NETWORK = os.environ.get("NSO_EXTERNAL_NETWORK", "ext-net")
IMAGE_REGEX = os.environ.get("NSO_IMAGE_REGEX", r"Ubuntu.*(20\.04|22\.04)")
FLAVOR = os.environ.get("NSO_FLAVOR", "m1.small")
SUBNET_CIDR = os.environ.get("NSO_SUBNET_CIDR", "10.10.20.0/24")
DNS_SERVERS = [s for s in os.environ.get("NSO_DNS", "8.8.8.8,1.1.1.1").split(",") if s]
SERVICE_PORT = int(os.environ.get("NSO_SERVICE_PORT", "5000"))
SNMP_PORT = int(os.environ.get("NSO_SNMP_PORT", "6000"))
ALIVE_PORT = int(os.environ.get("NSO_ALIVE_PORT", "5001"))

HERE = Path(__file__).resolve().parent
STATE_DIR = HERE / "state"
STATE_DIR.mkdir(exist_ok=True)


# --------------------------------------------------------------------------- #
# Logging — mirrors the format shown in the assignment example output.
# --------------------------------------------------------------------------- #

def log(msg: str) -> None:
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    print(f"{ts} {msg}", flush=True)


# --------------------------------------------------------------------------- #
# openrc handling
# --------------------------------------------------------------------------- #

def _looks_like_clouds_yaml(path: Path) -> bool:
    """Detect whether ``path`` is an OpenStack clouds.yaml (vs an openrc
    shell script). We trust the filename first and the content second."""
    name = path.name.lower()
    if name == "clouds.yaml" or name == "clouds.yml":
        return True
    if name.endswith(".yaml") or name.endswith(".yml"):
        return True
    try:
        head = path.read_text(errors="ignore")[:4096]
    except Exception:
        return False
    return "clouds:" in head and "auth:" in head


def _load_clouds_yaml(path: Path) -> None:
    """Point openstacksdk at the supplied clouds.yaml and pick the cloud.

    We honour an existing ``OS_CLOUD`` if the user has set one, otherwise
    we use the first ``clouds:`` entry in the file.
    """
    import yaml

    data = yaml.safe_load(path.read_text()) or {}
    clouds = (data.get("clouds") or {})
    if not clouds:
        sys.exit(f"{path}: no 'clouds:' entries found")

    cloud_name = os.environ.get("OS_CLOUD") or next(iter(clouds))
    if cloud_name not in clouds:
        sys.exit(f"{path}: cloud '{cloud_name}' not found "
                 f"(available: {', '.join(clouds)})")

    os.environ["OS_CLIENT_CONFIG_FILE"] = str(path)
    os.environ["OS_CLOUD"] = cloud_name

    # Surface the OS_AUTH_URL / OS_PROJECT_NAME for any subprocess (Ansible
    # callbacks etc.) that might want them.
    auth = (clouds[cloud_name].get("auth") or {})
    for k_yaml, k_env in [
        ("auth_url", "OS_AUTH_URL"),
        ("username", "OS_USERNAME"),
        ("password", "OS_PASSWORD"),
        ("project_name", "OS_PROJECT_NAME"),
        ("project_id", "OS_PROJECT_ID"),
        ("user_domain_name", "OS_USER_DOMAIN_NAME"),
        ("project_domain_name", "OS_PROJECT_DOMAIN_NAME"),
    ]:
        if auth.get(k_yaml) and not os.environ.get(k_env):
            os.environ[k_env] = str(auth[k_yaml])
    if clouds[cloud_name].get("region_name") and not os.environ.get("OS_REGION_NAME"):
        os.environ["OS_REGION_NAME"] = str(clouds[cloud_name]["region_name"])
    if clouds[cloud_name].get("identity_api_version") and not os.environ.get("OS_IDENTITY_API_VERSION"):
        os.environ["OS_IDENTITY_API_VERSION"] = str(clouds[cloud_name]["identity_api_version"])


def source_openrc(path: str) -> None:
    """Load OpenStack credentials from either an ``openrc`` shell file or a
    ``clouds.yaml``. The format is auto-detected from the filename / content.

    For openrc: Horizon-generated files prompt for the password with
    ``read -p`` when ``OS_PASSWORD`` is unset. We export ``OS_PASSWORD``
    first so this runs non-interactively in CI as long as the variable is
    in the environment when ``install`` is invoked.

    For clouds.yaml: we set ``OS_CLIENT_CONFIG_FILE`` and ``OS_CLOUD`` so
    that ``openstack.connect()`` finds the right cloud entry.
    """
    rc = Path(path).expanduser().resolve()
    if not rc.is_file():
        sys.exit(f"credentials file not found: {rc}")

    if _looks_like_clouds_yaml(rc):
        _load_clouds_yaml(rc)
        return

    bash = (
        "set -a && "
        "export OS_PASSWORD=\"${OS_PASSWORD:-}\" && "
        f"source {shlex.quote(str(rc))} >/dev/null 2>&1 && env"
    )
    out = subprocess.check_output(["bash", "-lc", bash], text=True)
    for line in out.splitlines():
        if "=" not in line:
            continue
        k, v = line.split("=", 1)
        if k.startswith("OS_"):
            os.environ[k] = v


def connect() -> Connection:
    """Open an authenticated OpenStack connection.

    Honours ``OS_CLOUD`` (clouds.yaml mode) when set, otherwise falls back
    to the standard ``OS_*`` environment variables (openrc mode).
    """
    cloud = os.environ.get("OS_CLOUD")
    if cloud:
        return openstack.connect(cloud=cloud)
    return openstack.connect()


# --------------------------------------------------------------------------- #
# State file (helps operate.py; cleanup never depends on it)
# --------------------------------------------------------------------------- #

@dataclass
class Deployment:
    tag: str
    ssh_key_path: str
    bastion_ip: str = ""
    proxy_ip: str = ""
    network_id: str = ""
    subnet_id: str = ""
    router_id: str = ""
    keypair_name: str = ""
    security_group_id: str = ""
    image_id: str = ""
    flavor_id: str = ""
    node_names: list[str] = field(default_factory=list)
    floating_ips_allocated: list[str] = field(default_factory=list)
    floating_ips_reused: list[str] = field(default_factory=list)

    @property
    def state_path(self) -> Path:
        return STATE_DIR / f"{self.tag}.json"

    def save(self) -> None:
        self.state_path.write_text(json.dumps(asdict(self), indent=2))

    @classmethod
    def load(cls, tag: str) -> "Deployment":
        p = STATE_DIR / f"{tag}.json"
        if not p.is_file():
            return cls(tag=tag, ssh_key_path="")
        return cls(**json.loads(p.read_text()))


# --------------------------------------------------------------------------- #
# Floating IPs (reuse before allocate — IPs are a costly resource)
# --------------------------------------------------------------------------- #

def reuse_or_allocate_floating_ip(conn: Connection, tag: str,
                                  state: Deployment,
                                  exclude: set[str] | None = None) -> str:
    """Return a floating IP, preferring an unattached one already in the
    project. Whichever path we take, the IP is tagged with ``tag`` so that
    cleanup can safely remove only the ones we explicitly allocated.
    """
    exclude = exclude or set()
    ext_net = conn.network.find_network(EXTERNAL_NETWORK)
    if ext_net is None:
        sys.exit(f"external network '{EXTERNAL_NETWORK}' not found")

    for fip in conn.network.ips():
        if fip.floating_ip_address in exclude:
            continue
        if fip.port_id is None and fip.floating_network_id == ext_net.id:
            log(f"Reusing existing floating IP {fip.floating_ip_address}.")
            try:
                conn.network.set_tags(fip, list(set(fip.tags or []) | {tag}))
            except Exception:
                pass
            state.floating_ips_reused.append(fip.floating_ip_address)
            return fip.floating_ip_address

    fip = conn.network.create_ip(floating_network_id=ext_net.id)
    log(f"Allocating floating IP {fip.floating_ip_address}. Done.")
    try:
        conn.network.set_tags(fip, [tag])
    except Exception:
        pass
    state.floating_ips_allocated.append(fip.floating_ip_address)
    return fip.floating_ip_address


# --------------------------------------------------------------------------- #
# Keypair
# --------------------------------------------------------------------------- #

def get_or_create_keypair(conn: Connection, tag: str, ssh_key_path: str) -> str:
    name = f"{tag}_key"
    pub = Path(ssh_key_path).expanduser().resolve()
    if pub.suffix != ".pub":
        candidate = (pub.with_suffix(pub.suffix + ".pub")
                     if pub.suffix else Path(str(pub) + ".pub"))
        if candidate.is_file():
            pub = candidate
    if not pub.is_file():
        sys.exit(f"public key not found at {pub}")

    if conn.compute.find_keypair(name):
        log(f"Checking if we have {name} availible. Yes.")
        return name

    log(f"Adding {name} associated with {Path(ssh_key_path).name}.")
    conn.compute.create_keypair(name=name, public_key=pub.read_text().strip())
    return name


# --------------------------------------------------------------------------- #
# Network / subnet / router
# --------------------------------------------------------------------------- #

def get_or_create_network(conn: Connection, tag: str) -> tuple[str, str, str]:
    """Return ``(network_id, subnet_id, router_id)``, creating any pieces
    that are missing. Idempotent so partial deployments can be resumed.
    """
    net_name = f"{tag}_network"
    sub_name = f"{tag}_subnet"
    rtr_name = f"{tag}_router"

    net = conn.network.find_network(net_name)
    if net is None:
        log(f"Did not detect {net_name} in the OpenStack project, adding it.")
        net = conn.network.create_network(name=net_name)
        try:
            conn.network.set_tags(net, [tag])
        except Exception:
            pass
        log(f"Added {net_name}.")
    else:
        log(f"{net_name} already present.")

    sub = conn.network.find_subnet(sub_name)
    if sub is None:
        log(f"Did not detect {sub_name} in the OpenStack project, adding it.")
        sub = conn.network.create_subnet(
            name=sub_name,
            network_id=net.id,
            ip_version=4,
            cidr=SUBNET_CIDR,
            dns_nameservers=DNS_SERVERS,
        )
        try:
            conn.network.set_tags(sub, [tag])
        except Exception:
            pass
        log(f"Added {sub_name}.")
    else:
        log(f"{sub_name} already present.")

    rtr = conn.network.find_router(rtr_name)
    if rtr is None:
        log(f"Did not detect {rtr_name} in the OpenStack project, adding it.")
        ext = conn.network.find_network(EXTERNAL_NETWORK)
        rtr = conn.network.create_router(
            name=rtr_name,
            external_gateway_info={"network_id": ext.id},
        )
        try:
            conn.network.set_tags(rtr, [tag])
        except Exception:
            pass
        log(f"Added {rtr_name}.")
        log("Adding networks to router.")
        conn.network.add_interface_to_router(rtr, subnet_id=sub.id)
        log("Done.")
    else:
        log(f"{rtr_name} already present.")

    return net.id, sub.id, rtr.id


# --------------------------------------------------------------------------- #
# Security group
# --------------------------------------------------------------------------- #

def get_or_create_security_group(conn: Connection, tag: str) -> str:
    name = f"{tag}_sg"
    sg = conn.network.find_security_group(name)
    if sg:
        log(f"Security group '{name}' already present.")
        return sg.id

    log("Adding security group(s).")
    sg = conn.network.create_security_group(
        name=name, description=f"NSO deployment {tag}",
    )
    try:
        conn.network.set_tags(sg, [tag])
    except Exception:
        pass

    rules = [
        ("ingress", "tcp", 22, 22, "0.0.0.0/0"),
        ("ingress", "tcp", SERVICE_PORT, SERVICE_PORT, "0.0.0.0/0"),
        ("ingress", "udp", SNMP_PORT, SNMP_PORT, "0.0.0.0/0"),
        ("ingress", "tcp", ALIVE_PORT, ALIVE_PORT, "0.0.0.0/0"),
        ("ingress", "icmp", None, None, "0.0.0.0/0"),
    ]
    for direction, proto, lo, hi, cidr in rules:
        try:
            conn.network.create_security_group_rule(
                security_group_id=sg.id,
                direction=direction,
                ethertype="IPv4",
                protocol=proto,
                port_range_min=lo,
                port_range_max=hi,
                remote_ip_prefix=cidr,
            )
        except openstack.exceptions.ConflictException:
            pass

    return sg.id


# --------------------------------------------------------------------------- #
# Image / flavor selection
# --------------------------------------------------------------------------- #

def pick_image(conn: Connection) -> tuple[str, str]:
    pat = re.compile(IMAGE_REGEX, re.IGNORECASE)
    matches = [im for im in conn.compute.images() if pat.search(im.name or "")]
    if not matches:
        sys.exit(f"No image matched /{IMAGE_REGEX}/ in this project.")
    log(f"Detecting suitable image, looking for {IMAGE_REGEX}; "
        + ", ".join(im.name for im in matches))
    matches.sort(key=lambda im: getattr(im, "updated_at", "") or "", reverse=True)
    chosen = matches[0]
    log(f"Selected: {chosen.name}")
    return chosen.id, chosen.name


def pick_flavor(conn: Connection) -> str:
    f = conn.compute.find_flavor(FLAVOR)
    if f is None:
        sys.exit(f"flavor '{FLAVOR}' not found")
    return f.id


# --------------------------------------------------------------------------- #
# Servers
# --------------------------------------------------------------------------- #

def boot_server(conn: Connection, name: str, image_id: str, flavor_id: str,
                network_id: str, keypair_name: str, sg_name: str, tag: str):
    """Create one server and wait until it is ACTIVE. Tag it for cleanup."""
    server = conn.compute.create_server(
        name=name,
        image_id=image_id,
        flavor_id=flavor_id,
        networks=[{"uuid": network_id}],
        key_name=keypair_name,
        security_groups=[{"name": sg_name}],
    )
    server = conn.compute.wait_for_server(server, status="ACTIVE", wait=300)
    try:
        conn.compute.set_server_metadata(server, nso_tag=tag)
    except Exception:
        pass
    return server


def attach_floating_ip(conn: Connection, server, fip_addr: str) -> None:
    """Attach a floating IP to the first non-floating port on the server.

    The compute proxy method ``add_floating_ip_to_server`` was removed in
    newer SDK releases, so we use the network-side API directly.
    """
    try:
        conn.compute.add_floating_ip_to_server(server, fip_addr)
        return
    except Exception:
        pass

    fip = conn.network.find_ip(fip_addr)
    if fip is None:
        raise RuntimeError(f"floating IP {fip_addr} not found")
    for port in conn.network.ports(device_id=server.id):
        if port.fixed_ips:
            conn.network.update_ip(fip, port_id=port.id)
            return
    raise RuntimeError(f"no fixed-IP port found on server {server.name}")


def list_tagged_servers(conn: Connection, tag: str, role: str | None = None):
    """Servers we recognise as ours: name starts with ``<tag>_`` and (when
    ``role`` is given) contains ``_<role>``."""
    out = []
    for s in conn.compute.servers():
        if not s.name or not s.name.startswith(f"{tag}_"):
            continue
        if role is None or f"_{role}" in s.name:
            out.append(s)
    return out


# --------------------------------------------------------------------------- #
# IP discovery on a server
# --------------------------------------------------------------------------- #

def get_internal_ip(server) -> str:
    """First fixed (non-floating) address on the server."""
    for _, addrs in (server.addresses or {}).items():
        for a in addrs:
            if a.get("OS-EXT-IPS:type") == "fixed":
                return a["addr"]
    for _, addrs in (server.addresses or {}).items():
        if addrs:
            return addrs[0]["addr"]
    return ""


def get_floating_ip(server) -> str:
    for _, addrs in (server.addresses or {}).items():
        for a in addrs:
            if a.get("OS-EXT-IPS:type") == "floating":
                return a["addr"]
    return ""


# --------------------------------------------------------------------------- #
# SSH config + Ansible inventory generation
# --------------------------------------------------------------------------- #

def write_ssh_config(tag: str, ssh_key_path: str, bastion_ip: str,
                     hosts: dict[str, str]) -> Path:
    """Write a per-deployment SSH config that uses the bastion as a jump host
    for every internal node. ``hosts`` maps logical hostname -> internal IP
    (excluding the bastion itself)."""
    p = HERE / f"{tag}_SSHconfig"
    lines = [
        "Host *",
        "    StrictHostKeyChecking no",
        "    UserKnownHostsFile /dev/null",
        "    LogLevel ERROR",
        "    ServerAliveInterval 30",
        "",
        f"Host {tag}_bastion bastion",
        f"    HostName {bastion_ip}",
        "    User ubuntu",
        f"    IdentityFile {ssh_key_path}",
        "",
    ]
    for name, ip in hosts.items():
        lines += [
            f"Host {name}",
            f"    HostName {ip}",
            "    User ubuntu",
            f"    IdentityFile {ssh_key_path}",
            f"    ProxyJump {tag}_bastion",
            "",
        ]
    p.write_text("\n".join(lines))
    log(f"Building base SSH config file, saved to {p.name} (current folder)")
    return p


def write_inventory(tag: str, bastion_ip: str, proxy_ip: str,
                    proxy_internal: str, node_map: dict[str, str]) -> Path:
    """Generate an Ansible inventory targeting bastion / proxy / nodes."""
    p = HERE / f"{tag}_inventory.ini"
    lines = [
        "[bastion]",
        f"{tag}_bastion ansible_host={bastion_ip}",
        "",
        "[proxy]",
        f"{tag}_proxy ansible_host={proxy_ip}",
        "",
        "[nodes]",
    ]
    for name, ip in node_map.items():
        lines.append(f"{name} ansible_host={ip}")
    lines += [
        "",
        "[all:vars]",
        "ansible_user=ubuntu",
        "ansible_python_interpreter=/usr/bin/python3",
        f"deploy_tag={tag}",
        f"service_port={SERVICE_PORT}",
        f"snmp_port={SNMP_PORT}",
        f"alive_port={ALIVE_PORT}",
        f"proxy_internal={proxy_internal}",
    ]
    p.write_text("\n".join(lines) + "\n")
    return p


def run_ansible(tag: str, ssh_config: Path, inventory: Path,
                playbook: str = "playbook.yaml") -> None:
    log("Running playbook.")
    env = os.environ.copy()
    env["ANSIBLE_HOST_KEY_CHECKING"] = "False"
    env["OBJC_DISABLE_INITIALIZE_FORK_SAFETY"] = "YES"

    # Prefer the ansible-playbook that lives next to our own python interpreter
    # (i.e. inside the venv) so this works even when the venv is not activated.
    venv_ap = Path(sys.executable).parent / "ansible-playbook"
    ap_bin = str(venv_ap) if venv_ap.is_file() else "ansible-playbook"

    cmd = [
        ap_bin,
        "--ssh-common-args", f"-F {ssh_config}",
        "-i", str(inventory),
        str(HERE / playbook),
    ]
    subprocess.check_call(cmd, env=env, cwd=str(HERE))
    log("Done, solution has been deployed.")


# --------------------------------------------------------------------------- #
# Misc convenience
# --------------------------------------------------------------------------- #

def read_required_node_count() -> int:
    """Read the desired node count from ``servers.conf``. Defaults to 3 if
    the file is unreadable for any reason."""
    p = HERE / "servers.conf"
    try:
        return max(1, int(p.read_text().strip()))
    except Exception:
        return 3


def next_node_index(existing: Iterable[str], tag: str) -> int:
    """Return the smallest positive integer not yet used as ``<tag>_node_<n>``."""
    used = set()
    for n in existing:
        m = re.match(rf"^{re.escape(tag)}_node_(\d+)$", n or "")
        if m:
            used.add(int(m.group(1)))
    i = 1
    while i in used:
        i += 1
    return i
