"""Browser picker: detection and resolving the chosen browser to an exe + profile."""
import json
from pathlib import Path

import pytest

from core import browsers, shared_config

BASE = Path("/data/langhirev2/browser_profile")


@pytest.fixture
def installed(monkeypatch):
    """Pretend Chrome and Edge are installed, Edge as the system default."""
    paths = {"chrome": "C:/chrome.exe", "msedge": "C:/msedge.exe"}
    monkeypatch.setattr(browsers, "find_browser", lambda bid: paths.get(bid))
    monkeypatch.setattr(browsers, "default_browser_id", lambda: "msedge")
    monkeypatch.setattr(browsers, "find_playwright_chromium", lambda: None)
    return paths


def test_detect_lists_only_installed_and_marks_default(installed):
    found = browsers.detect_browsers()
    assert [b["id"] for b in found] == ["chrome", "msedge"]
    assert [b["is_default"] for b in found] == [False, True]


def test_automatic_keeps_original_profile_and_lets_browser_use_choose(installed):
    assert browsers.resolve_browser({}, BASE) == (None, BASE)


def test_chrome_keeps_original_profile_so_logins_survive(installed):
    assert browsers.resolve_browser({"browser": "chrome"}, BASE) == ("C:/chrome.exe", BASE)


def test_other_browser_gets_its_own_profile(installed):
    exe, profile = browsers.resolve_browser({"browser": "msedge"}, BASE)
    assert exe == "C:/msedge.exe"
    assert profile == BASE.parent / "browser_profile_msedge"


def test_uninstalled_choice_falls_back_to_automatic(installed):
    assert browsers.resolve_browser({"browser": "brave"}, BASE) == (None, BASE)


def test_custom_path(tmp_path, installed):
    exe = tmp_path / "opera.exe"
    exe.write_text("")
    assert browsers.resolve_browser({"browser": "custom", "browser_path": f'"{exe}"'}, BASE) == (
        str(exe), BASE.parent / "browser_profile_custom")
    assert browsers.resolve_browser({"browser": "custom", "browser_path": "C:/missing.exe"}, BASE) == (None, BASE)


def test_build_browser_kwargs_passes_exe_and_profile(monkeypatch, installed):
    monkeypatch.setattr(shared_config, "load_runtime_settings", lambda: {"browser": "msedge"})
    kwargs = shared_config.build_browser_kwargs()
    assert kwargs["executable_path"] == "C:/msedge.exe"
    assert kwargs["user_data_dir"].endswith("browser_profile_msedge")


def test_build_browser_kwargs_automatic_has_no_exe(monkeypatch, installed):
    monkeypatch.setattr(shared_config, "load_runtime_settings", lambda: {})
    kwargs = shared_config.build_browser_kwargs()
    assert "executable_path" not in kwargs
    assert kwargs["user_data_dir"] == str(shared_config.BROWSER_PROFILE_DIR)


def test_automatic_uses_the_login_executable_when_bundled_chromium_is_available(monkeypatch, installed):
    monkeypatch.setattr(shared_config, "load_runtime_settings", lambda: {})
    monkeypatch.setattr(browsers, "find_playwright_chromium", lambda: "C:/bundled/chrome.exe")
    assert shared_config.build_browser_kwargs(purpose="collection")["executable_path"] == "C:/bundled/chrome.exe"


@pytest.mark.parametrize("platform,layout", [
    ("win32", "chrome-win64/chrome.exe"), ("win32", "chrome-win/chrome.exe"),
    ("linux", "chrome-linux64/chrome"), ("linux", "chrome-linux/chrome"),
    ("darwin", "chrome-mac-arm64/Google Chrome for Testing.app/Contents/MacOS/Google Chrome for Testing"),
    ("darwin", "chrome-mac/Chromium.app/Contents/MacOS/Chromium"),
])
def test_normal_login_and_automation_share_modern_and_legacy_discovery(monkeypatch, tmp_path, platform, layout):
    import main
    cache = tmp_path / "browsers"
    executable = cache / "chromium-1217" / layout
    executable.parent.mkdir(parents=True)
    executable.write_bytes(b"fake executable; never launched")
    monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH", str(cache))
    monkeypatch.setattr(browsers.sys, "platform", platform)
    assert browsers.find_playwright_chromium() == str(executable)
    assert main._find_playwright_chromium() == str(executable)


