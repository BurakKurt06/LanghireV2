"""Automation browser network policy.

Two kinds of launch:

* Application, sign-in, Inbox and verification-email browsers connect by themselves, as a
  normal browser does (`direct_args`). A small rule script only names what stays out of reach:
  Gmail (agent browsers), private address literals and local names. Everything else is the browser's own
  connection: its own TLS, WebSockets, workers and service workers. Before 2026-11 these went
  through a local proxy and a request gate that fetched every request in the backend; real
  hiring sites broke on that (new tabs stayed blank, workers and WebSockets never ran), and
  the gate needed "--host-resolver-rules", which Chrome warns about on every launch.

* Job collection keeps the gate (`browser_args`): its browser connects nowhere, the local
  SOCKS proxy in egress_proxy refuses everything, and the backend fetches what the
  collection policy allows. WebRTC UDP is disabled in its profile; QUIC is disabled.

Explicit local-host allowances still bypass destination restrictions. This is not an OS
firewall and does not protect other processes or prove every Chromium network feature obeys
the rules. See the local HTTP, STUN UDP and TURN TCP regression tests.
"""
from __future__ import annotations

import base64
import json
import os
import tempfile
from pathlib import Path

DEAD_PROXY = "PROXY 127.0.0.1:9"
GMAIL_HOSTS = ("mail.google.com", "gmail.com", "inbox.google.com")

_PAC = r"""
var ALLOWED = %(allowed)s;
var BLOCK_MAIL = %(block_mail)s;
var MAIL = %(mail)s;
function isPrivateV4(ip) {
  return isInNet(ip, "0.0.0.0", "255.0.0.0") || isInNet(ip, "10.0.0.0", "255.0.0.0") ||
    isInNet(ip, "127.0.0.0", "255.0.0.0") || isInNet(ip, "100.64.0.0", "255.192.0.0") ||
    isInNet(ip, "169.254.0.0", "255.255.0.0") || isInNet(ip, "172.16.0.0", "255.240.0.0") ||
    isInNet(ip, "192.168.0.0", "255.255.0.0") || isInNet(ip, "198.18.0.0", "255.254.0.0") ||
    isInNet(ip, "224.0.0.0", "224.0.0.0");
}
function isPrivate(ip) {
  ip = ip.toLowerCase();
  if (ip.indexOf(":") < 0) return isPrivateV4(ip);
  if (ip.indexOf("::ffff:") == 0) return isPrivateV4(ip.substring(7));
  return ip == "::" || ip == "::1" || ip.indexOf("fc") == 0 || ip.indexOf("fd") == 0 ||
    ip.indexOf("fe8") == 0 || ip.indexOf("fe9") == 0 || ip.indexOf("fea") == 0 || ip.indexOf("feb") == 0;
}
function FindProxyForURL(url, host) {
  // "mail.google.com." is the same host as "mail.google.com" (a trailing dot made rules miss it).
  host = host.toLowerCase().replace(/\.+$/, "");
  if (ALLOWED.indexOf(host) >= 0) return "DIRECT";
  if (BLOCK_MAIL) {
    for (var m = 0; m < MAIL.length; m++) {
      if (host == MAIL[m] || dnsDomainIs(host, "." + MAIL[m])) return "%(dead)s";
    }
  }
  if (isPlainHostName(host) || host == "localhost" || shExpMatch(host, "*.localhost") ||
      shExpMatch(host, "*.local") || shExpMatch(host, "*.internal") || host.indexOf(":") >= 0) return "%(dead)s";
  var resolved = (typeof dnsResolveEx == "function") ? dnsResolveEx(host) : dnsResolve(host);
  if (!resolved) return "DIRECT";
  var ips = String(resolved).split(";");
  for (var i = 0; i < ips.length; i++) {
    if (ips[i] && isPrivate(ips[i])) return "%(dead)s";
  }
  return "DIRECT";
}
"""


def pac_script(block_mail: bool, allowed_hosts: list[str] | None = None) -> str:
    allowed = sorted({h.strip().lower() for h in (allowed_hosts or []) if isinstance(h, str) and h.strip()})
    return _PAC % {"allowed": json.dumps(allowed), "block_mail": "true" if block_mail else "false",
                   "mail": json.dumps(list(GMAIL_HOSTS)), "dead": DEAD_PROXY}


