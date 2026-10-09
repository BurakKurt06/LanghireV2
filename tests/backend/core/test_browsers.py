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


def test_build_browser_kwargs_grants_location_so_no_popup_waits(monkeypatch, installed):
    monkeypatch.setattr(shared_config, "load_runtime_settings", lambda: {})
    permissions = shared_config.build_browser_kwargs()["permissions"]
    assert "geolocation" in permissions
    assert {"clipboardReadWrite", "notifications"} <= set(permissions)  # browser-use's defaults kept