def test_chromium_cache_versions_are_compared_numerically(monkeypatch, tmp_path):
    for version in (999, 1217):
        executable = tmp_path / f"chromium-{version}" / "chrome-win64/chrome.exe"
        executable.parent.mkdir(parents=True)
        executable.write_bytes(b"fake; never launched")
    monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH", str(tmp_path))
    monkeypatch.setattr(browsers.sys, "platform", "win32")
    assert browsers.find_playwright_chromium() == str(tmp_path / "chromium-1217/chrome-win64/chrome.exe")


def test_forget_open_tabs_removes_saved_tabs_but_keeps_sign_ins(tmp_path):
    default = tmp_path / "Default"
    (default / "Sessions").mkdir(parents=True)
    for name in ("Session_1", "Tabs_1"):
        (default / "Sessions" / name).write_bytes(b"x")
    (default / "Current Session").write_bytes(b"x")
    (default / "Cookies").write_bytes(b"x")
    (default / "Login Data").write_bytes(b"x")

    assert shared_config.forget_open_tabs(tmp_path) == 3
    assert not any((default / "Sessions").iterdir())
    assert not (default / "Current Session").exists()
    assert (default / "Cookies").exists() and (default / "Login Data").exists()


def test_forget_open_tabs_on_a_new_profile_does_nothing(tmp_path):
    assert shared_config.forget_open_tabs(tmp_path / "missing") == 0


def test_the_apps_profile_is_used_in_place_not_a_temporary_copy(monkeypatch, installed, tmp_path):
    """browser-use runs a copy of user_data_dir when the executable path contains "chrome" (the
    bundled Chromium's does), so what the agent's browser stored was thrown away after each run."""
    import shutil
    from browser_use import BrowserSession
    monkeypatch.setattr(shared_config, "load_runtime_settings", lambda: {})
    monkeypatch.setattr(browsers, "find_playwright_chromium", lambda: "C:/bundled/chrome.exe")
    for purpose, worker in (("collection", 1), ("application", 2), ("mail", 1)):
        kwargs = shared_config.build_browser_kwargs(worker=worker, purpose=purpose)
        session = BrowserSession(headless=True, **kwargs)
        # Resolved: Windows may spell the same temp folder as RUNNER~1 or runneradmin.
        assert Path(session.browser_profile.user_data_dir).resolve() == Path(kwargs["user_data_dir"]).resolve(), purpose
    # A folder the app didn't prepare keeps browser-use's own behaviour.
    other = BrowserSession(headless=True, executable_path="C:/bundled/chrome.exe", user_data_dir=str(tmp_path / "other"))
    try:
        assert "browser-use-user-data-dir-" in str(other.browser_profile.user_data_dir)
    finally:
        shutil.rmtree(other.browser_profile.user_data_dir, ignore_errors=True)


def _app_browser_args(purpose, worker=1):
    from browser_use import BrowserSession
    kwargs = shared_config.build_browser_kwargs(worker=worker, purpose=purpose)
    return BrowserSession(headless=True, **kwargs).browser_profile.get_args()


@pytest.mark.parametrize("purpose,worker", [("collection", 1), ("application", 2), ("mail", 1)])
def test_stop_recognises_the_apps_own_browsers(monkeypatch, installed, purpose, worker):
    """Stop's leftover-browser cleanup finds app browsers by their --user-data-dir. On browser-use's
    temporary copies it never could; on Windows the folder may also be spelled RUNNER~1 vs runneradmin."""
    import main
    monkeypatch.setattr(shared_config, "load_runtime_settings", lambda: {})
    monkeypatch.setattr(browsers, "find_playwright_chromium", lambda: "C:/bundled/chrome.exe")
    assert main._uses_owned_profile(_app_browser_args(purpose, worker), main._owned_profile_prefix())


def test_stop_recognises_app_browsers_when_the_home_folder_is_a_link(monkeypatch, installed, tmp_path):
    """browser-use launches with the resolved profile path; the data folder may be reached through a
    link (a symlinked home here, an 8.3 short name on Windows)."""
    import main
    real, link = tmp_path / "real-home", tmp_path / "link-home"
    real.mkdir()
    try:
        link.symlink_to(real, target_is_directory=True)
    except OSError:
        pytest.skip("symbolic links are not permitted here")
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: link))
    monkeypatch.setattr(shared_config, "BROWSER_PROFILE_DIR", main.get_data_dir() / "browser_profile")
    monkeypatch.setattr(shared_config, "load_runtime_settings", lambda: {})
    monkeypatch.setattr(browsers, "find_playwright_chromium", lambda: "C:/bundled/chrome.exe")
    assert main._uses_owned_profile(_app_browser_args("collection"), main._owned_profile_prefix())


