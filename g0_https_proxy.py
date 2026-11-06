"""PRIVATE, UNRUN HTTPS-only acquisition proxy for isolated hosted Docker.

No TLS interception. The package client validates the provider certificate.
Bind only to the gateway of this job's internal Docker network, never 0.0.0.0.
The container has no external route: its only acquisition path is this proxy.
"""

import ipaddress
import select
import socket
import socketserver
import threading
import time


class PolicyViolation(RuntimeError):
    """Scope, byte, connection or original-deadline violation."""


class Budget:
    def __init__(self, *, deadline_epoch, max_bytes, max_connections, allowed_hosts):
        self.deadline_epoch = deadline_epoch
        self.deadline_monotonic = time.monotonic() + max(0, deadline_epoch - time.time())
        self.max_bytes = max_bytes
        self.max_connections = max_connections
        self.allowed_hosts = frozenset(allowed_hosts)
        self.used = 0
        self.reserved = 0
        self.connections = 0
        self.hosts = set()
        self.failure = None
        self.connection_errors = []
        self.reachability_probes = 0
        self.lock = threading.Lock()
        self.stopped = threading.Event()

    def remaining(self):
        remaining = min(self.deadline_epoch - time.time(), self.deadline_monotonic - time.monotonic())
        if remaining <= 0 or self.stopped.is_set():
            raise PolicyViolation("Original G0 acquisition deadline or terminal stop")
        return remaining

    def fail(self, reason):
        with self.lock:
            self.failure = self.failure or reason
        self.stopped.set()

    def acquire_connection(self, host):
        self.remaining()
        with self.lock:
            if host not in self.allowed_hosts or self.connections >= self.max_connections:
                raise PolicyViolation("Unapproved acquisition host or connection cap")
            self.connections += 1
            self.hosts.add(host)

    def admit_probe(self):
        self.remaining()
        with self.lock:
            if self.reachability_probes or self.connections >= self.max_connections:
                raise PolicyViolation("Repeated reachability probe or connection cap")
            self.reachability_probes += 1
            self.connections += 1

    def receive(self, source):
        self.remaining()
        with self.lock:
            available = self.max_bytes - self.used - self.reserved
            if available <= 0:
                raise PolicyViolation("Original encrypted transport byte cap exhausted")
            reserved = min(65536, available)
            self.reserved += reserved
        data = b""
        try:
            data = source.recv(reserved)
            return data
        finally:
            with self.lock:
                self.reserved -= reserved
                self.used += len(data)

    def snapshot(self):
        with self.lock:
            return {"transport_bytes_both_directions": self.used,
                    "max_transport_bytes": self.max_bytes,
                    "connections": self.connections,
                    "max_connections": self.max_connections,
                    "hosts": sorted(self.hosts), "failure": self.failure,
                    "connection_errors": list(self.connection_errors), "reachability_probes": self.reachability_probes,
                    "tls_interception": False, "http_request_count": "NOT_OBSERVABLE_INSIDE_TLS"}


def public_connection(host, timeout):
    # Resolve once, validate the concrete address, and connect to that address;
    # do not perform a second hostname resolution after checking its scope.
    addresses = socket.getaddrinfo(host, 443, socket.AF_INET, socket.SOCK_STREAM)
    if not addresses:
        raise RuntimeError("Acquisition DNS returned no IPv4 address")
    family, kind, protocol, _, address = addresses[0]
    if not ipaddress.ip_address(address[0]).is_global:
        raise PolicyViolation("Acquisition DNS returned a nonpublic address")
    connection = socket.socket(family, kind, protocol)
    connection.settimeout(timeout)
    try:
        connection.connect(address)
    except Exception:
        connection.close()
        raise
    return connection


class Handler(socketserver.BaseRequestHandler):
    def handle(self):
        budget = self.server.budget
        upstream = None
        try:
            self.request.settimeout(min(5, budget.remaining()))
            header = b""
            while not header.endswith(b"\r\n\r\n"):
                if len(header) >= 8192:
                    raise PolicyViolation("Oversized proxy request header")
                byte = budget.receive(self.request)
                if not byte:
                    raise ConnectionError("Incomplete proxy CONNECT header")
                header += byte
                if len(header) > 8192:
                    raise PolicyViolation("Oversized proxy request header")
                # A conforming HTTPS proxy client waits for CONNECT's response
                # before sending TLS. Reject surplus bytes rather than drop any.
                if b"\r\n\r\n" in header and not header.endswith(b"\r\n\r\n"):
                    raise PolicyViolation("Unexpected bytes after CONNECT header")
            try:
                lines = header.decode("ascii").split("\r\n")
                method, target, protocol = lines[0].split(" ")
                host, port = target.rsplit(":", 1)
            except (ValueError, UnicodeError) as error:
                raise PolicyViolation("Malformed CONNECT header") from error
            if method != "CONNECT" or protocol not in ("HTTP/1.0", "HTTP/1.1"):
                raise PolicyViolation("Only HTTPS CONNECT acquisition is permitted")
            if port != "443" or host != host.lower() or any(c not in "abcdefghijklmnopqrstuvwxyz0123456789.-" for c in host):
                raise PolicyViolation("Invalid HTTPS acquisition destination")
            if any(line.lower().startswith("proxy-authorization:") for line in lines[1:]):
                raise PolicyViolation("This public-artifact proxy must not receive credentials")
            if host == "g0-reachability.invalid":
                budget.admit_probe()
                self.request.sendall(b"HTTP/1.1 403 G0-Reachability\r\nContent-Length: 0\r\n\r\n")
                return
            budget.acquire_connection(host)
            upstream = public_connection(host, min(5, budget.remaining()))
            self.request.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
            self.request.settimeout(min(2, budget.remaining()))
            upstream.settimeout(min(2, budget.remaining()))
            streams = [self.request, upstream]
            while True:
                budget.remaining()
                readable, _, _ = select.select(streams, [], [], min(0.25, budget.remaining()))
                for source in readable:
                    data = budget.receive(source)
                    if not data:
                        return
                    target = upstream if source is self.request else self.request
                    target.sendall(data)
        except PolicyViolation as error:
            budget.fail(type(error).__name__ + ": " + str(error))
        except Exception as error:
            # Empty connections, timeouts, DNS and peer resets are connection
            # failures. The package client/outer command still fails normally.
            # Retain only exception types, never tokens, signed URLs or headers.
            with budget.lock:
                if len(budget.connection_errors) < 128:
                    budget.connection_errors.append(type(error).__name__)
        finally:
            if upstream is not None:
                upstream.close()


class Proxy(socketserver.ThreadingTCPServer):
    daemon_threads = True
    allow_reuse_address = False

    def __init__(self, address, budget):
        if address[0] in ("", "0.0.0.0", "127.0.0.1"):
            raise RuntimeError("Proxy must bind the owned internal Docker bridge gateway")
        if ipaddress.ip_address(address[0]).is_global:
            raise RuntimeError("Proxy cannot bind a public interface")
        self.budget = budget
        super().__init__(address, Handler)

    def start(self):
        thread = threading.Thread(target=self.serve_forever, daemon=True)
        thread.start()
        return thread
