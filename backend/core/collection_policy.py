"""Collection has read capabilities, never application-mode sending authority.

Built-in boards have configured board/CDN read hosts for runtime modules and GET
APIs. Other destinations need exact declared capabilities. Imported plugins use
the stricter policy. Write methods and GET bodies are refused in both policies;
the request broker still enforces public-address/TLS and mail/channel protections.
"""
from html.parser import HTMLParser
from http.cookies import SimpleCookie, CookieError
import threading
from urllib.parse import urljoin, urlsplit, parse_qsl, urldefrag


def is_write(request) -> bool:
    """Any verb but GET/HEAD, or a GET/HEAD that carries a body."""
    return request.get("method") not in ("GET", "HEAD") or bool(
        request.get("hasPostData") or request.get("postData") or request.get("postDataEntries"))


def _url(value, base=""):
    try:
        value = urldefrag(urljoin(base, value))[0]
        p = urlsplit(value)
        if p.scheme != "https" or not p.hostname or p.username or p.password or p.port not in (None, 443):
            return ""
        return value
    except (ValueError, TypeError):
        return ""


def _css_urls(text):
    """Read URL tokens, ignoring comments and strings such as content:'url(/send)'."""
    i, size = 0, len(text)
    def quoted(start):
        quote = text[start]
        end = start+1
        while end < size:
            if text[end] == "\\":
                end += 2
            elif text[end] == quote:
                return text[start+1:end], end+1
            else:
                end += 1
        return "", size
    while i < size:
        if text.startswith("/*", i):
            end = text.find("*/", i+2)
            i = size if end < 0 else end+2
            continue
        if text[i] in "\"'":
            _, i = quoted(i)
            continue
        is_url = text[i:i+4].lower() == "url(" and (not i or not (text[i-1].isalnum() or text[i-1] in "_-"))
        is_import = text[i:i+7].lower() == "@import" and (i+7 == size or not text[i+7].isalnum())
        if is_url or is_import:
            i += 4 if is_url else 7
            while i < size and text[i].isspace():
                i += 1
            if i < size and text[i] in "\"'":
                value, i = quoted(i)
                if value:
                    yield value
            elif is_url:
                end = text.find(")", i)
                if end < 0:
                    return
                yield text[i:end].strip()
                i = end+1
            continue
        i += 1


