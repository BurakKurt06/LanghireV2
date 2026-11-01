"""Passwords, API keys and sign-in tokens: encrypted on disk, masked for the page, kept out of logs.

All values here are made up. Encryption is Windows DPAPI (this Windows user only),
so the at-rest checks run on Windows.
"""
import io
import json
import logging
import sys

import pytest

from core import chatgpt_auth, config, secret_store
from core import shared_config as sc

windows_only = pytest.mark.skipif(sys.platform != "win32", reason="DPAPI is Windows-only")
FAKE_PASSWORD = "fake-Passw0rd-for-tests"
FAKE_KEY = "sk-fake-0123456789abcdef"


def _raw(data_dir, name):
    return (data_dir / name).read_text(encoding="utf-8")


@windows_only
def test_settings_secrets_are_encrypted_on_disk(data_dir):
    config.save_settings({"sensitive_data": {"email": "a@example.com", "password": FAKE_PASSWORD},
                          "captcha_api_key": FAKE_KEY, "theme": "dark"})
    raw = _raw(data_dir, "settings.json")
    assert FAKE_PASSWORD not in raw and FAKE_KEY not in raw and secret_store.PREFIX in raw
    assert "a@example.com" in raw and '"dark"' in raw          # everything else stays readable
    loaded = config.load_settings()
    assert loaded["sensitive_data"]["password"] == FAKE_PASSWORD and loaded["captcha_api_key"] == FAKE_KEY


@windows_only
def test_llm_keys_are_encrypted_on_disk(data_dir):
    config.save_llm_settings({"provider": "openai", "openai": {"api_key": FAKE_KEY, "model": "gpt-x"},
                              "bedrock": {"access_key": "AKIAFAKEFAKE", "secret_key": "fake/secret/key"}})
    raw = _raw(data_dir, "llm_settings.json")
    assert FAKE_KEY not in raw and "AKIAFAKEFAKE" not in raw and "fake/secret/key" not in raw
    loaded = config.load_llm_settings()
    assert loaded["openai"]["api_key"] == FAKE_KEY and loaded["bedrock"]["secret_key"] == "fake/secret/key"


@windows_only
def test_chatgpt_tokens_are_encrypted_on_disk(data_dir):
    chatgpt_auth._save({"access_token": "fake.access.token", "refresh_token": "fake-refresh-token", "id_token": "x.y.z"})
    raw = _raw(data_dir, "chatgpt_auth.json")
    assert "fake-refresh-token" not in raw and "fake.access.token" not in raw
    assert chatgpt_auth.load()["refresh_token"] == "fake-refresh-token"


@windows_only
def test_migration_encrypts_plain_files_once(data_dir):
    (data_dir / "settings.json").write_text(json.dumps(
        {"sensitive_data": {"email": "a@example.com", "password": FAKE_PASSWORD}, "resume_path": "C:/r.pdf"}))
    (data_dir / "llm_settings.json").write_text(json.dumps({"provider": "gemini", "gemini": {"api_key": FAKE_KEY}}))
    (data_dir / "chatgpt_auth.json").write_text(json.dumps({"refresh_token": "fake-refresh-token", "expires_at": 1}))
    assert config.migrate_secrets() == 3
    for name, secret in (("settings.json", FAKE_PASSWORD), ("llm_settings.json", FAKE_KEY),
                         ("chatgpt_auth.json", "fake-refresh-token")):
        assert secret not in _raw(data_dir, name)
    assert config.load_settings()["sensitive_data"]["password"] == FAKE_PASSWORD
    assert config.load_settings()["resume_path"] == "C:/r.pdf"
    assert config.load_llm_settings()["gemini"]["api_key"] == FAKE_KEY
    assert chatgpt_auth.load()["refresh_token"] == "fake-refresh-token"
    assert config.migrate_secrets() == 0   # nothing left to do


def test_a_failed_migration_leaves_the_file_as_it_was(data_dir, monkeypatch):
    before = json.dumps({"sensitive_data": {"email": "", "password": FAKE_PASSWORD}})
    (data_dir / "settings.json").write_text(before)

    def broken(text):
        raise secret_store.SecretError("encrypted value didn't read back")

    monkeypatch.setattr(secret_store, "protect", broken)
    config.migrate_secrets()
    assert _raw(data_dir, "settings.json") == before


def test_masking_and_keeping_saved_secrets():
    stored = {"openai": {"api_key": FAKE_KEY, "model": "a"}, "gemini": {"api_key": ""}}
    masked = config.mask_secrets(stored, config.LLM_SECRETS)
    assert masked["openai"]["api_key"] == config.SECRET_MASK and masked["gemini"]["api_key"] == ""
    assert FAKE_KEY not in json.dumps(masked)
    sent_back = {"openai": {"api_key": config.SECRET_MASK, "model": "b"}, "gemini": {"api_key": "new-key"}}
    kept = config.keep_masked(sent_back, stored, config.LLM_SECRETS)
    assert kept == {"openai": {"api_key": FAKE_KEY, "model": "b"}, "gemini": {"api_key": "new-key"}}
    # A mask for a secret that was never saved becomes empty, never the mask itself.
    assert config.keep_masked({"anthropic": {"api_key": config.SECRET_MASK}}, {}, config.LLM_SECRETS) == {"anthropic": {"api_key": ""}}


def test_tracebacks_are_redacted_in_logs(data_dir, monkeypatch):
    monkeypatch.setattr(sc, "_secret_cache", {"key": None, "values": ()})
    config.save_settings({"sensitive_data": {"email": "", "password": FAKE_PASSWORD}})
    config.save_llm_settings({"provider": "gemini", "gemini": {"api_key": FAKE_KEY}})
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(sc.RedactingFormatter("%(levelname)s %(message)s"))
    log = logging.getLogger("langhire.redaction_test")
    log.addHandler(handler)
    log.propagate = False
    try:
        try:
            raise RuntimeError(f"401 for https://api.example/v1?key={FAKE_KEY} (password {FAKE_PASSWORD})")
        except RuntimeError:
            log.error("Request failed", exc_info=True)
    finally:
        log.removeHandler(handler)
    out = stream.getvalue()
    assert "Traceback" in out and "RuntimeError" in out
    assert FAKE_KEY not in out and FAKE_PASSWORD not in out and "***" in out


def test_employer_passwords_and_tokens_are_redacted(data_dir, monkeypatch):
    monkeypatch.setattr(sc, "_secret_cache", {"key": None, "values": ()})
    site_pw = secret_store.site_password("acme.wd5.myworkdayjobs.com")
    chatgpt_auth._save({"refresh_token": "fake-refresh-token-123", "access_token": "a.b.c"})
    text = sc.redact_secrets(f"typed {site_pw}; refreshing with fake-refresh-token-123")
    assert site_pw not in text and "fake-refresh-token-123" not in text
