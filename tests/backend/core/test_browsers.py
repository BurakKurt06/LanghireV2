"""Browser picker: detection and resolving the chosen browser to an exe + profile."""
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


def test_build_browser_kwargs_forgets_old_tabs(monkeypatch, installed):
    seen = []
    monkeypatch.setattr(shared_config, "load_runtime_settings", lambda: {})
    monkeypatch.setattr(shared_config, "forget_open_tabs", seen.append)
    kwargs = shared_config.build_browser_kwargs()
    assert [str(p) for p in seen] == [kwargs["user_data_dir"]]


def test_location_is_not_granted_to_every_site(monkeypatch, installed):
    monkeypatch.setattr(shared_config, "load_runtime_settings", lambda: {})
    permissions = shared_config.build_browser_kwargs()["permissions"]
    assert "geolocation" not in permissions
    assert {"clipboardReadWrite", "notifications"} <= set(permissions)  # browser-use's defaults kept


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