class CollectionPolicy:
    def __init__(self, search_url, job_url=lambda url: "", read_documents=(), read_hosts=(), any_public_reads=False):
        self.search_url = _url(search_url)
        self.job_url = job_url
        self.read_documents = {_url(value) for value in read_documents} - {""}
        # Built-in boards need runtime chunks and GET APIs. This is an explicit
        # read-compatibility choice; it cannot prove that server GETs have no
        # side effects. Imported plugins keep the exact capability policy.
        self.read_hosts = frozenset(str(host).lower().rstrip(".") for host in read_hosts)
        # Built-in boards also load anti-bot checks from other hosts (Google reCAPTCHA,
        # PerimeterX on li.protechts.net, Cloudflare); refusing them made public LinkedIn
        # hide every result (0 saved). User-approved 2026-10-28: any public HTTPS GET
        # without a body is a read; every write is still refused, and the gate still
        # blocks private addresses and Gmail.
        self.any_public_reads = bool(any_public_reads)
        self.documents = ({self.search_url} | self.read_documents) - {""}
        self.resources = set()
        self.lock = threading.RLock()
        self.refused = 0
        self.cookies = set()
        self.sealed = False

    def seal(self):
        """Quick reads/scrolls/clicks a settled page with no outbound capability.

        Only the controller's validated next-page navigation opens a new load.
        Even reusing a previously declared resource URL is refused after sealing.
        """
        with self.lock:
            self.sealed = True

    def snapshot_cookies(self, cookies):
        with self.lock:
            for cookie in cookies:
                self.cookies.add((cookie["domain"].lstrip("."), cookie.get("path", "/"), cookie["name"], cookie["value"]))

    def read_cookie_header(self, url, header):
        """Only browser-start cookies and server Set-Cookie values can be sent.

        A script cannot encode entered text in a cookie and reload a read URL.
        Chrome still chooses which cookies apply; this filter only removes values.
        """
        p = urlsplit(url)
        kept=[]
        with self.lock:
            for pair in header.split(";"):
                name, sep, value = pair.strip().partition("=")
                if sep and any(name==n and value==v and (p.hostname==domain or p.hostname.endswith("."+domain))
                               and (p.path or "/").startswith(path) for domain,path,n,v in self.cookies):
                    kept.append(pair.strip())
        return "; ".join(kept)

    def navigation(self, value):
        value = _url(value)
        if not value:
            return False
        start, dest = urlsplit(self.search_url), urlsplit(value)
        original_pairs, query_pairs = parse_qsl(start.query), parse_qsl(dest.query)
        if len(query_pairs) != len({key for key,_ in query_pairs}):
            return False
        original, query = dict(original_pairs), dict(query_pairs)
        paging = {"page", "pageno", "start", "offset", "cursor", "after", "before", "pageNumber"}
        search = (dest.netloc == start.netloc and dest.path == start.path
                  and all(query.get(k) == v for k, v in original.items() if k not in paging)
                  and set(query) <= set(original) | paging)
        if not search and not self.dynamic_read(value):
            return False
        with self.lock:
            self.documents = {value} | self.read_documents
            self.resources.clear()
            self.sealed = False
        return True

    def dynamic_read(self, value):
        value = _url(value)
        host = urlsplit(value).hostname if value else ""
        return bool(host and any(host == base or host.endswith("." + base) for base in self.read_hosts))

    def observe_jobs(self, urls):
        """Only controller-observed listing links grant canonical posting reads."""
        with self.lock:
            for value in urls:
                canonical = _url(self.job_url(value))
                if canonical and len(self.documents) < 500:
                    self.documents.add(canonical)

    def allows(self, request, resource_type):
        value = _url(request.get("url", ""))
        # GET bodies and all write verbs are refused before upload reads or transport.
        if is_write(request):
            self.refused += 1
            return False
        with self.lock:
            if self.sealed:
                self.refused += 1
                return False
            allowed = bool(self.any_public_reads and value) or self.dynamic_read(value) or (value in self.documents if resource_type == "Document" else
                       resource_type in {"Script", "Stylesheet", "Image", "Font", "Media", "Manifest"} and value in self.resources)
        if not allowed:
            self.refused += 1
        return allowed

    def observe(self, url, headers, body):
        """Grant exact passive dependencies from bounded, already-authorized responses."""
        host = urlsplit(url).hostname
        for header in headers:
            if header["name"].lower() == "set-cookie":
                jar = SimpleCookie()
                try:
                    jar.load(header["value"])
                except (ValueError, TypeError, CookieError):
                    continue
                with self.lock:
                    for name, cookie in jar.items():
                        domain = (cookie["domain"] or host).lstrip(".")
                        if host == domain or host.endswith("."+domain):
                            self.cookies.add((domain, cookie["path"] or "/", name, cookie.value))
        mime = next((h["value"].split(";", 1)[0].strip().lower() for h in headers
                     if h["name"].lower() == "content-type"), "")
        if len(body) > 3_000_000:
            return
        text = body.decode("utf-8", errors="replace")
        resources, documents, css = set(), set(), []
        if mime in ("text/html", "application/xhtml+xml"):
            class Reader(HTMLParser):
                in_style = False
                def handle_data(self, data):
                    if self.in_style:
                        css.append(data)
                def handle_endtag(self, tag):
                    if tag == "style":
                        self.in_style = False
                def handle_starttag(self, tag, attrs):
                    attrs = dict(attrs)
                    if tag == "style":
                        self.in_style = True
                    if attrs.get("style"):
                        css.append(attrs["style"])
                    # A base element can only resolve declared resources; it grants no host wildcard.
                    if tag in ("iframe", "frame") and attrs.get("src"):
                        documents.add(_url(attrs["src"], url))
                    if tag == "a" and attrs.get("href"):
                        canonical = _url(self_outer.job_url(_url(attrs["href"], url)))
                        if canonical:
                            documents.add(canonical)
                    if tag in ("script", "img", "source", "video", "audio", "input") and attrs.get("src"):
                        resources.add(_url(attrs["src"], url))
                    if tag == "link" and set((attrs.get("rel") or "").lower().split()) & {"stylesheet", "icon", "preload", "modulepreload"}:
                        resources.add(_url(attrs.get("href", ""), url))
                    for src in (attrs.get("srcset") or "").split(","):
                        if src.strip():
                            resources.add(_url(src.strip().split()[0], url))
            self_outer = self
            parser = Reader()
            try:
                parser.feed(text)
            except (ValueError, RecursionError):
                return
        if mime == "text/css":
            css.append(text)
        for stylesheet in css:
            for value in _css_urls(stylesheet):
                resources.add(_url(value, url))
        with self.lock:
            # Bound adversarial markup, including repeated responses.
            if len(self.resources) + len(resources) <= 20000 and len(self.documents) + len(documents) <= 500:
                self.resources.update(resources - {""})
                self.documents.update(documents - {""})
