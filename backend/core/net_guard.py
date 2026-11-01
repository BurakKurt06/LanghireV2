"""Automation browser network policy.

Production launches use the local enforcing SOCKS proxy in egress_proxy, with no
DIRECT fallback or implicit loopback bypass. It validates DNS results and connects
to the checked numeric address. WebRTC UDP is disabled in each unused profile;
setup refuses profiles it cannot secure. QUIC is disabled. The PAC generator below
is retained for the earlier regression checks, not used by production launches.

Explicit local-host allowances still bypass destination restrictions. This is not
an OS firewall and does not protect other processes or prove every Chromium network
feature obeys the proxy. See the local HTTP, STUN UDP and TURN TCP regression tests.
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


# Chromium implicitly bypasses proxies for loopback. Remove that bypass as well
# as installing the PAC; the local browser regression checks a page's loopback
# subrequest. Localhost names are also made unresolvable unless allowed explicitly.
NO_LOOPBACK_BYPASS = "--proxy-bypass-list=<-loopback>"
NO_LOCALHOST_NAMES = "--host-resolver-rules=MAP localhost ~NOTFOUND, MAP *.localhost ~NOTFOUND"
# WebRTC sends UDP (STUN, peer traffic) without asking the proxy script, to any address a page names.
# Brave and Chrome ignore --force-webrtc-ip-handling-policy (tested); the profile preference works.
WEBRTC_POLICY = "disable_non_proxied_udp"


def browser_args(block_mail: bool, allowed_hosts: list[str] | None = None, owner: str = "") -> list[str]:
    from .egress_proxy import proxy_url
    args = ["--proxy-server=" + proxy_url(block_mail, allowed_hosts or (), owner), NO_LOOPBACK_BYPASS, "--disable-quic"]
    if not any((h or "").strip().lower() in ("localhost", "127.0.0.1", "::1") for h in allowed_hosts or []):
        args.append(NO_LOCALHOST_NAMES)
    return args


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
