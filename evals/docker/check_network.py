#!/usr/bin/env python3
"""Free containment probes; never send model credentials."""

import os
from pathlib import Path
import shutil
import socket
import ssl


def tunnel(host):
    with socket.create_connection(("egress", 3128), 10) as connection:
        connection.sendall(f"CONNECT {host}:443 HTTP/1.1\r\nHost: {host}:443\r\n\r\n".encode("ascii"))
        response = connection.recv(4096).split(b"\r\n", 1)[0]
        if host in {"api.github.com", "api.githubcopilot.com"}:
            assert b" 200 " in response, response
            with ssl.create_default_context().wrap_socket(connection, server_hostname=host) as tls:
                tls.sendall(f"HEAD / HTTP/1.1\r\nHost: {host}\r\nUser-Agent: actualize-evals-doctor\r\nConnection: close\r\n\r\n".encode("ascii"))
                assert b"HTTP/" in tls.recv(4096)
        else:
            assert b" 403 " in response, (host, response)


assert os.getuid() != 0
assert not shutil.which("az")
assert not Path("/var/run/docker.sock").exists()
for host in ("api.github.com", "api.githubcopilot.com", "management.azure.com", "prices.azure.com", "example.com", "127.0.0.1"):
    tunnel(host)
try:
    socket.getaddrinfo("example.com", 443)
except socket.gaierror:
    pass
else:
    raise AssertionError("External DNS is reachable directly from the task network")
try:
    connection = socket.create_connection(("1.1.1.1", 443), 2)
except OSError:
    pass
else:
    connection.close()
    raise AssertionError("The task can bypass the allowlisted proxy")
print("Containment passed: non-root; Copilot/auth endpoints reachable; Azure, arbitrary egress and external DNS blocked.")
