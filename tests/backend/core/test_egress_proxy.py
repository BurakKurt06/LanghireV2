"""Real local SOCKS requests and deterministic rebinding answers; no public network."""
import socket
import socketserver
import threading
from contextlib import contextmanager
from urllib.parse import urlsplit

import pytest

from core import egress_proxy


@contextmanager
def proxy_client(block_mail=True, allowed=()):
    url = urlsplit(egress_proxy.proxy_url(block_mail, allowed))
    with socket.create_connection((url.hostname, url.port), timeout=3) as stream:
        stream.sendall(b"\x05\x01\x00")
        assert stream.recv(2) == b"\x05\x00"
        yield stream


def command(stream, host, port, operation=1):
    encoded = host.encode("ascii")
    stream.sendall(bytes([5, operation, 0, 3, len(encoded)]) + encoded + port.to_bytes(2, "big"))
    reply = bytearray()
    while len(reply) < 10:
        part = stream.recv(10 - len(reply))
        assert part
        reply.extend(part)
    return reply[1]


@pytest.mark.parametrize("host", ["127.0.0.1", "localhost", "localhost.", "169.254.169.254", "::1"])
def test_private_destinations_are_denied(host):
    with proxy_client() as stream:
        assert command(stream, host, 80) != 0


def test_gmail_is_denied_without_resolving_it(monkeypatch):
    resolve = socket.getaddrinfo
    lookups = []
    def no_dns(host, *args, **kwargs):
        lookups.append(host)
        assert host == "127.0.0.1", "Gmail must be rejected before DNS"
        return resolve(host, *args, **kwargs)
    monkeypatch.setattr(egress_proxy.socket, "getaddrinfo", no_dns)
    with proxy_client(allowed=("mail.google.com",)) as stream:
        assert command(stream, "mail.google.com.", 443) != 0
    assert lookups == ["127.0.0.1"]


def test_udp_associate_is_denied():
    with proxy_client(allowed=("127.0.0.1",)) as stream:
        assert command(stream, "127.0.0.1", 443, operation=3) != 0


def test_explicit_local_allowance_forwards_bytes():
    class Echo(socketserver.BaseRequestHandler):
        def handle(self):
            self.request.sendall(self.request.recv(100))
    server = socketserver.ThreadingTCPServer(("127.0.0.1", 0), Echo)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with proxy_client(allowed=("localhost",)) as stream:
            assert command(stream, "localhost", server.server_address[1]) == 0
            stream.sendall(b"dummy payload")
            assert stream.recv(100) == b"dummy payload"
    finally:
        server.shutdown()
        server.server_close()


@pytest.mark.parametrize("answer", ["127.0.0.1", "::ffff:127.0.0.1", "fd00::1", "169.254.169.254"])
def test_any_private_dns_answer_prevents_connecting(monkeypatch, answer):
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: [
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443)),
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", (answer, 443))])
    with pytest.raises(Exception, match="not a public"):
        egress_proxy._address_for("rebind.example", 443, True, ())


def test_connect_never_resolves_the_checked_name_again(monkeypatch):
    lookups, connections = [], []
    def resolve(host, port, **kwargs):
        lookups.append(host)
        address = "93.184.216.34" if len(lookups) == 1 else "127.0.0.1"
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, port))]
    class PinnedSocket:
        def settimeout(self, value):
            pass
        def connect(self, address):
            connections.append(address)
        def close(self):
            pass
    monkeypatch.setattr(socket, "getaddrinfo", resolve)
    monkeypatch.setattr(socket, "socket", lambda *args: PinnedSocket())
    egress_proxy._connect("rebind.example", 443, True, ())
    assert lookups == ["rebind.example"]
    assert connections == [("93.184.216.34", 443)]


def test_each_browser_gets_its_own_listener():
    """One page holding sockets open must not use up another worker's connections."""
    first, again = egress_proxy.proxy_url(True, (), "agent:1"), egress_proxy.proxy_url(True, (), "agent:1")
    second = egress_proxy.proxy_url(True, (), "agent:2")
    assert first == again and first != second


def test_a_browsers_old_listener_is_closed_when_its_settings_change():
    """Changing the allowed local hosts used to leave the old listener (and its thread) open for good."""
    old = urlsplit(egress_proxy.proxy_url(True, ("nas.home",), "agent:3"))
    new = urlsplit(egress_proxy.proxy_url(True, ("printer.home",), "agent:3"))
    assert new.port != old.port
    with pytest.raises(OSError):
        socket.create_connection((old.hostname, old.port), timeout=1).close()
    assert sum(1 for key in egress_proxy._servers if key == ("owner", "agent:3")) == 1
