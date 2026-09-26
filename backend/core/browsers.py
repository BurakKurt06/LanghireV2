"""Chromium-based browsers the agent can drive, and which one the user picked.

The agent controls the browser over the Chrome DevTools Protocol, so only
Chromium-based browsers work (Chrome, Edge, Brave, Vivaldi, Chromium). Firefox
and Safari don't speak CDP.

Settings (settings.json):
    browser       ""        -> automatic: browser-use picks (installed Chrome first)
                  "<id>"    -> one of KNOWN_BROWSERS below
                  "custom"  -> the exe in browser_path
    browser_path  full path to a Chromium-based browser, used when browser == "custom"

Each browser gets its own automation profile folder. Chrome and "automatic"
keep the original browser_profile so existing logins survive.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

# id -> (display name, Windows candidate paths, macOS paths, Linux paths)
KNOWN_BROWSERS: dict[str, tuple[str, list[str], list[str], list[str]]] = {
    "chrome": (
        "Google Chrome",
        [
            r"%PROGRAMFILES%\Google\Chrome\Application\chrome.exe",
            r"%PROGRAMFILES(X86)%\Google\Chrome\Application\chrome.exe",
            r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe",
        ],
        ["/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"],
        ["/usr/bin/google-chrome-stable", "/usr/bin/google-chrome"],
    ),
    "msedge": (
        "Microsoft Edge",
        [
            r"%PROGRAMFILES(X86)%\Microsoft\Edge\Application\msedge.exe",
            r"%PROGRAMFILES%\Microsoft\Edge\Application\msedge.exe",
            r"%LOCALAPPDATA%\Microsoft\Edge\Application\msedge.exe",
        ],
        ["/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge"],
        ["/usr/bin/microsoft-edge-stable", "/usr/bin/microsoft-edge"],
    ),
    "brave": (
        "Brave",
        [
            r"%PROGRAMFILES%\BraveSoftware\Brave-Browser\Application\brave.exe",
            r"%PROGRAMFILES(X86)%\BraveSoftware\Brave-Browser\Application\brave.exe",
            r"%LOCALAPPDATA%\BraveSoftware\Brave-Browser\Application\brave.exe",
        ],
        ["/Applications/Brave Browser.app/Contents/MacOS/Brave Browser"],
        ["/usr/bin/brave-browser"],
    ),
    "vivaldi": (
        "Vivaldi",
        [
            r"%LOCALAPPDATA%\Vivaldi\Application\vivaldi.exe",
            r"%PROGRAMFILES%\Vivaldi\Application\vivaldi.exe",
        ],
        ["/Applications/Vivaldi.app/Contents/MacOS/Vivaldi"],
        ["/usr/bin/vivaldi-stable", "/usr/bin/vivaldi"],
    ),
    "chromium": (
        "Chromium",
        [
            r"%PROGRAMFILES%\Chromium\Application\chrome.exe",
            r"%LOCALAPPDATA%\Chromium\Application\chrome.exe",
        ],
        ["/Applications/Chromium.app/Contents/MacOS/Chromium"],
        ["/usr/bin/chromium", "/usr/bin/chromium-browser", "/snap/bin/chromium"],
    ),
}

# Windows "App Paths" registry names, which catch installs in unusual folders.
_APP_PATHS = {"chrome": "chrome.exe", "msedge": "msedge.exe", "brave": "brave.exe", "vivaldi": "vivaldi.exe"}

# Windows default-browser ProgId prefixes (HKCU ...\UrlAssociations\https\UserChoice).
_PROGID_TO_ID = {"ChromeHTML": "chrome", "MSEdgeHTM": "msedge", "BraveHTML": "brave",
                 "VivaldiHTM": "vivaldi", "ChromiumHTM": "chromium"}


def _candidates(browser_id: str) -> list[str]:
    _, win, mac, linux = KNOWN_BROWSERS[browser_id]
    if sys.platform == "win32":
        return [os.path.expandvars(p) for p in win]
    if sys.platform == "darwin":
        return mac
    return linux


def _registry_app_path(exe_name: str) -> str | None:
    if sys.platform != "win32":
        return None
    import winreg
    key = rf"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\{exe_name}"
    for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
        try:
            with winreg.OpenKey(hive, key) as k:
                value, _ = winreg.QueryValueEx(k, None)
                if value:
                    return value.strip('"')
        except OSError:
            continue
    return None


def find_browser(browser_id: str) -> str | None:
    """Executable path for a known browser id, or None if it isn't installed."""
    if browser_id not in KNOWN_BROWSERS:
        return None
    for path in _candidates(browser_id):
        if Path(path).is_file():
            return path
    reg = _registry_app_path(_APP_PATHS[browser_id]) if browser_id in _APP_PATHS else None
    if reg and Path(reg).is_file():
        return reg
    return None


def default_browser_id() -> str | None:
    """The system default browser's id when it is a known Chromium browser (Windows only)."""
    if sys.platform != "win32":
        return None
    import winreg
    try:
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\Shell\Associations\UrlAssociations\https\UserChoice",
        ) as k:
            prog_id, _ = winreg.QueryValueEx(k, "ProgId")
    except OSError:
        return None
    for prefix, browser_id in _PROGID_TO_ID.items():
        if prog_id.startswith(prefix):
            return browser_id
    return None


def detect_browsers() -> list[dict]:
    """Installed Chromium-based browsers: [{id, name, path, is_default}]."""
    default = default_browser_id()
    found = []
    for browser_id, (name, *_rest) in KNOWN_BROWSERS.items():
        path = find_browser(browser_id)
        if path:
            found.append({"id": browser_id, "name": name, "path": path, "is_default": browser_id == default})
    return found


def resolve_browser(settings: dict, base_profile_dir: Path) -> tuple[str | None, Path]:
    """(executable path or None for automatic, automation profile dir) for the chosen browser.

    Falls back to automatic when the chosen browser is no longer installed, so
    a run never fails just because a browser was uninstalled.
    """
    choice = (settings.get("browser") or "").strip()
    if choice == "custom":
        path = (settings.get("browser_path") or "").strip().strip('"')
        if path and Path(path).is_file():
            return path, base_profile_dir.parent / "browser_profile_custom"
        return None, base_profile_dir
    if choice and choice != "chrome":
        path = find_browser(choice)
        if path:
            return path, base_profile_dir.parent / f"browser_profile_{choice}"
        return None, base_profile_dir
    if choice == "chrome":
        return find_browser("chrome"), base_profile_dir
    return None, base_profile_dir
