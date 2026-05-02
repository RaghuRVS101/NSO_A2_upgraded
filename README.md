# NSO Project — single-student version (Grade E)

Three-stage automation that deploys, operates and tears down a small
Flask-based service inside an OpenStack project. Built to match the
assignment specification:

| Stage    | Command                                  | What it does                                                         |
|----------|------------------------------------------|----------------------------------------------------------------------|
| Deploy   | `./install <openrc> <tag> <ssh_key>`     | Provision the network, security group, bastion, proxy and N nodes.   |
| Operate  | `./operate <openrc> <tag> <ssh_key>`     | Loop until Ctrl-C; reconcile to `servers.conf` every 30 seconds.     |
| Teardown | `./cleanup <openrc> <tag> <ssh_key>`     | Release every cloud resource carrying the tag.                       |

Every cloud resource is created with the name `<tag>_<role>` and tagged
with `<tag>`, so `cleanup` works purely from the tag — even when the
local state file is missing.

## Architecture

```
                         (Apache Benchmark / curl / browser)
                                       |
                                       v
                  +-----------------------------------------+
                  |                PROXY VM                  |
                  |  HAProxy   :TCP {SERVICE_PORT=5000}      |
                  |  NGINX-stream :UDP {SNMP_PORT=6000}      |
                  +-----------------------------------------+
                                       |
              +--------------------+---+---+--------------------+
              v                    v       v                    v
        +-----------+        +-----------+ ...           +-----------+
        |  node_1   |        |  node_2   |               |  node_N   |
        |service.py |        |service.py |               |service.py |
        |   snmpd   |        |   snmpd   |               |   snmpd   |
        +-----------+        +-----------+               +-----------+
              ^                    ^                            ^
              +--------------------+----------------------------+
                                   |  ICMP from BASTION
                                   |
                  +----------------+----------------+
                  |             BASTION             |
                  |  alive.py :TCP {ALIVE_PORT=5001}|
                  |  ssh ProxyJump for all nodes    |
                  +---------------------------------+
                                   ^
                                   |
                              You / operate.py
```

## Requirements

* Python 3.10+
* `ansible-playbook` available in `PATH`
* OpenStack credentials in **either** format:
  * a classic `openrc` shell file (Horizon → *OpenStack RC File v3*), or
  * a `clouds.yaml` (Horizon → *OpenStack clouds.yaml File*) — saved
    anywhere; if you keep the password out of the YAML, set
    `OS_PASSWORD` in the env before invoking `install`. The path you
    pass as `<openrc>` is auto-detected.
* An SSH **public** key on disk (path passed as `<ssh_key>` — the
  matching `.pub` file is used for keypair upload; the private key is
  what Ansible uses to log in via the bastion)

Install Python deps into a local virtualenv:

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

The wrapper scripts (`install`, `operate`, `cleanup`) automatically
activate `./venv/` if it exists, so you don't have to.

## Configuration

Most cloud-specific knobs are environment variables with sensible
defaults. Override only what you need to:

| Variable                | Default                       | Meaning                            |
|-------------------------|-------------------------------|------------------------------------|
| `NSO_EXTERNAL_NETWORK`  | `ext-net`                     | Public network to allocate FIPs on |
| `NSO_IMAGE_REGEX`       | `Ubuntu.*(20\.04\|22\.04)`    | Regex matched against image names  |
| `NSO_FLAVOR`            | `m1.small`                    | Flavor for every VM                |
| `NSO_SUBNET_CIDR`       | `10.10.20.0/24`               | Internal subnet                    |
| `NSO_DNS`               | `8.8.8.8,1.1.1.1`             | Comma-separated DNS resolvers      |
| `NSO_SERVICE_PORT`      | `5000`                        | service.py + HAProxy frontend port |
| `NSO_SNMP_PORT`         | `6000`                        | snmpd + NGINX UDP frontend port    |
| `NSO_ALIVE_PORT`        | `5001`                        | Bastion alive.py port              |

`servers.conf` (a single integer, default `3`) controls how many
service nodes the operate loop tries to maintain.

## Usage

```bash
# With an openrc shell file:
./install ~/myRC rev1 ~/.ssh/id_ed25519
./operate ~/myRC rev1 ~/.ssh/id_ed25519     # Ctrl-C to stop
./cleanup ~/myRC rev1 ~/.ssh/id_ed25519

# Or with a clouds.yaml (BTH lab default):
export OS_PASSWORD='your-horizon-password'   # only if password is not in clouds.yaml
./install ./clouds.yaml raghu1 ~/.ssh/nso_id
./operate ./clouds.yaml raghu1 ~/.ssh/nso_id
./cleanup ./clouds.yaml raghu1 ~/.ssh/nso_id
```

While `operate` is running, edit `servers.conf` to scale up or down —
the next cycle (within 30 s) will converge to the new count.

### Sending traffic

Once `install` is done, the proxy's public IP is logged. You can hit
it manually or via Apache Benchmark:

```bash
curl http://<proxy_ip>:5000/
ab -n 1000 -c 10 http://<proxy_ip>:5000/
snmpget -v2c -c public <proxy_ip>:6000 1.3.6.1.2.1.1.1.0
```

## File layout

```
.
├── install / operate / cleanup        # bash wrappers (just exec the .py)
├── install.py / operate.py / cleanup.py
├── nso.py                             # all OpenStack helpers
├── service.py                         # Flask backend (deployed to nodes)
├── alive.py                           # Flask availability checker (deployed to bastion)
├── nodes.yaml                         # placeholder; rendered on bastion
├── servers.conf                       # desired node count (single integer)
├── playbook.yaml                      # top-level Ansible play
├── roles/
│   ├── service_node/                  # service.py + snmpd
│   ├── bastion_node/                  # alive.py + ICMP utilities
│   └── proxy_node/                    # HAProxy (TCP) + NGINX stream (UDP)
├── state/                             # generated: <tag>.json
├── report/                            # IEEE-conference-template report
├── requirements.txt
└── README.md
```

Generated at runtime (one per `<tag>`):

```
<tag>_SSHconfig         # SSH config with bastion as ProxyJump
<tag>_inventory.ini     # Ansible inventory
state/<tag>.json        # Cached deployment metadata
```

## Reconciliation logic (`operate`)

Every 30 s:

1. Read desired count from `servers.conf`.
2. `GET http://<bastion>:5001/` once — the bastion reports which node
   IPs respond to ICMP. (One request answers for the whole fleet.)
3. Compare actual vs desired:
   * **Missing / unreachable nodes:** delete them and boot replacements
     with the next free `<tag>_node_<n>` index.
   * **Surplus nodes:** delete the highest-numbered ones first.
4. If anything changed, regenerate `<tag>_inventory.ini`, `nodes.yaml`
   on the bastion and `<tag>_SSHconfig`, then re-run the playbook so
   HAProxy/NGINX upstream lists match reality. Send a few smoke-test
   requests through the proxy to confirm the new pool serves traffic.

## Cleanup ordering

`cleanup` removes resources in the safe order:

1. servers (bastion, proxy, all `<tag>_node_*`)
2. floating IPs we allocated (re-used IPs are left behind)
3. security groups starting with `<tag>_`
4. router (gateway + interfaces detached first)
5. subnet
6. network
7. keypair
8. local `state/<tag>.json`