def test_build_browser_kwargs_forgets_old_tabs(monkeypatch, installed):
    seen = []
    monkeypatch.setattr(shared_config, "load_runtime_settings", lambda: {})
    monkeypatch.setattr(shared_config, "forget_open_tabs", seen.append)
    kwargs = shared_config.build_browser_kwargs()
    assert [str(p) for p in seen] == [kwargs["user_data_dir"]]


def test_webrtc_udp_is_turned_off_and_the_other_settings_kept(tmp_path):
    """WebRTC's UDP ignores the proxy rules (a page reached a local port; see test_browser_network_live)."""
    from core import net_guard
    prefs = tmp_path / "Default" / "Preferences"
    prefs.parent.mkdir()
    prefs.write_text(json.dumps({"homepage": "https://example.com", "webrtc": {"multiple_routes_enabled": False}}))
    assert net_guard.limit_webrtc(tmp_path)
    assert json.loads(prefs.read_text()) == {"homepage": "https://example.com", "webrtc": {
        "multiple_routes_enabled": False, "ip_handling_policy": "disable_non_proxied_udp"}}
    assert net_guard.limit_webrtc(tmp_path / "new")   # a new profile gets it before its first start
    assert json.loads((tmp_path / "new" / "Default" / "Preferences").read_text())["webrtc"] == {
        "ip_handling_policy": "disable_non_proxied_udp"}


def test_unreadable_preferences_are_left_alone(tmp_path):
    from core import net_guard
    prefs = tmp_path / "Default" / "Preferences"
    prefs.parent.mkdir()
    prefs.write_text("{not json")
    assert not net_guard.limit_webrtc(tmp_path) and prefs.read_text() == "{not json"


def test_build_browser_kwargs_limits_webrtc(monkeypatch, installed):
    from core import net_guard
    seen = []
    monkeypatch.setattr(shared_config, "load_runtime_settings", lambda: {})
    monkeypatch.setattr(net_guard, "limit_webrtc", lambda path: seen.append(path) or True)
    kwargs = shared_config.build_browser_kwargs(purpose="mail")
    assert [str(p) for p in seen] == [kwargs["user_data_dir"]]


def test_no_site_gets_standing_permissions(monkeypatch, installed):
    """No clipboard (it can hold passwords), notifications or location for every page."""
    monkeypatch.setattr(shared_config, "load_runtime_settings", lambda: {})
    assert shared_config.build_browser_kwargs()["permissions"] == []


@pytest.mark.parametrize("purpose, blocks_mail, profile_suffix", [
    ("agent", True, ""), ("inbox", False, ""), ("mail", False, "_mail"),
])
def test_every_automation_browser_really_starts_with_the_guards(monkeypatch, installed, purpose, blocks_mail,
                                                                  profile_suffix):
    """A real BrowserSession accepts the kwargs (block_ip_addresses once crashed every session)."""
    from browser_use import BrowserSession
    from core import net_guard
    monkeypatch.setattr(shared_config, "load_runtime_settings", lambda: {"browser": "msedge"})
    session = BrowserSession(chromium_sandbox=True, **shared_config.build_browser_kwargs(purpose=purpose))
    profile = session.browser_profile
    assert profile.block_ip_addresses is True and profile.permissions == []
    assert profile.args == net_guard.browser_args(block_mail=blocks_mail, owner=f"{purpose}:1")
    assert str(profile.user_data_dir).endswith("browser_profile_msedge" + profile_suffix)
    launch = " ".join(profile.get_args())
    assert "--proxy-server=socks5://127.0.0.1:" in launch and "MAP localhost ~NOTFOUND" in launch
    assert "--proxy-pac-url=" not in launch and "--disable-quic" in launch


@pytest.mark.parametrize("value, expected", [(None, 1), (0, 1), (1, 1), (3, 3), (4, 4), (9, 4), ("2", 2), ("x", 1)])
def test_parallel_jobs_is_1_to_4(value, expected):
    assert shared_config.parallel_jobs({"parallel_jobs": value}) == expected


