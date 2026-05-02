"""Bastion-hosted node availability checker.

Reads the list of node IPs from ``nodes.yaml`` (one per line, despite the
name) and ICMP-pings each one, returning a single text response containing
one line per node. The operate loop polls this endpoint to know how many
service nodes are currently reachable from inside the deployment network.
"""

import os
import time

import flask
from ping3 import ping

basedir = os.path.abspath(os.path.dirname(__file__))
data_file = os.path.join(basedir, 'nodes.yaml')

app = flask.Flask(__name__)


@app.route('/')
def index():
    Time = time.strftime("%Y-%m-%d %H:%M:%S")
    lines = []
    with open(data_file, "r") as fh:
        for raw in fh.readlines():
            node = raw.rstrip()
            if not node:
                continue
            try:
                rtt_raw = ping(node, timeout=1, unit='ms')
                rtt = int(rtt_raw) if rtt_raw else 0
            except Exception:
                rtt = 0
            if rtt == 0:
                lines.append(f"{Time} {node} N/A")
            else:
                lines.append(f"{Time} {node} {rtt} ms")
    return "\n".join(lines) + "\n"


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5001)
