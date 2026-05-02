"""Backend HTTP service that the proxy load-balances.

Returns a single line containing time, the client IP/port, the backend host
and IP, the hostname, and a random number so we can prove load balancing
works (every request that lands on a different backend yields a different
hostname).
"""

import flask
import socket
import time
import random

h_name = socket.gethostname()
IP_addres = socket.gethostbyname(h_name)
app = flask.Flask(__name__)


@app.route('/')
def index():
    host = IP_addres
    client_ip = flask.request.remote_addr
    client_port = str(flask.request.environ.get('REMOTE_PORT'))
    hostname = h_name
    Time = time.strftime("%H:%M:%S")
    rand = str(random.randint(0, 100))
    return Time + " " + client_ip + ":" + client_port + " -- " + host + " (" + hostname + ") " + rand + "\n"


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
