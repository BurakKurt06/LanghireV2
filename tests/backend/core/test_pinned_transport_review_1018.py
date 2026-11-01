"""Actual local TLS connections: pinning must not merge different TLS identities."""
from datetime import datetime, timedelta, timezone
import http.server
import socket
import ssl
import threading

import httpx
import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

from core import job_page

pytestmark = pytest.mark.real_posting_fetch


@pytest.fixture
def tls_redirect(tmp_path, monkeypatch):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "one.test")])
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
            .serial_number(x509.random_serial_number()).not_valid_before(datetime.now(timezone.utc)-timedelta(days=1))
            .not_valid_after(datetime.now(timezone.utc)+timedelta(days=1))
            .add_extension(x509.SubjectAlternativeName([x509.DNSName("one.test")]), critical=False)
            .sign(key, hashes.SHA256()))
    cert_path, key_path = tmp_path/"cert.pem", tmp_path/"key.pem"
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                         serialization.NoEncryption()))
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(cert_path, key_path)
    requests, snis, destination = [], [], {"host": "two.test"}
    context.set_servername_callback(lambda sock, name, context: snis.append(name))
    class Handler(http.server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        def do_GET(self):
            requests.append((self.headers["Host"], self.path))
            body = b"<title>Analyst</title>"
            self.send_response(302 if self.path == "/start" else 200)
            if self.path == "/start":
                self.send_header("Location", f"https://{destination['host']}:{self.server.server_port}/target"
                                 if destination["host"] else "/target")
                body = b""
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        def log_message(self, *args):
            pass
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    server.socket = context.wrap_socket(server.socket, server_side=True)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    client_context = ssl.create_default_context(cafile=str(cert_path))
    original_init = httpx.HTTPTransport.__init__
    def init(self, *args, **kwargs):
        kwargs["verify"] = client_context
        original_init(self, *args, **kwargs)
    monkeypatch.setattr(httpx.HTTPTransport, "__init__", init)
    monkeypatch.setattr(job_page, "_public_only", lambda request: None)
    original_public = job_page._public_address
    original_dns = socket.getaddrinfo
    monkeypatch.setattr(socket, "getaddrinfo", lambda host, port, *a, **k:
                        [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", port))]
                        if host == "private.test" else original_dns(host, port, *a, **k))
    monkeypatch.setattr(job_page, "_public_address", lambda host, port:
                        original_public(host, port) if host == "private.test" else "127.0.0.1")
    yield f"https://one.test:{server.server_port}/start", requests, snis, destination
    server.shutdown()
    server.server_close()


@pytest.mark.parametrize("host", ["two.test", "127.0.0.1"])
def test_redirect_never_reuses_another_names_verified_tls_connection(tls_redirect, host):
    url, requests, snis, destination = tls_redirect
    destination["host"] = host
    # Expose handshake failures in fixture setup rather than passing because
    # fetch() swallowed one before the redirect was ever reached.
    with httpx.Client(transport=job_page._PinnedTransport(), trust_env=False) as c:
        first = c.get(url)
        assert first.status_code == 302
    requests.clear()
    snis.clear()
    result = job_page.fetch(url)
    assert result is None, (requests, snis)
    assert len(requests) == 1 and requests[0][1] == "/start", (requests, snis)


def test_redirect_private_address_is_refused_before_a_second_connection(tls_redirect):
    url, requests, snis, destination = tls_redirect
    destination["host"] = "private.test"
    assert job_page.fetch(url) is None
    assert len(requests) == 1 and snis == ["one.test"]


def test_relative_redirect_preserves_the_verified_name_and_host(tls_redirect):
    url, requests, snis, destination = tls_redirect
    destination["host"] = ""
    result = job_page.fetch(url)
    assert result is not None and result.title == "Analyst"
    assert len(requests) == 2 and all(host.startswith("one.test:") for host, path in requests)
    assert snis == ["one.test"]  # a connection can be reused for its own verified name


def test_imported_posting_redirect_host_limit_precedes_the_next_request(monkeypatch):
    # Virtual HTTPS origins and a mock response isolate the redirect policy
    # from TLS, whose actual wire identity is tested separately above.
    seen = []
    def handle(self, request):
        seen.append(request.headers["Host"])
        return httpx.Response(302, headers={"Location": "https://outside.example/target"})
    monkeypatch.setattr(job_page, "_public_only", lambda request: None)
    monkeypatch.setattr(job_page, "_public_address", lambda host, port: "93.184.216.34")
    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", handle)
    assert job_page.fetch("https://board.example/job/123", allowed_hosts=["board.example"]) is None
    assert seen == ["board.example"]