def pac_arg(block_mail: bool, allowed_hosts: list[str] | None = None) -> str:
    """The Chromium flag that installs the script (a data: URL, nothing on disk)."""
    encoded = base64.b64encode(pac_script(block_mail, allowed_hosts).encode()).decode()
    return f"--proxy-pac-url=data:application/x-ns-proxy-autoconfig;base64,{encoded}"


# The rule script of a browser that connects by itself. No name is looked up here (a lookup
# per request is what made the older script slow): a destination is refused by what it is
# called. Gmail for agent browsers, this computer, single-word and local names, and private
# or loopback address literals go to a proxy that isn't there; everything else is DIRECT.
_DIRECT_PAC = r"""
var ALLOWED = %(allowed)s;
var BLOCK_MAIL = %(block_mail)s;
var MAIL = %(mail)s;
function privateV4(ip) {
  var p = ip.split(".");
  var a = parseInt(p[0], 10), b = parseInt(p[1], 10);
  return a == 0 || a == 10 || a == 127 || (a == 100 && b >= 64 && b <= 127) || (a == 169 && b == 254) ||
    (a == 172 && b >= 16 && b <= 31) || (a == 192 && b == 168) || (a == 198 && (b == 18 || b == 19)) || a >= 224;
}
function FindProxyForURL(url, host) {
  host = host.toLowerCase().replace(/\.+$/, "").replace(/^\[|\]$/g, "");
  if (ALLOWED.indexOf(host) >= 0) return "DIRECT";
  if (BLOCK_MAIL) {
    for (var m = 0; m < MAIL.length; m++) {
      if (host == MAIL[m] || dnsDomainIs(host, "." + MAIL[m])) return "%(dead)s";
    }
  }
  if (host.indexOf(":") >= 0) return "%(dead)s";
  if (/^\d+$/.test(host) || /^0x[0-9a-f]+$/.test(host)) return "%(dead)s";
  if (/^\d{1,3}(\.\d{1,3}){3}$/.test(host)) return privateV4(host) ? "%(dead)s" : "DIRECT";
  if (host.indexOf(".") < 0 || host == "localhost" || shExpMatch(host, "*.localhost") ||
      shExpMatch(host, "*.local") || shExpMatch(host, "*.internal")) return "%(dead)s";
  return "DIRECT";
}
"""


def direct_pac_script(block_mail: bool, allowed_hosts: list[str] | None = None) -> str:
    allowed = sorted({h.strip().lower().rstrip(".") for h in (allowed_hosts or []) if isinstance(h, str) and h.strip()})
    return _DIRECT_PAC % {"allowed": json.dumps(allowed), "block_mail": "true" if block_mail else "false",
                          "mail": json.dumps(list(GMAIL_HOSTS)), "dead": DEAD_PROXY}


def direct_args(block_mail: bool, allowed_hosts: list[str] | None = None) -> list[str]:
    """Launch flags of a browser that connects by itself (applications, sign-in, Inbox, verification email).

    One rule script (a data: URL, nothing on disk). No proxy server, no resolver rules: nothing
    Chrome warns about, and nothing between the browser and the site.

    What the script does and doesn't do (measured in Chromium and Brave, 2026-11): Gmail is
    unreachable for agent browsers, and so are private address literals and local names. This
    computer itself (localhost, 127.0.0.1) is NOT covered: browsers never ask a rule script
    about loopback. Account details are kept off local addresses where they are typed
    (cli/apply_jobs.credential_refusal), not here.
    """
    encoded = base64.b64encode(direct_pac_script(block_mail, allowed_hosts).encode()).decode()
    return [f"--proxy-pac-url=data:application/x-ns-proxy-autoconfig;base64,{encoded}"]


# Chromium implicitly bypasses proxies for loopback. Remove that bypass as well
# as installing the PAC; the local browser regression checks a page's loopback
# subrequest.
NO_LOOPBACK_BYPASS = "--proxy-bypass-list=<-loopback>"
# WebRTC sends UDP (STUN, peer traffic) without asking the proxy script, to any address a page names.
# Brave and Chrome ignore --force-webrtc-ip-handling-policy (tested); the profile preference works.
WEBRTC_POLICY = "disable_non_proxied_udp"


