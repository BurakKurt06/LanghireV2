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
