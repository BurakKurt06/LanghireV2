"""Adversarial checks of Claude's follow-up, using isolated state only."""
import json
import stat
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from core import net_guard, shared_config


@pytest.mark.parametrize("kind", ["unreadable", "protected"])
def test_an_unsecured_webrtc_profile_cannot_be_launched(tmp_path, monkeypatch, kind):
    profile = tmp_path / "browser"
    prefs = profile / "Default" / "Preferences"
    prefs.parent.mkdir(parents=True)
    prefs.write_text("{broken" if kind == "unreadable" else json.dumps({"homepage": "about:blank"}))
    monkeypatch.setattr(shared_config, "load_runtime_settings", lambda: {})
    monkeypatch.setattr(shared_config, "selected_browser", lambda settings=None: (None, profile))
    if kind == "protected":
        def refuse_replace(*args):
            raise PermissionError("protected preferences")
        monkeypatch.setattr(net_guard.os, "replace", refuse_replace)
    original = prefs.read_bytes()
    with pytest.raises(RuntimeError, match="WebRTC"):
        shared_config.build_browser_kwargs()
    assert prefs.read_bytes() == original


def test_read_only_preferences_are_preserved_on_windows(tmp_path):
    import sys
    if sys.platform != "win32":
        pytest.skip("Windows read-only replacement semantics")
    prefs = tmp_path / "Default" / "Preferences"
    prefs.parent.mkdir()
    original = json.dumps({"homepage": "about:blank"})
    prefs.write_text(original)
    prefs.chmod(stat.S_IREAD)
    try:
        assert not net_guard.limit_webrtc(tmp_path)
        assert prefs.read_text() == original
    finally:
        prefs.chmod(stat.S_IWRITE)


@pytest.mark.parametrize("host", ["localhost:80@evil.example", "127.0.0.1:80.evil.example", "localhost:not-a-port"])
def test_the_backend_rejects_malformed_host_authorities(host):
    import main
    with TestClient(main.app, base_url="http://127.0.0.1") as client:
        assert client.get("/health", headers={"Host": host}).status_code == 400


@pytest.mark.parametrize("args", [["--user-data-dir={profile}"], ["--user-data-dir", "{profile}"]])
def test_renamed_browser_using_the_profile_is_not_missed(tmp_path, monkeypatch, args):
    import psutil
    process = SimpleNamespace(info={"name": "custom-chromium.exe"},
                              cmdline=lambda: ["custom-chromium.exe", *[a.format(profile=tmp_path) for a in args]])
    monkeypatch.setattr(psutil, "process_iter", lambda *args: iter([process]))
    assert not net_guard.limit_webrtc(tmp_path)
    assert not (tmp_path / "Default" / "Preferences").exists()


def test_a_different_profile_does_not_block_setup(tmp_path, monkeypatch):
    import psutil
    process = SimpleNamespace(info={"name": "brave.exe"},
                              cmdline=lambda: ["brave.exe", "--user-data-dir=" + str(tmp_path) + "-other"])
    monkeypatch.setattr(psutil, "process_iter", lambda *args: iter([process]))
    assert net_guard.limit_webrtc(tmp_path)


@pytest.mark.parametrize("method", ["GET", "OPTIONS"])
def test_duplicate_or_untrusted_hosts_do_not_pass_cors(method):
    import main
    with TestClient(main.app, base_url="http://127.0.0.1") as client:
        headers = [("Host", "127.0.0.1"), ("Host", "evil.example")]
        assert client.request(method, "/health", headers=headers).status_code == 400
        headers = {"Host": "evil.example", "Origin": "http://localhost:1420",
                   "Access-Control-Request-Method": "GET"}
        assert client.request(method, "/health", headers=headers).status_code == 400
