"""Local SOCKS5 CONNECT proxy: resolve once, validate, then connect to that exact IP.

The browser retains TLS/certificate verification. No HTTP bodies, cookies or TLS
contents are inspected. UDP ASSOCIATE is refused. Listeners belong to this backend
process and close at exit; failed proxies have no DIRECT fallback in Chromium.
"""
from __future__ import annotations

import atexit
import select
import socket
import socketserver
import threading
import time

from .job_page import _public_address


def _address_for(host: str, port: int, block_mail: bool, allowed_hosts: tuple[str, ...]) -> str:
    host = host.lower().rstrip(".")
    if not host or any(c in host for c in "/\\@%\x00"):
        raise ValueError("Invalid proxy destination")
    if block_mail and any(host == name or host.endswith("." + name)
                          for name in ("mail.google.com", "gmail.com", "inbox.google.com")):
        raise ValueError("Mail is unavailable to the application agent")
    if host in allowed_hosts:
        # An explicit user allowance is the only way to connect to a private address.
        answers = {info[4][0] for info in socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)}
        if not answers:
            raise ValueError("No destination address")
        return sorted(answers, key=lambda address: ":" in address)[0]
    if ("." not in host and ":" not in host) or host.endswith((".local", ".localhost", ".internal")):
        raise ValueError("Local names are unavailable")
    return _public_address(host, port)


def _connect(host: str, port: int, block_mail: bool, allowed_hosts: tuple[str, ...]):
    address = _address_for(host, port, block_mail, allowed_hosts)
    upstream = socket.socket(socket.AF_INET6 if ":" in address else socket.AF_INET, socket.SOCK_STREAM)
    try:
        upstream.settimeout(10)
        upstream.connect((address, port))  # numeric address: never resolve the name again
        return upstream
    except BaseException:
        upstream.close()
        raise


def _read(sock, count):
    result = bytearray()
    while len(result) < count:
        part = sock.recv(count - len(result))
        if not part:
            raise OSError("Proxy client disconnected")
        result.extend(part)
    return bytes(result)


class _Handler(socketserver.BaseRequestHandler):
    def handle(self):
        if not self.server.slots.acquire(blocking=False):
            return
        upstream = None
        ready = False
        try:
            self.request.settimeout(10)
            version, methods = _read(self.request, 2)
            if version != 5 or 0 not in _read(self.request, methods):
                self.request.sendall(b"\x05\xff")
                return
            self.request.sendall(b"\x05\x00")
            version, command, reserved, kind = _read(self.request, 4)
            if version != 5 or command != 1 or reserved != 0:
                raise ValueError("Only SOCKS CONNECT is supported")
            if kind == 1:
                host = socket.inet_ntop(socket.AF_INET, _read(self.request, 4))
            elif kind == 4:
                host = socket.inet_ntop(socket.AF_INET6, _read(self.request, 16))
            elif kind == 3:
                host = _read(self.request, _read(self.request, 1)[0]).decode("ascii")
            else:
                raise ValueError("Invalid address type")
            port = int.from_bytes(_read(self.request, 2), "big")
            if port == 0:
                raise ValueError("Invalid port")
            if not self.server.permitted():
                raise ValueError("Application request gate is unavailable")
            upstream = _connect(host, port, self.server.block_mail, self.server.allowed_hosts)
            self.request.sendall(b"\x05\x00\x00\x01" + b"\x00" * 6)
            ready = True
            deadline = time.monotonic() + 300
            while time.monotonic() < deadline:
                if not self.server.permitted():
                    return
                readable, _, _ = select.select([self.request, upstream], [], [], 0.1)
                for source in readable:
                    if not self.server.permitted():
                        return
                    data = source.recv(65536)
                    if not data:
                        return
                    (upstream if source is self.request else self.request).sendall(data)
                    deadline = time.monotonic() + 300
        except (OSError, ValueError, UnicodeError):
            if not ready:
                try:
                    self.request.sendall(b"\x05\x02\x00\x01" + b"\x00" * 6)
                except OSError:
                    pass
        except Exception:
            # Resolver/validation library failures must close the connection too.
            if not ready:
                try:
                    self.request.sendall(b"\x05\x02\x00\x01" + b"\x00" * 6)
                except OSError:
                    pass
        finally:
            if upstream is not None:
                upstream.close()
            self.server.slots.release()


class _Server(socketserver.ThreadingTCPServer):
    daemon_threads = True
    request_queue_size = 64

    def permitted(self):
        try:
            return self.guard is None or self.guard() is True
        except Exception:
            return False


_servers = {}
_lock = threading.Lock()


def proxy_url(block_mail: bool, allowed_hosts=(), owner: str = "") -> str:
    """The proxy for one browser (`owner`, e.g. "agent:2"). Each browser gets its own listener and
    connection slots: a page holding many sockets open (WebSockets) used to fill the slots that every
    application worker shared, and the other workers' pages then failed to load."""
    policy = (bool(block_mail), tuple(sorted({h.strip().lower().rstrip(".") for h in allowed_hosts
                                            if isinstance(h, str) and h.strip()})))
    # One listener per browser. When a browser's settings change (Settings -> allowed local hosts), its
    # old listener is closed: build_browser_kwargs has already checked that browser isn't running.
    key = ("owner", owner) if owner else policy
    with _lock:
        server = _servers.get(key)
        if server is not None and (server.block_mail, server.allowed_hosts) != policy:
            server.shutdown()
            server.server_close()
            server = None
        if server is None:
            server = _Server(("127.0.0.1", 0), _Handler)
            server.block_mail, server.allowed_hosts = policy
            server.slots = threading.BoundedSemaphore(64)
            server.guard = None
            thread = threading.Thread(target=server.serve_forever, daemon=True, name="browser-egress")
            thread.start()
            _servers[key] = server
        return "socks5://127.0.0.1:" + str(server.server_address[1])


def protect(owner, healthy):
    """Fail closed before launch and if the independent request debugger dies."""
    with _lock:
        server = _servers.get(("owner", owner))
        if server is None:
            raise RuntimeError("Application proxy must exist before its request gate")
        server.guard = healthy


@atexit.register
def close():
    with _lock:
        for server in _servers.values():
            server.shutdown()
            server.server_close()
        _servers.clear()