def browser_args(block_mail: bool, allowed_hosts: list[str] | None = None, owner: str = "") -> list[str]:
    """Launch flags of the collection browser: everything through the local SOCKS proxy.

    The proxy itself refuses this computer and local names (the loopback bypass is removed, so
    they reach it), which is what "--host-resolver-rules=MAP localhost ~NOTFOUND" used to add
    on top. That flag made Chrome show "You are using an unsupported command-line flag" on
    every launch, so it is no longer passed.
    """
    from .egress_proxy import proxy_url
    return ["--proxy-server=" + proxy_url(block_mail, allowed_hosts or (), owner), NO_LOOPBACK_BYPASS, "--disable-quic"]


def _profile_in_use(profile_dir) -> bool:
    """Refuse a live profile: editing its file does not update Chromium's in-memory policy."""
    import psutil
    expected = os.path.normcase(os.path.realpath(profile_dir))
    browser_names = {"chrome", "chrome.exe", "chromium", "chromium.exe", "chromium-browser",
                     "brave", "brave.exe", "brave-browser", "brave browser", "google chrome",
                     "msedge", "msedge.exe", "vivaldi", "vivaldi.exe", "opera", "opera.exe"}
    try:
        for process in psutil.process_iter(["name"]):
            known_browser = (process.info.get("name") or "").lower() in browser_names
            try:
                args = process.cmdline()
                if not args:
                    if known_browser:
                        return True  # this browser's profile cannot be checked
                    continue
                for index, arg in enumerate(args):
                    value = None
                    if arg.startswith("--user-data-dir="):
                        value = arg.split("=", 1)[1]
                    elif arg == "--user-data-dir" and index + 1 < len(args):
                        value = args[index + 1]
                    if value is not None and os.path.normcase(os.path.realpath(value.strip('"'))) == expected:
                        return True
            except psutil.NoSuchProcess:
                continue
            except psutil.AccessDenied:
                if known_browser:
                    return True  # cannot establish that the profile is unused
    except (OSError, psutil.Error):
        return True
    return False


def limit_webrtc(profile_dir) -> bool:
    """Turn off WebRTC's UDP in an automation profile, before its browser starts.

    Only the one preference is changed; the rest of the profile's settings are kept.
    False if already in use or if preferences cannot be secured. The caller must not launch then.
    """
    path = Path(profile_dir) / "Default" / "Preferences"
    temp = None
    try:
        if _profile_in_use(profile_dir):
            return False
        prefs = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        if not isinstance(prefs, dict):
            return False
        webrtc = prefs.get("webrtc")
        if not isinstance(webrtc, dict):
            webrtc = prefs["webrtc"] = {}
        if webrtc.get("ip_handling_policy") == WEBRTC_POLICY:
            return True
        webrtc["ip_handling_policy"] = WEBRTC_POLICY
        path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                         prefix="Preferences.langhire-", delete=False) as output:
            temp = Path(output.name)
            json.dump(prefs, output)
        os.replace(temp, path)
        return True
    except (OSError, ValueError):
        return False
    finally:
        if temp is not None:
            temp.unlink(missing_ok=True)


def restore_webrtc(profile_dir) -> bool:
    """Undo limit_webrtc in a profile whose browser now connects by itself (best effort).

    Earlier versions set the "no UDP outside the proxy" policy in every automation profile. A
    browser without a proxy doesn't need it, and the application browser should behave like an
    ordinary one. Never refuses a launch: False only says nothing was changed.
    """
    path = Path(profile_dir) / "Default" / "Preferences"
    temp = None
    try:
        if not path.exists():
            return False
        prefs = json.loads(path.read_text(encoding="utf-8"))
        webrtc = prefs.get("webrtc") if isinstance(prefs, dict) else None
        if not isinstance(webrtc, dict) or webrtc.get("ip_handling_policy") != WEBRTC_POLICY:
            return False
        if _profile_in_use(profile_dir):
            return False
        del webrtc["ip_handling_policy"]
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                         prefix="Preferences.langhire-", delete=False) as output:
            temp = Path(output.name)
            json.dump(prefs, output)
        os.replace(temp, path)
        return True
    except (OSError, ValueError):
        return False
    finally:
        if temp is not None:
            temp.unlink(missing_ok=True)