def test_each_worker_after_the_first_gets_its_own_profile(monkeypatch, installed):
    monkeypatch.setattr(shared_config, "load_runtime_settings", lambda: {"browser": "msedge"})
    first = shared_config.build_browser_kwargs()["user_data_dir"]
    assert shared_config.build_browser_kwargs(worker=1)["user_data_dir"] == first
    assert shared_config.build_browser_kwargs(worker=3)["user_data_dir"] == first + "_w3"


def test_worker_profiles_copy_sign_ins_but_not_caches(tmp_path, monkeypatch):
    base = tmp_path / "browser_profile_brave"
    for path in ("Local State", "Default/Network/Cookies", "Default/Login Data", "Default/Local Storage/leveldb/000.log",
                 "Default/Cache/Cache_Data/f_000001", "Default/Code Cache/js/index", "Default/Sessions/Session_1",
                 "lockfile", "BrowserMetrics/a.pma"):
        (base / path).parent.mkdir(parents=True, exist_ok=True)
        (base / path).write_bytes(b"x")
    stale = tmp_path / "browser_profile_brave_w4"
    stale.mkdir()
    monkeypatch.setattr(shared_config, "selected_browser", lambda settings=None: (None, base))

    made = shared_config.prepare_worker_profiles(3)

    assert made == [tmp_path / "browser_profile_brave_w2", tmp_path / "browser_profile_brave_w3"]
    for copy in made:
        assert (copy / "Local State").exists() and (copy / "Default/Network/Cookies").exists()
        assert (copy / "Default/Login Data").exists() and (copy / "Default/Local Storage/leveldb/000.log").exists()
        for skipped in ("Default/Cache", "Default/Code Cache", "Default/Sessions", "lockfile", "BrowserMetrics"):
            assert not (copy / skipped).exists(), skipped
    assert not stale.exists()  # a copy for a worker you no longer run is removed


def test_worker_profiles_are_refreshed_each_run(tmp_path, monkeypatch):
    base = tmp_path / "p"
    (base / "Default").mkdir(parents=True)
    (base / "Default" / "Cookies").write_text("old")
    monkeypatch.setattr(shared_config, "selected_browser", lambda settings=None: (None, base))
    shared_config.prepare_worker_profiles(2)
    (tmp_path / "p_w2" / "Default" / "Leftover").write_text("from the last run")
    (base / "Default" / "Cookies").write_text("new sign-in")
    shared_config.prepare_worker_profiles(2)
    assert (tmp_path / "p_w2" / "Default" / "Cookies").read_text() == "new sign-in"
    assert not (tmp_path / "p_w2" / "Default" / "Leftover").exists()


def test_the_network_guard_script():
    from core import net_guard
    agent = net_guard.pac_script(block_mail=True)
    inbox = net_guard.pac_script(block_mail=False, allowed_hosts=["nas.home"])
    assert "var BLOCK_MAIL = true" in agent and '"mail.google.com"' in agent
    assert "var BLOCK_MAIL = false" in inbox and 'var ALLOWED = ["nas.home"]' in inbox
    for rule in ('"10.0.0.0"', '"127.0.0.0"', '"192.168.0.0"', '"172.16.0.0"', '"169.254.0.0"', '"100.64.0.0"'):
        assert rule in agent
    # Allowing a loopback service also keeps localhost resolvable.
    assert net_guard.NO_LOCALHOST_NAMES not in net_guard.browser_args(True, ["localhost"])
    assert net_guard.NO_LOCALHOST_NAMES in net_guard.browser_args(True, [])


def test_the_mail_profile_copies_only_the_sign_in_cookies(tmp_path, monkeypatch):
    base = tmp_path / "browser_profile_brave"
    for rel in ("Local State", "Default/Network/Cookies", "Default/Preferences", "Default/Login Data",
                "Default/History", "Default/Cache/data_0"):
        (base / rel).parent.mkdir(parents=True, exist_ok=True)
        (base / rel).write_text("x")
    monkeypatch.setattr(shared_config, "selected_browser", lambda settings=None: (None, base))
    dest = shared_config.prepare_mail_profile()
    assert dest == tmp_path / "browser_profile_brave_mail"
    assert (dest / "Local State").exists() and (dest / "Default/Network/Cookies").exists()
    assert not (dest / "Default/Login Data").exists() and not (dest / "Default/History").exists()
    assert not (dest / "Default/Cache").exists()
