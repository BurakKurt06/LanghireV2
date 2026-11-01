"""The test run's own safety net (tests/conftest.py) actually blocks what it says."""
import socket
import subprocess

import pytest

import conftest


def test_the_network_is_off():
    with pytest.raises(OSError, match="may not reach the network"):
        socket.create_connection(("93.184.216.34", 80), timeout=1)


def test_a_browser_cant_be_launched():
    with pytest.raises(RuntimeError, match="may not launch a browser"):
        subprocess.Popen([r"C:\Program Files\BraveSoftware\Brave-Browser\Application\brave.exe", "--new-window"])


def test_a_browser_session_cant_start():
    import asyncio
    from browser_use import BrowserSession
    with pytest.raises(RuntimeError, match="may not start a real browser"):
        asyncio.run(BrowserSession(user_data_dir=str(conftest.SESSION_HOME / "p")).start())


def test_the_memory_database_is_the_tests_own(tmp_path):
    import memory.store as store
    from core import shared_config
    assert store.MemoryStore().db_path == tmp_path / "memory_store.db"
    assert shared_config.get_memory_store().db_path == tmp_path / "memory_store.db"
    assert not store.DB_PATH.is_relative_to(conftest.ROOT)  # never the repo-root (developer) database


def test_the_database_follows_the_home_folder(tmp_path, monkeypatch):
    """Codex's case: from source, memory and metrics used <repo>/memory_store.db whatever HOME said,
    so the integration script's throwaway home still wrote to it."""
    from memory import store, metrics
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    for module in (store, metrics):
        path = module._get_db_path()
        assert path.is_relative_to(tmp_path), path
        assert path.name == "memory_store.db"
