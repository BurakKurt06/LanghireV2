"""Normal board sign-in is explicit user activity, never an agent capability."""
from types import SimpleNamespace

import pytest

import main
from sources.registry import PluginRegistry
from tests.backend.test_main_api import _isolate_state, auth_client, client  # noqa: F401


@pytest.fixture
def launches(monkeypatch):
    from core import shared_config
    monkeypatch.setattr(shared_config, "BROWSER_PROFILE_DIR", main.get_data_dir() / "browser_profile")
    calls = []

    class Process:
        def poll(self):
            return None

    def start(args, **kwargs):
        calls.append((args, kwargs))
        return Process()

    monkeypatch.setattr(main, "_login_process", None)
    monkeypatch.setattr(main, "_browser_user", lambda: "")
    monkeypatch.setattr(main, "_find_playwright_chromium", lambda: "C:/test-browser/chrome.exe")
    monkeypatch.setattr(main.subprocess, "Popen", start)
    return calls


@pytest.mark.parametrize("board", ["linkedin", "indeed", "glassdoor", "handshake", "interninsider",
    "naukri", "reed", "seek", "stepstone", "ziprecruiter"])
def test_every_bundled_board_opens_its_own_login_in_the_collection_profile(auth_client, launches, board):
    from core import shared_config
    result = auth_client.post(f"/auth/login/{board}").json()
    plugin = PluginRegistry().get_by_name(board)
    assert result == {"success": True,
        "message": f"Sign in to {plugin.display_name} in the window that opened, then close it."}
    args, kwargs = launches[0]
    assert args[-1] == plugin.login_url
    assert args[0] == "C:/test-browser/chrome.exe"
    assert f"--user-data-dir={shared_config.selected_browser()[1]}" in args
    assert not any(flag in arg for arg in args for flag in (
        "remote-debugging", "enable-automation", "proxy", "ignore-certificate", "disable-web-security"))
    assert kwargs["stdout"] == main.subprocess.DEVNULL


@pytest.mark.parametrize("busy", ["login", "collect", "apply", "inbox"])
def test_board_login_never_competes_with_a_profile_user(auth_client, launches, monkeypatch, busy):
    monkeypatch.setattr(main, "_browser_user", lambda: busy)
    monkeypatch.setattr(main, "_get_browser_profile_dir", lambda: pytest.fail("Busy profiles must not be prepared"))
    assert auth_client.post("/auth/login/indeed").json()["success"] is False
    assert launches == []


@pytest.mark.parametrize("name", ["imported", "https://evil.example", "--proxy-server=evil", "unknown"])
def test_untrusted_plugins_and_caller_urls_cannot_launch_a_login(auth_client, launches, monkeypatch, name):
    monkeypatch.setattr(main, "_get_plugin_registry", lambda: SimpleNamespace(get_by_name=lambda _: SimpleNamespace(
        is_builtin=False, login_url="https://evil.example", display_name="Impersonated board")))
    assert auth_client.post("/auth/login/" + name).json().get("success") is not True
    assert launches == []


@pytest.mark.parametrize("url", ["http://board.example/login", "https://user:pass@board.example/login",
    "https://board.example:444/login", "https://board.example:invalid/login", "file:///C:/private"])
def test_invalid_bundled_login_addresses_do_not_launch(auth_client, launches, monkeypatch, url):
    monkeypatch.setattr(main, "_get_plugin_registry", lambda: SimpleNamespace(get_by_name=lambda _: SimpleNamespace(
        is_builtin=True, login_url=url, display_name="Test board")))
    assert auth_client.post("/auth/login/indeed").json()["success"] is False
    assert launches == []


def test_board_login_requires_the_existing_api_authentication(client, launches):
    assert client.post("/auth/login/indeed").status_code == 401
    assert launches == []


def test_collector_detects_a_login_window_and_its_exit_without_reading_cookies(auth_client, launches, monkeypatch):
    assert auth_client.get("/jobs/collect/status").json()["login_window_open"] is False
    assert auth_client.post("/auth/login/indeed").json()["success"] is True
    # Status must not open or copy the cookie store while its browser is active.
    monkeypatch.setattr(main, "_get_browser_profile_dir", lambda: pytest.fail("Status must not inspect the profile"))
    assert auth_client.get("/jobs/collect/status").json()["login_window_open"] is True
    monkeypatch.setattr(main._login_process, "poll", lambda: 0)
    assert auth_client.get("/jobs/collect/status").json()["login_window_open"] is False
