"""UNRUN Docker-only, loopback-only tests of the actual HTTPS proxy harness.

Only provider connection establishment is replaced by an owned local echo
socket. This is synthetic infrastructure validation, never a Docling/provider
observation, and it does not prove actual Docker internal-bridge connectivity.
"""

import socket
import socketserver
import threading
import time

import g0_https_proxy as actual


def main():
    checks = []
    budget = actual.Budget(deadline_epoch=time.time() + 15, max_bytes=1024,
                           max_connections=1, allowed_hosts=["pypi.org"])
    budget.acquire_connection("pypi.org")
    try:
        budget.acquire_connection("pypi.org")
    except RuntimeError:
        checks.append("actual connection cap")
    else:
        raise AssertionError("Connection cap accepted")
    left, right = socket.socketpair()
    try:
        right.sendall(b"synthetic")
        assert budget.receive(left) == b"synthetic"
        assert budget.snapshot()["transport_bytes_both_directions"] == 9
        checks.append("actual byte accounting")
    finally:
        left.close()
        right.close()
    blocked = actual.Budget(deadline_epoch=time.time() + 15, max_bytes=1,
                            max_connections=2, allowed_hosts=["pypi.org"])
    try:
        blocked.acquire_connection("private.example")
    except RuntimeError:
        checks.append("unapproved host rejected")
    else:
        raise AssertionError("Unapproved host accepted")
    expired = actual.Budget(deadline_epoch=time.time() - 1, max_bytes=10,
                            max_connections=1, allowed_hosts=["pypi.org"])
    try:
        expired.remaining()
    except RuntimeError:
        checks.append("original deadline rejected")
    else:
        raise AssertionError("Expired original deadline accepted")
    for address in ["0.0.0.0", "127.0.0.1", "8.8.8.8"]:
        try:
            actual.Proxy((address, 0), blocked)
        except RuntimeError:
            checks.append("invalid bind rejected " + address)
        else:
            raise AssertionError("Invalid proxy interface accepted")

    remote, echo = socket.socketpair()
    echo_thread = threading.Thread(target=lambda: echo.sendall(echo.recv(64)), daemon=True)
    echo_thread.start()
    original = actual.public_connection
    actual.public_connection = lambda host, timeout: remote
    server = socketserver.ThreadingTCPServer(("127.0.0.1", 0), actual.Handler)
    server.daemon_threads = True
    server.budget = actual.Budget(deadline_epoch=time.time() + 10, max_bytes=4096,
                                 max_connections=1, allowed_hosts=["pypi.org"])
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with socket.create_connection(server.server_address, timeout=2) as client:
            client.settimeout(2)
            client.sendall(b"CONNECT pypi.org:443 HTTP/1.1\r\nHost: pypi.org:443\r\n\r\n")
            response = b""
            while not response.endswith(b"\r\n\r\n"):
                response += client.recv(1)
            assert response == b"HTTP/1.1 200 Connection Established\r\n\r\n"
            client.sendall(b"fixture")
            assert client.recv(7) == b"fixture"
        checks.append("actual CONNECT parser and bidirectional synthetic tunnel")
        with socket.create_connection(server.server_address, timeout=2) as client:
            client.settimeout(2)
            client.sendall(b"CONNECT g0-reachability.invalid:443 HTTP/1.1\r\n\r\n")
            # This server's single connection allowance is already used; the
            # repeated/capped probe must stop the budget, never grant a new one.
            assert client.recv(128) == b""
        assert server.budget.stopped.wait(1)
        assert server.budget.failure.startswith("PolicyViolation:")
        checks.append("actual capped reachability probe fails policy budget")
    finally:
        server.shutdown()
        server.server_close()
        remote.close()
        echo.close()
        actual.public_connection = original
    assert echo_thread.is_alive() is False
    import json
    print(json.dumps({"kind": "SYNTHETIC_HARNESS_SELF_CHECK_ONLY", "status": "PASS",
                      "checks": checks, "external_network_requests": 0, "project_imports": False,
                      "actual_docling_observations": False, "candidate_code": False}))


if __name__ == "__main__":
    main()
