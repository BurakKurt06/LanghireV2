"""
Industry-grade API / integration tests for the LangHireV2 FastAPI backend.

These exercise ``backend/main.py`` in-process with FastAPI's ``TestClient``
(no real HTTP server, no subprocess, no browser, no network). Heavy
dependencies (LLM creation, resume tailoring, Playwright) are monkeypatched so
only the request/response and persistence layers are exercised.

Auth model (see ``_AuthMiddleware`` in ``backend/main.py``):
  * ``/health`` and ``/chromium/status`` are unauthenticated.
  * Every other path requires ``Authorization: Bearer <_API_TOKEN>``.
  * If an ``Origin`` header is present it must be in ``_ALLOWED_ORIGINS``,
    otherwise the request is rejected with 403 *before* the token is checked.

``_API_TOKEN`` is captured from ``$JOB_APPLICANT_TOKEN`` (or a random secret)
*at import time*, so we set that env var before importing ``main`` and reuse
the value the module actually loaded.
"""
import os

# Pin the auth token BEFORE importing the backend so the middleware uses a
# value we know. main.py reads JOB_APPLICANT_TOKEN at import time.
os.environ.setdefault("JOB_APPLICANT_TOKEN", "test-token-deadbeef")

import pytest
from fastapi.testclient import TestClient

import main  # noqa: E402  (import after env var is set)

TOKEN = main._API_TOKEN


def main_mask():
    from core.config import SECRET_MASK
    return SECRET_MASK
AUTH = {"Authorization": f"Bearer {TOKEN}"}


# ── Fixtures ────────────────────────────────────────────────────────────────
@pytest.fixture(autouse=True)
def _isolate_state(data_dir, monkeypatch):
    """Repoint stateful module-level paths at the per-test tmp data dir.

    Several backend modules resolve their storage paths *once at import time*
    from the real ``$HOME`` (``core.shared_config.JOBS_FILE``,
    ``memory.store.DB_PATH``, ``memory.metrics.DB_PATH``). Because ``main`` is
    imported at module load — before the ``data_dir`` fixture repoints HOME —
    those constants still point at the developer's real data dir. We rebind
    them here so every test reads and writes the same isolated tmp storage and
    never touches (or asserts against) real local data.
    """
    import core.shared_config as shared_config
    import memory.store as mem_store
    import memory.metrics as mem_metrics

    jobs_file = data_dir / "jobs.json"
    monkeypatch.setattr(shared_config, "DATA_DIR", data_dir)
    monkeypatch.setattr(shared_config, "JOBS_FILE", jobs_file)
    monkeypatch.setattr(shared_config, "JOBS_LOCK", data_dir / "jobs.json.lock")
    monkeypatch.setattr(shared_config, "QA_FILE", data_dir / "qa_repository.json")
    monkeypatch.setattr(
        shared_config, "CANDIDATE_PROFILE", data_dir / "candidate_profile.json"
    )

    db_path = data_dir / "memory_store.db"
    monkeypatch.setattr(mem_store, "DB_PATH", db_path)
    monkeypatch.setattr(mem_metrics, "DB_PATH", db_path)
    # The store/metrics default arg captured the old DB_PATH at class-definition
    # time, and main._get_*_store() construct them with no args. Force both
    # helpers to build instances against the isolated db.
    monkeypatch.setattr(main, "_get_memory_store", lambda: mem_store.MemoryStore(db_path))
    monkeypatch.setattr(
        main, "_get_metrics_store", lambda: mem_metrics.MetricsStore(db_path)
    )


@pytest.fixture(autouse=True)
def _ready_to_run(request, monkeypatch):
    """Most start tests aren't about readiness: count the app as set up (TestStartReadiness checks it)."""
    if request.node.get_closest_marker("real_readiness") is None:
        monkeypatch.setattr(main, "_start_problem", lambda apply: "")


@pytest.fixture
def client(data_dir):
    """A TestClient bound to the FastAPI app with an isolated data dir.

    ``data_dir`` (from conftest) repoints HOME at a tmp dir so all config /
    profile / settings / memory writes go to throwaway storage. We do NOT enter
    the TestClient context manager because that runs the app lifespan, which
    kills browser processes, spawns a parent watchdog and triggers a background
    Chromium install — none of which the API tests need.
    """
    return TestClient(main.app, base_url="http://127.0.0.1")   # the address the app's window uses


@pytest.fixture
def auth_client(client):
    """A client whose every request carries the valid bearer token."""
    client.headers.update(AUTH)
    return client


# ── Health & Chromium (unauthenticated) ─────────────────────────────────────
class TestHealthAndChromium:
    """Endpoints that the auth middleware lets through without a token."""

    def test_health_no_auth_required(self, client):
        """/health responds 200 even without an Authorization header."""
        resp = client.get("/health")
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] in ("ok", "degraded")
        assert "checks" in body
        for key in ("database", "chromium", "llm_configured", "worker_running"):
            assert key in body["checks"]

    def test_chromium_status_no_auth_required(self, client):
        """/chromium/status is reachable without a token and reports a known state."""
        resp = client.get("/chromium/status")
        assert resp.status_code == 200
        state = resp.json().get("state")
        assert state in ("ready", "installed", "installing", "not_installed", "checking", "failed")


# ── Auth / middleware ────────────────────────────────────────────────────────
class TestAuth:
    """Authorization, origin and rate-limit behaviour of the middleware."""

    def test_protected_endpoint_rejects_without_token(self, client):
        """A protected endpoint returns 401 when no token is supplied."""
        resp = client.get("/profile")
        assert resp.status_code == 401
        assert resp.json() == {"error": "unauthorized"}

    def test_protected_endpoint_rejects_bad_token(self, client):
        """A wrong bearer token is rejected with 401."""
        resp = client.get("/profile", headers={"Authorization": "Bearer nope"})
        assert resp.status_code == 401

    def test_protected_endpoint_accepts_valid_token(self, auth_client):
        """The valid bearer token lets the request through."""
        resp = auth_client.get("/profile")
        assert resp.status_code == 200

    def test_disallowed_origin_is_forbidden(self, client):
        """A non-allowlisted Origin is blocked with 403 before the token check."""
        resp = client.get(
            "/profile",
            headers={"Origin": "https://evil.example.com", **AUTH},
        )
        assert resp.status_code == 403
        assert resp.json() == {"error": "forbidden"}

    @pytest.mark.parametrize("path", ["/health", "/chromium/status", "/profile"])
    def test_a_rebound_website_name_is_refused(self, client, path):
        """DNS rebinding: a page whose own name now points at 127.0.0.1 reads it same-origin (no Origin
        header), but its request still carries that name as the Host."""
        resp = client.get(path, headers={"Host": "evil.example:8000", **AUTH})
        assert resp.status_code == 400

    def test_the_app_window_address_is_accepted(self, client):
        assert client.get("/health", headers={"Host": "127.0.0.1:51234"}).status_code == 200
        assert client.get("/health", headers={"Host": "localhost:51234"}).status_code == 200

    def test_allowed_origin_passes(self, auth_client):
        """An allowlisted Origin combined with a valid token succeeds."""
        resp = auth_client.get("/profile", headers={"Origin": "http://localhost:1420"})
        assert resp.status_code == 200

    def test_health_needs_no_token_but_refuses_other_websites(self, client):
        """/health skips the token (the app polls it while starting) but no web page can read it."""
        assert client.get("/health").status_code == 200
        assert client.get("/health", headers={"Origin": "https://evil.example.com"}).status_code == 403

    def test_cors_only_allows_the_app(self, client):
        resp = client.options("/jobs", headers={"Origin": "https://evil.example.com",
                                                "Access-Control-Request-Method": "GET"})
        assert resp.headers.get("access-control-allow-origin") != "https://evil.example.com"

    def test_tailored_resume_ids_are_never_paths(self, auth_client):
        for bad in ("..%5C..%5Cnotes", "C:%5Cnotes", "abc"):
            assert auth_client.get(f"/resume/tailor/{bad}").status_code == 404
            assert auth_client.delete(f"/resume/tailor/{bad}").status_code == 404


# ── Profile CRUD roundtrip ────────────────────────────────────────────────────
class TestProfile:
    """GET/PUT /profile persistence."""

    def test_default_profile_shape(self, auth_client):
        """A fresh data dir returns the default profile skeleton."""
        body = auth_client.get("/profile").json()
        assert body["name"] == ""
        assert body["country"] == "US"
        assert isinstance(body["target_job_titles"], list)

    def test_profile_save_and_load_roundtrip(self, auth_client):
        """A saved profile is read back verbatim (key fields)."""
        profile = {
            "name": "Test User",
            "email": "test@example.com",
            "country": "IN",
            "notice_period": "30 days",
            "target_job_titles": ["Senior Engineer"],
            "skills": ["Python", "React"],
        }
        save = auth_client.put("/profile", json=profile)
        assert save.status_code == 200
        assert save.json() == {"success": True}

        loaded = auth_client.get("/profile").json()
        assert loaded["name"] == "Test User"
        assert loaded["country"] == "IN"
        assert loaded["notice_period"] == "30 days"
        assert loaded["skills"] == ["Python", "React"]


# ── App settings CRUD ─────────────────────────────────────────────────────────
class TestAppSettings:
    """GET/PUT /settings including server-side clamping."""

    def test_settings_default_shape(self, auth_client):
        body = auth_client.get("/settings").json()
        assert "resume_path" in body
        assert "blocked_domains" in body

    def test_settings_roundtrip(self, auth_client):
        settings = {"resume_path": "/tmp/resume.pdf", "blocked_domains": ["spam.com"]}
        assert auth_client.put("/settings", json=settings).json() == {"success": True}
        loaded = auth_client.get("/settings").json()
        assert loaded["resume_path"] == "/tmp/resume.pdf"
        assert loaded["blocked_domains"] == ["spam.com"]

    def test_settings_clamps_out_of_range_values(self, auth_client):
        """max_failures / human-delay bounds are clamped to safe bounds on save."""
        auth_client.put("/settings", json={"max_failures": 9999, "human_delay_min": -5})
        loaded = auth_client.get("/settings").json()
        assert loaded["max_failures"] == 50  # clamped to upper bound
        assert loaded["human_delay_min"] == 0  # clamped to lower bound

    def test_partial_update_does_not_wipe_other_settings(self, auth_client):
        """A partial PUT is a patch, not a replace.

        save_settings() rewrites the file wholesale, so without merging, the
        theme button (which sends only {"theme"}) destroyed every other key -
        including the account password and the 2Captcha API key.
        """
        auth_client.put("/settings", json={
            "resume_path": "/r.pdf",
            "sensitive_data": {"email": "me@example.com", "password": "pw"},
            "captcha_api_key": "key-123",
            "blocked_domains": ["spam.com"],
        })
        # Now send only the theme, exactly as the theme buttons do.
        auth_client.put("/settings", json={"theme": "dark"})

        loaded = auth_client.get("/settings").json()
        assert loaded["theme"] == "dark"
        assert loaded["resume_path"] == "/r.pdf"
        assert loaded["blocked_domains"] == ["spam.com"]
        # The secrets are kept (the page only ever gets a mask for them).
        stored = main.load_settings()
        assert stored["sensitive_data"]["password"] == "pw"
        assert stored["captcha_api_key"] == "key-123"
        assert loaded["sensitive_data"]["password"] == loaded["captcha_api_key"] == main_mask()

    def test_partial_update_still_replaces_a_supplied_list(self, auth_client):
        """Merging is per key, so a supplied list replaces rather than appends."""
        auth_client.put("/settings", json={"blocked_domains": ["a.com", "b.com"]})
        auth_client.put("/settings", json={"blocked_domains": ["a.com"]})
        assert auth_client.get("/settings").json()["blocked_domains"] == ["a.com"]

    def test_switches_survive_an_unrelated_partial_update(self, auth_client):
        auth_client.put("/settings", json={
            "job_collection_enabled": False, "stealth_enabled": True,
        })
        auth_client.put("/settings", json={"max_failures": 12})
        loaded = auth_client.get("/settings").json()
        assert loaded["job_collection_enabled"] is False
        assert loaded["stealth_enabled"] is True
        assert loaded["max_failures"] == 12

    def test_settings_strips_blank_blocked_domains(self, auth_client):
        auth_client.put("/settings", json={"blocked_domains": ["  ", "real.com", ""]})
        loaded = auth_client.get("/settings").json()
        assert loaded["blocked_domains"] == ["real.com"]


# ── LLM settings CRUD ─────────────────────────────────────────────────────────
class TestLLMSettings:
    """GET/PUT /settings/llm roundtrip."""

    def test_llm_default_shape(self, auth_client):
        body = auth_client.get("/settings/llm").json()
        assert "provider" in body

    def test_llm_settings_roundtrip(self, auth_client):
        settings = {
            "provider": "openrouter",
            "openrouter": {"api_key": "sk-xxx", "model": "meta-llama/llama-3.1-8b-instruct"},
        }
        assert auth_client.put("/settings/llm", json=settings).json() == {"success": True}
        loaded = auth_client.get("/settings/llm").json()
        assert loaded["provider"] == "openrouter"
        assert loaded["openrouter"]["model"] == "meta-llama/llama-3.1-8b-instruct"

    def test_saved_keys_never_go_to_the_page(self, auth_client):
        auth_client.put("/settings/llm", json={"provider": "openai", "openai": {"api_key": "sk-fake-123456", "model": "m"}})
        body = auth_client.get("/settings/llm")
        assert "sk-fake-123456" not in body.text
        assert body.json()["openai"]["api_key"] == main_mask()

    def test_a_masked_key_sent_back_keeps_the_saved_one(self, auth_client):
        auth_client.put("/settings/llm", json={"provider": "openai", "openai": {"api_key": "sk-fake-123456", "model": "m"}})
        auth_client.put("/settings/llm", json={"provider": "openai", "openai": {"api_key": main_mask(), "model": "n"}})
        assert main.load_llm_settings()["openai"] == {"api_key": "sk-fake-123456", "model": "n"}
        auth_client.put("/settings/llm", json={"provider": "openai", "openai": {"api_key": "sk-fake-new", "model": "n"}})
        assert main.load_llm_settings()["openai"]["api_key"] == "sk-fake-new"
        auth_client.put("/settings/llm", json={"provider": "openai", "openai": {"api_key": "", "model": "n"}})
        assert main.load_llm_settings()["openai"]["api_key"] == ""   # cleared on purpose

    def test_connection_test_uses_the_saved_key_for_a_masked_one(self, auth_client, monkeypatch):
        from core import llm_factory
        seen = []

        async def _ok(_llm):
            return "pong"

        monkeypatch.setattr(llm_factory, "create_llm", lambda s: seen.append(s) or object())
        monkeypatch.setattr(llm_factory, "test_connection", _ok)
        auth_client.put("/settings/llm", json={"provider": "gemini", "gemini": {"api_key": "fake-gemini-key", "model": "g"}})
        auth_client.post("/llm/test", json={"provider": "gemini", "gemini": {"api_key": main_mask(), "model": "g"}})
        assert seen[-1]["gemini"]["api_key"] == "fake-gemini-key"


class TestProviderModelLists:
    """Model lists come through the backend: the app's content policy blocks the page from AI providers."""

    @pytest.fixture
    def fake_http(self, monkeypatch):
        import httpx
        calls = []

        class Response:
            def __init__(self, url):
                self.url = url

            def raise_for_status(self):
                pass

            def json(self):
                return {"data": [{"id": "model-a"}]}

        class Client:
            def __init__(self, **kwargs):
                calls.append({"client": kwargs})

            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                return False

            async def get(self, url, headers=None):
                calls.append({"url": url, "headers": headers or {}})
                return Response(url)

        monkeypatch.setattr(httpx, "AsyncClient", Client)
        return calls

    def test_the_saved_key_is_used_and_never_returned(self, auth_client, fake_http):
        auth_client.put("/settings/llm", json={"provider": "openai", "openai": {"api_key": "sk-fake-123456", "model": "m"}})
        resp = auth_client.post("/llm/models", json={"provider": "openai", "api_key": main_mask()})
        assert resp.json() == {"success": True, "data": {"data": [{"id": "model-a"}]}}
        request = fake_http[-1]
        assert request["url"] == "https://api.openai.com/v1/models"
        assert request["headers"]["Authorization"] == "Bearer sk-fake-123456"
        assert fake_http[-2]["client"]["follow_redirects"] is False

    def test_a_typed_key_is_used_as_typed(self, auth_client, fake_http):
        auth_client.post("/llm/models", json={"provider": "gemini", "api_key": "typed-key"})
        assert fake_http[-1]["headers"] == {"x-goog-api-key": "typed-key"}
        assert fake_http[-1]["url"].startswith("https://generativelanguage.googleapis.com/")

    def test_openrouter_needs_no_key_and_others_do(self, auth_client, fake_http):
        assert auth_client.post("/llm/models", json={"provider": "openrouter"}).json()["success"] is True
        body = auth_client.post("/llm/models", json={"provider": "anthropic"}).json()
        assert body["success"] is False and len(fake_http) == 2   # no request without a key

    def test_unknown_providers_and_bad_addresses_are_refused(self, auth_client, fake_http):
        assert auth_client.post("/llm/models", json={"provider": "evil"}).status_code == 400
        resp = auth_client.post("/llm/models", json={"provider": "openai_compatible", "base_url": "file:///etc/passwd"})
        assert resp.status_code == 400 and fake_http == []

    def test_a_local_compatible_server_is_allowed(self, auth_client, fake_http):
        """The user's own LM Studio / vLLM address is intentional configuration."""
        auth_client.post("/llm/models", json={"provider": "openai_compatible", "base_url": "http://localhost:1234/v1/"})
        assert fake_http[-1]["url"] == "http://localhost:1234/v1/models"


# ── Countries ─────────────────────────────────────────────────────────────────
class TestCountries:
    def test_countries_list(self, auth_client):
        """/countries returns >= 18 country configs plus notice-period options."""
        body = auth_client.get("/countries").json()
        assert body["success"] is True
        assert len(body["countries"]) >= 18
        assert "notice_period_options" in body

    def test_get_known_country(self, auth_client):
        body = auth_client.get("/countries").json()
        code = next(iter(body["countries"]))
        resp = auth_client.get(f"/countries/{code}")
        assert resp.status_code == 200
        assert resp.json()["success"] is True

    def test_get_unknown_country_404(self, auth_client):
        """An unsupported country code yields a structured 404."""
        resp = auth_client.get("/countries/ZZ")
        assert resp.status_code == 404
        assert resp.json()["ok"] is False
        assert resp.json()["error"]["code"] == "not_found"


# ── Plugins ───────────────────────────────────────────────────────────────────
class TestPlugins:
    EXPECTED = {"linkedin", "indeed", "seek", "naukri", "reed", "stepstone"}

    def test_plugins_list(self, auth_client):
        """/plugins returns at least the six built-in job sources with full schema."""
        body = auth_client.get("/plugins").json()
        assert body["success"] is True
        plugins = body["plugins"]
        assert len(plugins) >= 6
        names = {p["name"] for p in plugins}
        assert self.EXPECTED.issubset(names)
        sample = plugins[0]
        for field in ("name", "display_name", "version", "countries", "filters"):
            assert field in sample

    def test_plugins_filtered_by_country(self, auth_client):
        """Country filtering returns only global ('ALL') + matching-country plugins."""
        # Unknown country: only the global plugins (countries == ['ALL']) match.
        zz = {p["name"] for p in auth_client.get("/plugins", params={"country": "ZZ"}).json()["plugins"]}
        assert "indeed" in zz and "linkedin" in zz
        assert "naukri" not in zz  # naukri is India-only

        # India: global plugins plus the India-specific one.
        india = {p["name"] for p in auth_client.get("/plugins", params={"country": "IN"}).json()["plugins"]}
        assert "naukri" in india
        assert "reed" not in india  # reed is GB-only

    def test_toggle_unknown_plugin_404(self, auth_client):
        resp = auth_client.put("/plugins/does-not-exist/toggle", json={"enabled": False})
        assert resp.status_code == 404
        assert resp.json()["error"]["code"] == "not_found"

    def test_toggle_known_plugin(self, auth_client):
        resp = auth_client.put("/plugins/linkedin/toggle", json={"enabled": True})
        assert resp.status_code == 200
        assert resp.json() == {"success": True, "name": "linkedin", "enabled": True}

    def test_import_plugin_missing_file_400(self, auth_client):
        """Importing a non-existent YAML path returns a 400 import error."""
        resp = auth_client.post("/plugins/import", json={"file_path": "/nonexistent/plugin.yaml"})
        assert resp.status_code == 400
        assert resp.json()["error"]["code"] == "import_failed"

    def test_reload_plugins(self, auth_client):
        resp = auth_client.post("/plugins/reload")
        assert resp.status_code == 200
        body = resp.json()
        assert body["success"] is True
        assert body["count"] >= 6


# ── Jobs (CRUD over jobs.json in tmp data dir) ────────────────────────────────
class TestJobs:
    def test_jobs_empty(self, auth_client):
        """A fresh data dir has no jobs."""
        assert auth_client.get("/jobs").json() == []

    def test_job_stats_empty(self, auth_client):
        stats = auth_client.get("/jobs/stats").json()
        assert stats["total"] == 0
        for k in ("pending", "applied", "failed", "blocked", "in_progress"):
            assert stats[k] == 0

    def test_add_job_and_list(self, auth_client):
        """A manually added job appears in listing and stats."""
        url = "https://www.linkedin.com/jobs/view/123456"
        resp = auth_client.post("/jobs/add", json={"url": url, "title": "Dev", "company": "Acme"})
        assert resp.status_code == 200
        assert resp.json()["success"] is True

        jobs = auth_client.get("/jobs").json()
        assert any(j["url"] == url for j in jobs)
        assert auth_client.get("/jobs/stats").json()["pending"] == 1

    def test_add_job_missing_url_400(self, auth_client):
        resp = auth_client.post("/jobs/add", json={"title": "Dev"})
        assert resp.status_code == 400
        assert resp.json()["error"]["code"] == "missing_field"

    # ── Bulk add (pasted links / uploaded file) ──────────────────────────
    def test_bulk_add_accepts_a_pasted_block(self, auth_client):
        text = "https://jobs.example.com/a\nhttps://jobs.example.com/b"
        resp = auth_client.post("/jobs/add-bulk", json={"text": text})
        assert resp.status_code == 200
        body = resp.json()
        assert body["added"] == 2
        assert auth_client.get("/jobs/stats").json()["pending"] == 2

    def test_bulk_add_accepts_a_url_list(self, auth_client):
        resp = auth_client.post(
            "/jobs/add-bulk",
            json={"urls": ["https://jobs.example.com/a", "https://jobs.example.com/b"]},
        )
        assert resp.json()["added"] == 2

    def test_bulk_add_dedupes_within_the_same_paste(self, auth_client):
        text = "https://jobs.example.com/a\nhttps://jobs.example.com/a"
        body = auth_client.post("/jobs/add-bulk", json={"text": text}).json()
        assert body["added"] == 1
        assert body["skipped_duplicate"] == 1

    def test_bulk_add_skips_urls_already_in_the_queue(self, auth_client):
        url = "https://jobs.example.com/a"
        auth_client.post("/jobs/add", json={"url": url})
        body = auth_client.post("/jobs/add-bulk", json={"text": url}).json()
        assert body["added"] == 0
        assert body["skipped_duplicate"] == 1

    def test_bulk_add_skips_invalid_and_internal_urls(self, auth_client):
        """Junk lines and SSRF-style internal hosts are rejected, not fatal."""
        text = "\n".join([
            "https://jobs.example.com/good",
            "not-a-url",
            "http://127.0.0.1/admin",
            "http://192.168.1.5/internal",
            "ftp://files.example.com/x",
        ])
        body = auth_client.post("/jobs/add-bulk", json={"text": text}).json()
        assert body["added"] == 1
        assert body["skipped_invalid"] == 4

    def test_bulk_add_ignores_blank_lines_and_decorations(self, auth_client):
        """Pasted lists often carry bullets, quotes and trailing commas."""
        text = '\n- https://jobs.example.com/a\n\n  "https://jobs.example.com/b",\n\t• https://jobs.example.com/c\n'
        body = auth_client.post("/jobs/add-bulk", json={"text": text}).json()
        assert body["added"] == 3

    def test_bulk_added_jobs_are_external_and_pending(self, auth_client):
        """Pasted links must flow into the External/All apply modes."""
        auth_client.post("/jobs/add-bulk", json={"text": "https://jobs.example.com/a"})
        job = auth_client.get("/jobs").json()[0]
        assert job["status"] == "pending"
        assert job["easy_apply"] is False
        assert job["source"] == "pasted"

    def test_bulk_add_empty_input_is_a_no_op(self, auth_client):
        body = auth_client.post("/jobs/add-bulk", json={"text": "   \n\n  "}).json()
        assert body["added"] == 0
        assert body["total_submitted"] == 0

    def test_bulk_add_reports_invalid_samples_for_the_ui(self, auth_client):
        body = auth_client.post("/jobs/add-bulk", json={"text": "nonsense-line"}).json()
        assert body["invalid_samples"] == ["nonsense-line"]

    def test_bulk_add_counts_every_invalid_link_but_samples_ten(self, auth_client):
        text = "\n".join(f"http://10.0.0.{i}/job" for i in range(12))
        body = auth_client.post("/jobs/add-bulk", json={"text": text}).json()
        assert body["skipped_invalid"] == 12 and len(body["invalid_samples"]) == 10

    @pytest.mark.parametrize("path, payload", [
        ("/jobs/add", {"url": "https://jobs.example.com/a"}),
        ("/jobs/add-bulk", {"text": "https://jobs.example.com/a\nhttps://jobs.example.com/b"}),
    ])
    def test_link_checks_run_off_the_event_loop(self, auth_client, monkeypatch, path, payload):
        """Each check can wait on DNS; on the event loop it stalled every other request."""
        import asyncio
        import core.shared_config as shared_config
        on_loop = []

        def check(url, resolve=True):
            try:
                asyncio.get_running_loop()
                on_loop.append(url)
            except RuntimeError:
                pass
            return True

        monkeypatch.setattr(shared_config, "validate_job_url", check)
        assert auth_client.post(path, json=payload).status_code == 200
        assert on_loop == []

    def test_delete_ignores_entries_that_are_not_links(self, auth_client):
        url = "https://www.linkedin.com/jobs/view/556"
        auth_client.post("/jobs/add", json={"url": url})
        resp = auth_client.request("DELETE", "/jobs", json={"urls": [1, None, {"x": 1}, url]})
        assert resp.status_code == 200 and resp.json()["deleted"] == 1
        assert auth_client.get("/jobs").json() == []

    def test_export_jobs_csv_empty(self, auth_client):
        """Export on an empty data dir returns a CSV with only the header row."""
        resp = auth_client.get("/jobs/export")
        assert resp.status_code == 200
        assert "text/csv" in resp.headers["content-type"]
        assert "attachment" in resp.headers["content-disposition"]
        assert "langhirev2-jobs.csv" in resp.headers["content-disposition"]
        lines = [ln for ln in resp.text.splitlines() if ln.strip()]
        assert len(lines) == 1  # header only
        assert "Job Title" in lines[0]
        assert "Company" in lines[0]
        assert "URL" in lines[0]

    def test_export_jobs_csv_with_data(self, auth_client):
        """Added jobs appear as CSV rows with the expected columns."""
        url = "https://www.linkedin.com/jobs/view/555000"
        auth_client.post("/jobs/add", json={"url": url, "title": "Engineer", "company": "Globex", "location": "Remote"})
        resp = auth_client.get("/jobs/export")
        assert resp.status_code == 200
        body = resp.text
        assert "Engineer" in body
        assert "Globex" in body
        assert "Remote" in body
        assert url in body
        # header + at least one data row
        assert len([ln for ln in body.splitlines() if ln.strip()]) >= 2

    def test_export_jobs_csv_requires_auth(self, client):
        """Export endpoint is behind auth like the rest of the API."""
        resp = client.get("/jobs/export")  # no Authorization header
        assert resp.status_code == 401

    def test_add_duplicate_job_409(self, auth_client):
        url = "https://www.linkedin.com/jobs/view/777"
        auth_client.post("/jobs/add", json={"url": url})
        resp = auth_client.post("/jobs/add", json={"url": url})
        assert resp.status_code == 409
        assert resp.json()["error"]["code"] == "duplicate"

    def test_update_status_unknown_job_404(self, auth_client):
        resp = auth_client.put(
            "/jobs/status",
            json={"url": "https://example.com/nope", "status": "applied"},
        )
        assert resp.status_code == 404
        assert resp.json()["error"]["code"] == "not_found"

    def test_update_status_invalid_status_400(self, auth_client):
        url = "https://www.linkedin.com/jobs/view/888"
        auth_client.post("/jobs/add", json={"url": url})
        resp = auth_client.put("/jobs/status", json={"url": url, "status": "bogus"})
        assert resp.status_code == 400
        assert resp.json()["error"]["code"] == "invalid_status"

    def test_update_status_success(self, auth_client):
        url = "https://www.linkedin.com/jobs/view/999"
        auth_client.post("/jobs/add", json={"url": url})
        resp = auth_client.put("/jobs/status", json={"url": url, "status": "applied"})
        assert resp.status_code == 200
        assert resp.json()["status"] == "applied"
        assert auth_client.get("/jobs/stats").json()["applied"] == 1

    def test_delete_jobs(self, auth_client):
        url = "https://www.linkedin.com/jobs/view/555"
        auth_client.post("/jobs/add", json={"url": url})
        resp = auth_client.request("DELETE", "/jobs", json={"urls": [url]})
        assert resp.status_code == 200
        assert resp.json()["deleted"] == 1

    def test_delete_jobs_missing_array_400(self, auth_client):
        resp = auth_client.request("DELETE", "/jobs", json={})
        assert resp.status_code == 400
        assert resp.json()["error"]["code"] == "missing_field"

    def test_jobs_status_filter(self, auth_client):
        """The ?status filter excludes non-matching jobs."""
        url = "https://www.linkedin.com/jobs/view/4242"
        auth_client.post("/jobs/add", json={"url": url})  # pending
        assert auth_client.get("/jobs", params={"status": "applied"}).json() == []
        assert len(auth_client.get("/jobs", params={"status": "pending"}).json()) == 1


# ── Memory / Q&A / dashboard (SQLite-backed, empty store) ─────────────────────
class TestMemoryAndQA:
    def test_memory_stats(self, auth_client):
        stats = auth_client.get("/memory/stats").json()
        assert stats["total_memories"] == 0
        assert "by_category" in stats

    def test_memory_domains_empty(self, auth_client):
        assert auth_client.get("/memory/domains").json() == []

    def test_memory_search_blank_query(self, auth_client):
        """A blank query returns an empty list rather than erroring."""
        assert auth_client.get("/memory/search", params={"q": ""}).json() == []

    def test_memory_export(self, auth_client):
        assert auth_client.get("/memory/export").json() == []

    def test_memory_decay(self, auth_client):
        resp = auth_client.post("/memory/decay", json={"days": 30, "factor": 0.9})
        assert resp.status_code == 200
        assert resp.json()["success"] is True

    def test_memory_decay_validation_error(self, auth_client):
        """factor > 1.0 violates the pydantic model -> 422."""
        resp = auth_client.post("/memory/decay", json={"factor": 5.0})
        assert resp.status_code == 422

    def test_memory_cleanup(self, auth_client):
        resp = auth_client.post("/memory/cleanup", json={"threshold": 0.3})
        assert resp.status_code == 200
        assert resp.json()["success"] is True

    def test_qa_empty(self, auth_client):
        assert auth_client.get("/qa").json() == []

    def test_qa_stats(self, auth_client):
        stats = auth_client.get("/qa/stats").json()
        assert stats == {"total": 0, "answered": 0, "unanswered": 0, "guessed": 0}

    def test_qa_auto_squash(self, auth_client):
        resp = auth_client.post("/qa/auto-squash")
        assert resp.status_code == 200
        assert resp.json()["success"] is True

    def test_dashboard_aggregates(self, auth_client):
        """/dashboard merges jobs + memory (+ metrics) into one payload."""
        body = auth_client.get("/dashboard").json()
        assert "jobs" in body
        assert "memory" in body
        assert "metrics" in body
        assert body["jobs"]["total"] == 0


# ── Metrics & logs (empty store) ──────────────────────────────────────────────
class TestMetricsAndLogs:
    def test_metric_runs_empty(self, auth_client):
        assert auth_client.get("/metrics/runs").json() == []

    def test_metric_domains_empty(self, auth_client):
        assert auth_client.get("/metrics/domains").json() == []

    def test_recent_logs_empty(self, auth_client):
        assert auth_client.get("/logs/recent").json() == []

    def test_runs_with_logs_empty(self, auth_client):
        assert auth_client.get("/logs/runs").json() == []


# ── Setup status & onboarding ─────────────────────────────────────────────────
class TestSetup:
    def test_setup_status_fresh(self, auth_client):
        """A fresh install reports nothing configured / done."""
        body = auth_client.get("/setup/status").json()
        assert body["profile"] is False
        assert body["resume"] is False
        assert body["onboarding_completed"] is False
        assert body["all_required_done"] is False

    def test_complete_onboarding_persists(self, auth_client):
        assert auth_client.post("/setup/complete-onboarding").json() == {"success": True}
        body = auth_client.get("/setup/status").json()
        assert body["onboarding_completed"] is True

    def test_setup_status_llm_done_for_gemini(self, auth_client):
        """A configured Gemini key marks the LLM step done (#40 provider)."""
        auth_client.put("/settings/llm", json={"provider": "gemini", "gemini": {"api_key": "AIza-x", "model": "gemini-2.5-pro"}})
        assert auth_client.get("/setup/status").json()["llm"] is True

    def test_setup_status_llm_done_for_openai_compatible(self, auth_client):
        """A configured base_url marks the LLM step done for openai_compatible."""
        auth_client.put("/settings/llm", json={"provider": "openai_compatible", "openai_compatible": {"base_url": "https://api.together.xyz/v1", "api_key": "", "model": "m"}})
        assert auth_client.get("/setup/status").json()["llm"] is True

    def test_setup_status_llm_not_done_when_gemini_key_blank(self, auth_client):
        auth_client.put("/settings/llm", json={"provider": "gemini", "gemini": {"api_key": "", "model": "gemini-2.5-pro"}})
        assert auth_client.get("/setup/status").json()["llm"] is False


    def test_setup_status_chatgpt_needs_a_sign_in(self, auth_client):
        from core import chatgpt_auth
        auth_client.put("/settings/llm", json={"provider": "chatgpt", "chatgpt": {"model": "gpt-6-luna"}})
        assert auth_client.get("/setup/status").json()["llm"] is False
        chatgpt_auth._save({"access_token": "a.e30.s", "refresh_token": "r"})
        assert auth_client.get("/setup/status").json()["llm"] is True


class TestChatGPTEndpoints:
    def test_status_when_signed_out(self, auth_client):
        body = auth_client.get("/chatgpt/status").json()
        assert body["signed_in"] is False and body["pending"] is False

    def test_models_when_signed_out_is_a_soft_failure(self, auth_client):
        body = auth_client.get("/chatgpt/models").json()
        assert body["success"] is False and body["models"] == []
        assert "Not signed in" in body["message"]

    def test_login_returns_the_url_without_a_real_browser(self, auth_client, monkeypatch):
        from core import chatgpt_auth
        monkeypatch.setattr(chatgpt_auth, "start_login", lambda: "https://auth.openai.com/oauth/authorize?x=1")
        assert auth_client.post("/chatgpt/login").json() == {"url": "https://auth.openai.com/oauth/authorize?x=1", "error": ""}

    def test_login_reports_busy_ports(self, auth_client, monkeypatch):
        from core import chatgpt_auth

        def busy():
            raise RuntimeError("ports 1455 and 1457 are in use")

        monkeypatch.setattr(chatgpt_auth, "start_login", busy)
        assert "in use" in auth_client.post("/chatgpt/login").json()["error"]

    def test_logout(self, auth_client):
        from core import chatgpt_auth
        chatgpt_auth._save({"access_token": "a.e30.s", "refresh_token": "r"})
        assert auth_client.post("/chatgpt/logout").json()["signed_in"] is False


# ── Status endpoints for the long-running workers (no work triggered) ─────────
class TestWorkerStatus:
    def test_collection_status_idle(self, auth_client):
        body = auth_client.get("/jobs/collect/status").json()
        assert body["running"] is False
        assert isinstance(body["log"], list)

    def test_apply_status_idle(self, auth_client):
        body = auth_client.get("/apply/status").json()
        assert body["running"] is False
        assert body["workers"] == 1


# ── Auth/login status (cookie inspection, no browser launched) ────────────────
class TestAuthStatus:
    def test_auth_status_no_cookies(self, auth_client):
        """With no browser profile, both services report logged_in=False."""
        body = auth_client.get("/auth/status").json()
        assert body["linkedin"]["logged_in"] is False
        assert body["gmail"]["logged_in"] is False

    def test_login_unknown_service(self, auth_client):
        """An unknown login service is rejected without launching anything."""
        body = auth_client.post("/auth/login/myspace").json()
        assert body["success"] is False
        assert "Unknown service" in body["message"]


# ── LLM-dependent endpoints: validation branches + mocked success ─────────────
class TestLLMEndpoints:
    """These endpoints call create_llm / network. We never invoke the real
    dependency: we either hit the input-validation branch, or monkeypatch
    create_llm + the LLM's ainvoke to assert the success path."""

    def test_llm_test_invalid_key_message(self, auth_client, monkeypatch):
        """/llm/test surfaces a friendly message when create_llm raises an auth error."""
        from core import llm_factory

        def _boom(_settings):
            raise RuntimeError("Invalid api_key provided")

        monkeypatch.setattr(llm_factory, "create_llm", _boom)
        resp = auth_client.post("/llm/test", json={"provider": "openai"})
        assert resp.status_code == 200
        body = resp.json()
        assert body["success"] is False
        assert "Invalid API key" in body["message"]

    def test_llm_test_success_mocked(self, auth_client, monkeypatch):
        """/llm/test returns success when create_llm + test_connection are stubbed."""
        from core import llm_factory

        async def _ok(_llm):
            return "pong"

        monkeypatch.setattr(llm_factory, "create_llm", lambda s: object())
        monkeypatch.setattr(llm_factory, "test_connection", _ok)
        resp = auth_client.post("/llm/test", json={"provider": "openai"})
        assert resp.status_code == 200
        assert resp.json() == {"success": True, "message": "pong"}

    def test_cover_letter_requires_description(self, auth_client):
        """/cover-letter/generate validates that a job description is present."""
        resp = auth_client.post("/cover-letter/generate", json={"job_title": "Dev"})
        assert resp.status_code == 400
        assert resp.json()["error"]["code"] == "missing_field"

    def test_cover_letter_requires_llm_configured(self, auth_client):
        """With no LLM provider configured the endpoint errors clearly (no network)."""
        # Persist an LLM settings file with an empty provider.
        auth_client.put("/settings/llm", json={"provider": ""})
        resp = auth_client.post(
            "/cover-letter/generate",
            json={"job_description": "Build things.", "job_title": "Dev", "company": "Acme"},
        )
        assert resp.status_code == 400
        assert resp.json()["error"]["code"] == "no_llm"

    def test_cover_letter_success_mocked(self, auth_client, monkeypatch):
        """Happy path: a configured + mocked LLM yields a cover letter string."""
        auth_client.put(
            "/settings/llm",
            json={"provider": "openai", "openai": {"api_key": "sk", "model": "gpt-4o"}},
        )

        class _Resp:
            content = "Dear Hiring Manager, ..."

        class _LLM:
            async def ainvoke(self, _messages):
                return _Resp()

        from core import llm_factory

        monkeypatch.setattr(llm_factory, "create_llm", lambda s: _LLM())
        resp = auth_client.post(
            "/cover-letter/generate",
            json={"job_description": "Build things.", "job_title": "Dev", "company": "Acme"},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["success"] is True
        assert body["cover_letter"].startswith("Dear Hiring Manager")


# ── Resume tailoring: validation branch (no LLM / no tailoring invoked) ───────
class TestResumeTailor:
    def test_tailor_requires_job_urls(self, auth_client):
        resp = auth_client.post("/resume/tailor", json={})
        assert resp.status_code == 400
        assert resp.json()["error"]["code"] == "missing_field"

    def test_tailor_refine_requires_job_url(self, auth_client):
        resp = auth_client.post("/resume/tailor/refine", json={"instruction": "shorter"})
        assert resp.status_code == 400
        assert resp.json()["error"]["code"] == "missing_field"

    def test_get_tailored_by_url_requires_url(self, auth_client):
        resp = auth_client.post("/resume/tailor/get-by-url", json={})
        assert resp.status_code == 400
        assert resp.json()["error"]["code"] == "missing_field"

    def test_get_tailored_by_hash_not_found(self, auth_client):
        resp = auth_client.get("/resume/tailor/deadbeef")
        assert resp.status_code == 404
        assert resp.json()["error"]["code"] == "not_found"

    def test_tailor_unknown_job_reported_per_url(self, auth_client):
        """tailor returns per-URL error status for jobs not in the queue (no LLM call)."""
        resp = auth_client.post(
            "/resume/tailor", json={"job_urls": ["https://example.com/missing"]}
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["success"] is True
        assert body["results"][0]["status"] == "error"
        assert body["results"][0]["message"] == "Job not found"


# ── Job-collection / apply request validation (no worker thread started) ──────
class TestCollectApplyValidation:
    def test_collect_refused_when_the_switch_is_off(self, auth_client):
        """Turning collection off must stop it at the API, not just in the UI."""
        auth_client.put("/settings", json={"job_collection_enabled": False})
        resp = auth_client.post("/jobs/collect", json={"title": "Engineer"})
        assert resp.status_code == 409
        assert resp.json()["error"]["code"] == "collection_disabled"

    def test_collect_switch_defaults_to_enabled(self, auth_client):
        """With no setting written, collection behaves as it always has."""
        resp = auth_client.post("/jobs/collect", json={"max_jobs": 10000})
        # Rejected for the max_jobs bound, not for being disabled.
        assert resp.status_code == 422

    def test_bulk_add_still_works_when_collection_is_off(self, auth_client):
        """The switch gates board scraping only; pasting links is unaffected."""
        auth_client.put("/settings", json={"job_collection_enabled": False})
        resp = auth_client.post("/jobs/add-bulk", json={"text": "https://jobs.example.com/a"})
        assert resp.status_code == 200
        assert resp.json()["added"] == 1

    def test_collect_rejects_out_of_range_max_jobs(self, auth_client):
        """max_jobs > 500 violates the pydantic CollectRequest model -> 422."""
        resp = auth_client.post("/jobs/collect", json={"max_jobs": 10000})
        assert resp.status_code == 422

    def test_apply_rejects_invalid_mode(self, auth_client):
        """An unknown apply mode is rejected by the Literal field -> 422."""
        resp = auth_client.post("/apply/start", json={"mode": "telepathy"})
        assert resp.status_code == 422

    def test_apply_rejects_too_many_workers(self, auth_client):
        resp = auth_client.post("/apply/start", json={"mode": "easy", "workers": 99})
        assert resp.status_code == 422


# ── Generic 404 ───────────────────────────────────────────────────────────────
class TestNotFound:
    def test_unknown_route_404(self, auth_client):
        resp = auth_client.get("/this/route/does/not/exist")
        assert resp.status_code == 404

    def test_ollama_models_unreachable_server(self, auth_client):
        """/llm/ollama-models gracefully reports failure for an unreachable host."""
        resp = auth_client.post(
            "/llm/ollama-models", json={"base_url": "http://127.0.0.1:1"}
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["success"] is False
        assert body["models"] == []


# ── Sidecar lifecycle helpers (Windows orphan / stale-port fixes) ────────────

class _FakeProc:
    def __init__(self, pid, name, cmdline=(), ppid=1, parent=None):
        self.pid, self._name, self._cmdline, self._ppid, self._parent = pid, name, list(cmdline), ppid, parent
        self.killed = False

    def name(self):
        return self._name

    def cmdline(self):
        return self._cmdline

    def ppid(self):
        return self._ppid

    def parent(self):
        return self._parent

    def kill(self):
        self.killed = True


@pytest.mark.parametrize(
    "name,cmdline,expected",
    [
        ("langhirev2-backend-x86_64-pc-windows-msvc.exe", [], True),
        ("python.exe", ["python", "backend/main.py", "8743"], True),
        ("python.exe", ["python", "some_other_server.py"], False),
        ("nginx.exe", [], False),
    ],
)
def test_is_backend_process(name, cmdline, expected):
    assert main._is_backend_process(_FakeProc(10, name, cmdline)) is expected


def test_watched_parent_skips_pyinstaller_bootloader(monkeypatch):
    """Frozen one-file build: watch the Tauri app, not our same-named bootloader."""
    import psutil
    procs = {
        None: _FakeProc(300, "langhirev2-backend.exe", ppid=200),  # us
        200: _FakeProc(200, "langhirev2-backend.exe", ppid=100),   # bootloader
    }
    monkeypatch.setattr(main.sys, "frozen", True, raising=False)
    monkeypatch.setattr(main.os, "getppid", lambda: 200)
    monkeypatch.setattr(psutil, "Process", lambda pid=None: procs[pid])
    assert main._watched_parent_pid() == 100


def test_watched_parent_unfrozen_is_direct_parent(monkeypatch):
    monkeypatch.setattr(main.sys, "frozen", False, raising=False)
    monkeypatch.setattr(main.os, "getppid", lambda: 4242)
    assert main._watched_parent_pid() == 4242


def test_kill_stale_backend_only_kills_backends(monkeypatch):
    import psutil
    from types import SimpleNamespace as NS
    boot = _FakeProc(50, "langhirev2-backend.exe")
    stale = _FakeProc(51, "langhirev2-backend.exe", parent=boot)
    other = _FakeProc(60, "someserver.exe")
    me = _FakeProc(99, "langhirev2-backend.exe", ppid=98)
    table = {51: stale, 60: other, None: me}
    conns = [
        NS(laddr=NS(port=8743), status=psutil.CONN_LISTEN, pid=51),
        NS(laddr=NS(port=8743), status=psutil.CONN_LISTEN, pid=60),
        NS(laddr=NS(port=9999), status=psutil.CONN_LISTEN, pid=61),
    ]
    monkeypatch.setattr(psutil, "net_connections", lambda kind: conns)
    monkeypatch.setattr(psutil, "Process", lambda pid=None: table[pid])
    monkeypatch.setattr(psutil, "wait_procs", lambda procs, timeout: ([], []))
    main._kill_stale_backend(8743)
    assert stale.killed and boot.killed
    assert not other.killed


def test_kill_browser_processes_matches_windows_profile_paths(monkeypatch):
    """Chrome launched with a backslash Windows profile path must still be stopped."""
    import psutil

    class P:
        def __init__(self, pid, cmdline):
            self.pid, self.info, self.terminated = pid, {"pid": pid, "name": "chrome.exe", "cmdline": cmdline}, False

        def terminate(self):
            self.terminated = True

        def kill(self):
            pass

    profile = str(main.get_data_dir() / "browser_profile")
    windows_style = profile.replace("/", "\\").upper()
    ours = P(1, ["chrome.exe", f"--user-data-dir={windows_style}"])
    worker = P(2, ["brave.exe", f'--user-data-dir="{profile}_brave_w2"'])
    unrelated = P(3, ["chrome.exe", r"--user-data-dir=C:\Users\x\AppData\Local\Google\Chrome\User Data"])
    # The real app's profile (another data folder): a test or a second copy must never close it.
    other_app = P(4, ["chrome.exe", r"--user-data-dir=C:\Users\x\AppData\Roaming\LangHireV2\browser_profile"])
    mentions_it = P(5, ["notepad.exe", profile + "\\notes.txt"])
    monkeypatch.setattr(psutil, "process_iter", lambda attrs: [ours, worker, unrelated, other_app, mentions_it])
    monkeypatch.setattr(psutil, "wait_procs", lambda procs, timeout: (procs, []))
    main._kill_browser_processes()
    assert ours.terminated and worker.terminated
    assert not unrelated.terminated and not other_app.terminated and not mentions_it.terminated


@pytest.mark.parametrize("cleanup_fails", [False, True])
def test_when_the_app_exits_the_backend_stops_runs_and_closes_browsers_before_ending(monkeypatch, cleanup_fails):
    """The SIGTERM the parent watchdog sends itself ends the process without lifespan shutdown on
    Windows (TerminateProcess) and in the packaged app (no signal handlers), so the app's browsers
    stayed open after the app closed (reproduced by running main.py with sys.frozen set, killing its
    parent, and watching a process on the app's profile folder outlive the backend)."""
    import signal
    import time
    order = []

    def kill_browsers():
        order.append("browsers")
        if cleanup_fails:
            raise RuntimeError("psutil failed")

    monkeypatch.setattr(main, "_kill_browser_processes", kill_browsers)
    monkeypatch.setattr(time, "sleep", lambda seconds: order.append(f"sleep {seconds}"))
    monkeypatch.setattr(main.os, "kill", lambda pid, sig: order.append(("kill", pid, sig)))
    monkeypatch.setitem(main._apply_status, "running", True)
    monkeypatch.setitem(main._apply_status, "log", [])
    monkeypatch.setitem(main._apply_status, "_loop", None)
    monkeypatch.setitem(main._apply_status, "cancel_requested", False)
    if cleanup_fails:
        with pytest.raises(RuntimeError):
            main._on_parent_exit()
    else:
        main._on_parent_exit()
    assert main._apply_status.get("cancel_requested") is True
    assert order[0] == "browsers" and order[-1] == ("kill", os.getpid(), signal.SIGTERM)
    if not cleanup_fails:
        assert order == ["browsers", "sleep 2", "browsers", ("kill", os.getpid(), signal.SIGTERM)]


def test_when_the_app_exits_an_open_sign_in_window_is_left_for_the_user(monkeypatch):
    """A forced close can lose a sign-in not yet saved to disk; no run goes on while it is open."""
    import signal
    killed = []

    class OpenWindow:
        def poll(self):
            return None

    monkeypatch.setattr(main, "_login_process", OpenWindow())
    monkeypatch.setattr(main, "_kill_browser_processes", lambda: killed.append("browsers"))
    monkeypatch.setattr(main.os, "kill", lambda pid, sig: killed.append(sig))
    main._on_parent_exit()
    assert killed == [signal.SIGTERM]


class TestApplyPause:
    @pytest.fixture(autouse=True)
    def _unpaused(self):
        from cli import apply_jobs
        yield
        apply_jobs.set_paused(False)
        main._apply_status["running"] = False

    def test_pause_when_nothing_runs_does_nothing(self, auth_client):
        from cli import apply_jobs
        assert auth_client.post("/apply/pause").json() == {"success": False, "paused": False}
        assert not apply_jobs.is_paused()

    def test_pause_and_resume_a_running_session(self, auth_client):
        from cli import apply_jobs
        main._apply_status.update({"running": True, "log": []})
        assert auth_client.post("/apply/pause").json()["paused"] is True
        body = auth_client.get("/apply/status").json()
        assert body["paused"] is True and "Paused" in body["log"][-1]
        assert auth_client.post("/apply/resume").json()["paused"] is False
        assert auth_client.get("/apply/status").json()["paused"] is False
        assert not apply_jobs.is_paused()

    def test_stop_clears_the_pause(self, auth_client, monkeypatch):
        from cli import apply_jobs
        monkeypatch.setattr(main, "_kill_browser_processes", lambda: None)
        main._apply_status.update({"running": True, "log": []})
        auth_client.post("/apply/pause")
        auth_client.post("/apply/stop")
        assert not apply_jobs.is_paused()


def test_cover_letter_prompt_includes_resume_and_major(auth_client, monkeypatch, tmp_path):
    """The cover letter used to see only name/role/skills, never the resume."""
    import fitz
    pdf = tmp_path / "r.pdf"
    doc = fitz.open()
    doc.new_page().insert_text((72, 72), "CORNER MARKET | Cashier 2023")
    doc.save(str(pdf))
    doc.close()
    auth_client.put("/settings", json={"resume_path": str(pdf)})
    auth_client.put("/profile", json={"name": "Ada", "education": {"degree": "BS", "major": "Computer Science", "school": "State University", "graduation": "2028"}})
    auth_client.put("/settings/llm", json={"provider": "openai", "openai": {"api_key": "sk", "model": "gpt-4o"}})
    prompts = []

    class _LLM:
        async def ainvoke(self, messages):
            prompts.append(messages[0].content)
            return type("R", (), {"content": "Dear team"})()

    from core import llm_factory
    monkeypatch.setattr(llm_factory, "create_llm", lambda s: _LLM())
    auth_client.post("/cover-letter/generate", json={"job_description": "Build.", "job_title": "Dev", "company": "Acme"})
    assert "CORNER MARKET | Cashier" in prompts[0]
    assert "BS in Computer Science from State University (2028)" in prompts[0]


class TestApplyReview:
    def test_review_decision_reaches_the_waiting_application(self, auth_client, monkeypatch):
        from cli import apply_jobs
        decided = []
        monkeypatch.setattr(apply_jobs, "decide_review", lambda url, approve: decided.append((url, approve)) or True)
        main._apply_status.update({"running": True, "log": []})
        try:
            assert auth_client.post("/apply/review", json={"url": "https://x.example/1", "approve": True}).json() == {"success": True}
            assert decided == [("https://x.example/1", True)]
            assert "Approved" in main._apply_status["log"][-1]
        finally:
            main._apply_status["running"] = False

    def test_status_lists_waiting_reviews(self, auth_client, monkeypatch):
        from cli import apply_jobs
        pending = [{"url": "u", "title": "Intern", "company": "Acme", "summary": "s"}]
        monkeypatch.setattr(apply_jobs, "pending_reviews", lambda: pending)
        main._apply_status.update({"running": True, "log": []})
        try:
            assert auth_client.get("/apply/status").json()["reviews"] == pending
        finally:
            main._apply_status["running"] = False

    def test_a_failed_start_releases_the_running_claim(self, auth_client, monkeypatch):
        def broken():
            raise RuntimeError("couldn't list processes")

        monkeypatch.setattr(main, "_kill_browser_processes", broken)
        main._apply_status["running"] = False
        with pytest.raises(RuntimeError):
            auth_client.post("/apply/start", json={"mode": "all"})
        assert main._apply_status["running"] is False

    def test_a_run_whose_thread_doesnt_start_releases_the_claim(self, auth_client, monkeypatch):
        monkeypatch.setattr(main, "_kill_browser_processes", lambda: None)

        def no_thread(make_coro, status):
            raise RuntimeError("can't start new thread")

        monkeypatch.setattr(main, "_run_async_in_thread", no_thread)
        main._apply_status["running"] = False
        with pytest.raises(RuntimeError):
            auth_client.post("/apply/start", json={"mode": "all"})
        assert main._apply_status["running"] is False

    def test_collection_waits_for_a_gmail_check(self, auth_client, monkeypatch):
        from core import inbox
        killed = []
        monkeypatch.setattr(main, "_kill_browser_processes", lambda: killed.append(1))
        monkeypatch.setitem(inbox._state, "checking", True)
        resp = auth_client.post("/jobs/collect", json={"title": "Intern"}).json()
        assert resp["success"] is False and "Inbox" in resp["message"] and killed == []

    def test_start_uses_the_jobs_at_the_same_time_setting(self, auth_client, monkeypatch):
        monkeypatch.setattr(main, "_kill_browser_processes", lambda: None)
        monkeypatch.setattr(main, "_run_async_in_thread", lambda make_coro, status: None)
        main.save_settings({**main.load_settings(), "parallel_jobs": 3})
        try:
            auth_client.post("/apply/start", json={"mode": "all"})
            assert main._apply_status["workers"] == 3
            main._apply_status["running"] = False
            auth_client.post("/apply/start", json={"mode": "all", "workers": 2})   # an explicit number wins
            assert main._apply_status["workers"] == 2
        finally:
            main._apply_status["running"] = False

    def test_start_passes_the_trial_run_choice(self, auth_client, monkeypatch):
        from cli import apply_jobs
        monkeypatch.setattr(main, "_kill_browser_processes", lambda: None)
        monkeypatch.setattr(main, "_run_async_in_thread", lambda make_coro, status: None)
        auth_client.post("/apply/start", json={"mode": "all", "review": True})
        assert apply_jobs.is_trial_run()
        main._apply_status["running"] = False
        auth_client.post("/apply/start", json={"mode": "all"})
        assert apply_jobs.is_trial_run()  # default never grants automatic sending
        main._apply_status["running"] = False
        auth_client.post("/apply/start", json={"mode": "all", "run_mode": "auto"})
        assert not apply_jobs.is_trial_run()  # only an explicit mode choice
        main._apply_status["running"] = False


class TestHelpAndNotes:
    @pytest.mark.parametrize("body", [
        {"ids": ["a" * 32], "approve": "false"},
        {"ids": [], "approve": True},
        {"ids": ["a" * 32] * 65, "approve": True},
        {"ids": "a" * 32, "approve": True},
    ])
    def test_browser_request_decisions_reject_ambiguous_types(self, auth_client, body):
        assert auth_client.post("/apply/requests/decide", json=body).status_code == 400

    def test_no_request_id_can_authorize_a_future_request(self, auth_client):
        result = auth_client.post("/apply/requests/decide", json={"ids": ["a" * 32], "approve": True})
        assert result.status_code == 200 and result.json() == {"success": False}

    def test_collection_requests_are_visible_without_an_application_run(self, auth_client, monkeypatch):
        from core import request_gate
        monkeypatch.setattr(request_gate, "pending", lambda: [{"id": "a" * 32, "kind": "request"}])
        main._apply_status["running"] = False
        assert auth_client.get("/apply/status").json()["requests"] == [{"id": "a" * 32, "kind": "request"}]

    def test_retry_requires_explicit_duplicate_acknowledgement(self, auth_client, monkeypatch):
        from core import shared_config
        url = "https://jobs.example/1"
        shared_config.write_jobs({url: {"url": url, "status": "needs_you", "submit_attempted_at": "test-earlier-request"}})
        started = []
        monkeypatch.setattr(main, "_kill_browser_processes", lambda: None)
        monkeypatch.setattr(main, "_run_async_in_thread", lambda *a: started.append(True))
        result = auth_client.post("/apply/start", json={"job_url": url})
        assert not result.json()["success"] and not started
        assert auth_client.post("/apply/start", json={"job_url": url, "acknowledge_unknown": "true"}).status_code == 422
        try:
            result = auth_client.post("/apply/start", json={"job_url": url, "acknowledge_unknown": True})
            assert result.json()["success"] and started
        finally:
            main._apply_status["running"] = False

    def test_only_an_explicit_manual_status_change_records_user_receipt_confirmation(self, auth_client):
        from core import shared_config
        url = "https://jobs.example/1"
        shared_config.write_jobs({url: {"url": url, "status": "needs_you", "submit_attempted_at": "test-request"}})
        assert auth_client.put("/jobs/status", json={"url": url, "status": "applied"}).json()["success"]
        saved = shared_config.read_jobs()[url]
        assert saved["submission_evidence"]["kind"] == "user_confirmed" and saved["submit_attempted_at"] == "test-request"

    def test_help_reply_reaches_the_waiting_agent(self, auth_client, monkeypatch):
        from cli import apply_jobs
        replies = []
        monkeypatch.setattr(apply_jobs, "answer_help", lambda url, msg, give_up: replies.append((url, msg, give_up)) or True)
        main._apply_status.update({"running": True, "log": []})
        try:
            body = {"url": "u", "message": "Tick the box", "give_up": False}
            assert auth_client.post("/apply/help", json=body).json() == {"success": True}
            assert replies == [("u", "Tick the box", False)]
            assert "Continuing" in main._apply_status["log"][-1]
        finally:
            main._apply_status["running"] = False

    def test_status_lists_help_requests(self, auth_client, monkeypatch):
        from cli import apply_jobs
        pending = [{"url": "u", "title": "Intern", "company": "Cyberdyne", "problem": "stuck"}]
        monkeypatch.setattr(apply_jobs, "pending_help", lambda: pending)
        main._apply_status.update({"running": True, "log": []})
        try:
            assert auth_client.get("/apply/status").json()["help"] == pending
        finally:
            main._apply_status["running"] = False

    def test_note_is_saved_on_the_job(self, auth_client):
        auth_client.post("/jobs/add", json={"url": "https://boards.greenhouse.io/acme/jobs/9", "title": "Intern", "company": "Acme"})
        url = "https://boards.greenhouse.io/acme/jobs/9"
        assert auth_client.put("/jobs/note", json={"url": url, "note": "  Tick the box  "}).json() == {"success": True}
        job = next(j for j in auth_client.get("/jobs").json() if j["url"] == url)
        assert job["user_note"] == "Tick the box"

    def test_note_for_an_unknown_job(self, auth_client):
        assert auth_client.put("/jobs/note", json={"url": "https://nope.example/1", "note": "x"}).status_code == 404

    def test_notes_stay_on_one_job_and_are_never_silently_truncated(self, auth_client):
        first, second = "https://boards.greenhouse.io/acme/jobs/9", "https://boards.greenhouse.io/acme/jobs/10"
        for url in (first, second):
            auth_client.post("/jobs/add", json={"url": url, "title": "Intern", "company": "Acme"})
        note = "A" * 2500 + "Keep this final instruction"
        assert auth_client.put("/jobs/note", json={"url": first, "note": note}).status_code == 200
        assert auth_client.put("/jobs/note", json={"url": second, "note": "Other job"}).status_code == 200
        assert auth_client.put("/jobs/note", json={"url": first, "note": "x" * 10001}).status_code == 400
        jobs = {j["url"]: j for j in auth_client.get("/jobs").json()}
        assert jobs[first]["user_note"] == note and jobs[second]["user_note"] == "Other job"
        assert auth_client.put("/jobs/note", json={"url": first, "note": ""}).status_code == 200
        jobs = {j["url"]: j for j in auth_client.get("/jobs").json()}
        assert jobs[first]["user_note"] == "" and jobs[second]["user_note"] == "Other job"


def test_start_passes_the_run_mode(auth_client, monkeypatch):
    from cli import apply_jobs
    monkeypatch.setattr(main, "_kill_browser_processes", lambda: None)
    monkeypatch.setattr(main, "_run_async_in_thread", lambda make_coro, status: None)
    try:
        auth_client.post("/apply/start", json={"mode": "all", "run_mode": "training"})   # the old name of a trial
        assert apply_jobs.is_trial_run() and apply_jobs._run_options["mode"] == "trial"
        main._apply_status["running"] = False
        auth_client.post("/apply/start", json={"mode": "all", "review": True})   # older callers
        assert apply_jobs.is_trial_run()
    finally:
        main._apply_status["running"] = False
        apply_jobs.set_run_mode("auto")


# ── New statuses, CSV export, posting details, sign-in window ──────────────
class TestFailureFixes:
    def test_stats_count_closed_and_needs_you(self, auth_client):
        import core.shared_config as shared_config
        for i, status in enumerate(["closed", "needs_you", "needs_you"]):
            url = f"https://jobs.example/{i}"
            auth_client.post("/jobs/add", json={"url": url, "title": "Intern"})
            shared_config.update_job(url, status=status)
        stats = auth_client.get("/jobs/stats").json()
        assert (stats["closed"], stats["needs_you"]) == (1, 2)

    @pytest.mark.parametrize("status", ["closed", "needs_you"])
    def test_status_can_be_set_by_hand(self, auth_client, status):
        url = "https://jobs.example/hand"
        auth_client.post("/jobs/add", json={"url": url})
        assert auth_client.put("/jobs/status", json={"url": url, "status": status}).json()["status"] == status

    def test_needs_you_jobs_can_be_retried(self):
        assert "needs_you" in main.RETRYABLE_STATUSES and "closed" not in main.RETRYABLE_STATUSES

    def test_csv_has_bom_new_columns_and_safe_cells(self, auth_client):
        import core.shared_config as shared_config
        url = "https://jobs.example/csv"
        auth_client.post("/jobs/add", json={"url": url, "title": "=HYPERLINK(\"x\")", "company": "Café Ltd"})
        shared_config.update_job(url, status="failed", error="@Create Account blocked", practised_at="2026-10-04T10:00:00Z")
        resp = auth_client.get("/jobs/export")
        assert resp.content.startswith("\ufeff".encode("utf-8"))
        text = resp.content.decode("utf-8-sig")
        header = text.splitlines()[0]
        for column in ("Last Attempt", "Practised (Skipped at Review)", "Reason", "Duration (s)", "Steps"):
            assert column in header
        assert "'=HYPERLINK" in text and "'@Create Account" in text and "Café Ltd" in text

    def test_csv_includes_the_last_runs_time_and_duration(self, auth_client):
        url = "https://jobs.example/ran"
        auth_client.post("/jobs/add", json={"url": url, "title": "Intern"})
        metrics = main._get_metrics_store()
        from datetime import datetime, timezone
        metrics.record_run(job_url=url, job_title="Intern", company="Acme", website_domain="jobs.example",
                           ats_platform=None, success=False,
                           started_at=datetime(2026, 10, 4, 14, 0, 0, tzinfo=timezone.utc),
                           finished_at=datetime(2026, 10, 4, 14, 2, 0, 400000, tzinfo=timezone.utc), step_count=17)
        row = [r for r in auth_client.get("/jobs/export").content.decode("utf-8-sig").splitlines() if url in r][0]
        assert row.startswith("2026-10-04T14:00:00+00:00") and ",120,17," in row

    def test_save_csv_to_a_chosen_path(self, auth_client, tmp_path):
        target = tmp_path / "out" / "jobs.csv"
        resp = auth_client.post("/jobs/export/save", json={"path": str(target)})
        assert resp.json()["success"] and target.read_bytes().startswith("\ufeff".encode("utf-8"))

    def test_save_csv_needs_a_csv_name(self, auth_client, tmp_path):
        resp = auth_client.post("/jobs/export/save", json={"path": str(tmp_path / "jobs.exe")})
        assert resp.status_code == 400

    def test_pasted_links_get_real_titles_and_closed_postings(self, auth_client, monkeypatch):
        import core.job_page as job_page
        import core.shared_config as shared_config
        pages = {"https://jobs.example/a": job_page.Posting(title="Finance Intern", company="Northwind"),
                 "https://jobs.example/b": job_page.Posting(closed=True, reason="The posting is gone (HTTP 404).")}
        monkeypatch.setattr(job_page, "fetch", lambda url: pages.get(url))
        for url in pages:
            shared_config.refresh_posting_details  # noqa: B018 (imported for the patch above)
        auth_client.post("/jobs/add-bulk", json={"text": "https://jobs.example/a\nhttps://jobs.example/b"})
        # The background task ran on the TestClient's loop; run it directly too, to be deterministic.
        for url in pages:
            shared_config.refresh_posting_details(url)
        jobs = shared_config.read_jobs()
        assert (jobs["https://jobs.example/a"]["title"], jobs["https://jobs.example/a"]["company"]) == ("Finance Intern", "Northwind")
        assert jobs["https://jobs.example/b"]["status"] == "closed"

    def test_a_real_title_is_never_overwritten(self, auth_client, monkeypatch):
        import core.job_page as job_page
        import core.shared_config as shared_config
        monkeypatch.setattr(job_page, "fetch", lambda url: job_page.Posting(title="Other", company="Other Co"))
        auth_client.post("/jobs/add", json={"url": "https://jobs.example/c", "title": "Mine", "company": "My Co"})
        shared_config.refresh_posting_details("https://jobs.example/c")
        job = shared_config.read_jobs()["https://jobs.example/c"]
        assert (job["title"], job["company"]) == ("Mine", "My Co")

    def test_sign_in_window_blocks_applying_and_shows_in_status(self, auth_client, monkeypatch):
        class Running:
            def poll(self):
                return None

        monkeypatch.setattr(main, "_login_process", Running())
        assert auth_client.get("/auth/status").json()["login_window_open"] is True
        resp = auth_client.post("/apply/start", json={"mode": "all"}).json()
        assert resp["success"] is False and "sign-in window" in resp["message"]
        resp = auth_client.post("/auth/login/gmail").json()
        assert resp["success"] is False and "already open" in resp["message"]

    def test_sign_in_opens_a_plain_browser_with_the_agent_profile(self, auth_client, monkeypatch):
        launched = {}

        class Proc:
            def poll(self):
                return None

        def fake_popen(args, **kwargs):
            launched["args"] = args
            return Proc()

        monkeypatch.setattr(main, "_login_process", None)
        monkeypatch.setattr(main.subprocess, "Popen", fake_popen)
        monkeypatch.setattr(main, "_find_playwright_chromium", lambda: "C:/chromium/chrome.exe")
        resp = auth_client.post("/auth/login/gmail").json()
        assert resp["success"] is True
        args = launched["args"]
        assert args[0] == "C:/chromium/chrome.exe"
        assert any(a.startswith("--user-data-dir=") for a in args)
        assert args[-1].startswith("https://accounts.google.com/")
        assert not any("remote-debugging" in a or "enable-automation" in a for a in args)



class TestBatch1Api:
    def test_status_reports_current_job_and_mode(self, auth_client, monkeypatch):
        from cli import apply_jobs
        monkeypatch.setitem(main._apply_status, "running", True)
        monkeypatch.setattr(apply_jobs, "_current", {1: {"url": "u", "title": "Intern", "company": "Acme", "step": 4,
                                                         "max_steps": 70, "started_at": "2026-10-08T10:00:00+00:00"}})
        monkeypatch.setitem(apply_jobs._run_options, "mode", "trial")
        status = auth_client.get("/apply/status").json()
        assert status["current"][0]["title"] == "Intern" and status["current"][0]["worker"] == 1
        assert status["run_mode"] == "trial"

    def test_skip_endpoint(self, auth_client, monkeypatch):
        from cli import apply_jobs
        monkeypatch.setitem(main._apply_status, "running", True)
        monkeypatch.setitem(main._apply_status, "log", [])
        monkeypatch.setattr(apply_jobs, "skip_job", lambda url: url == "u")
        assert auth_client.post("/apply/skip", json={"url": "u"}).json()["success"] is True
        assert auth_client.post("/apply/skip", json={"url": "x"}).json()["success"] is False

    def test_interrupted_jobs_get_a_note(self, auth_client):
        import core.shared_config as shared_config
        auth_client.post("/jobs/add", json={"url": "https://jobs.example/i", "title": "Intern"})
        shared_config.update_job("https://jobs.example/i", status="in_progress")
        main._recover_stale_jobs()
        job = shared_config.read_jobs()["https://jobs.example/i"]
        assert job["status"] == "pending" and job["error"] == main.INTERRUPTED_NOTE

    def test_typed_names_are_the_users(self, auth_client):
        import core.shared_config as shared_config
        auth_client.post("/jobs/add", json={"url": "https://jobs.example/t", "title": "My Title"})
        auth_client.post("/jobs/add", json={"url": "https://jobs.example/b", "title": "My Title", "company": "Acme"})
        auth_client.post("/jobs/add", json={"url": "https://jobs.example/n"})
        jobs = shared_config.read_jobs()
        # Only the typed field is the user's; the company can still be filled in.
        assert jobs["https://jobs.example/t"]["user_named"] == ["title"] and "name_source" not in jobs["https://jobs.example/t"]
        assert jobs["https://jobs.example/b"]["name_source"] == "user"
        assert "name_source" not in jobs["https://jobs.example/n"] and "user_named" not in jobs["https://jobs.example/n"]

    def test_skipped_status_counts_and_can_be_set(self, auth_client):
        auth_client.post("/jobs/add", json={"url": "https://jobs.example/s"})
        assert auth_client.put("/jobs/status", json={"url": "https://jobs.example/s", "status": "skipped"}).json()["status"] == "skipped"
        assert auth_client.get("/jobs/stats").json()["skipped"] == 1
        assert "skipped" in main.RETRYABLE_STATUSES



class TestInboxApi:
    @pytest.fixture(autouse=True)
    def _inbox_dir(self, data_dir, monkeypatch):
        from core import inbox
        monkeypatch.setattr(inbox, "get_data_dir", lambda: data_dir)
        inbox._state.update(checking=False, requested=False, status="", error="")

    def test_get_inbox(self, auth_client):
        data = auth_client.get("/inbox").json()
        assert data["emails"] == [] and data["schedule"] == "off" and data["performance_mode"] is False
        assert data["busy"] is False

    def test_check_needs_a_model_then_queues(self, auth_client, monkeypatch):
        monkeypatch.setattr(main, "load_llm_settings", lambda: {})
        assert auth_client.post("/inbox/check").status_code == 400
        monkeypatch.setattr(main, "load_llm_settings", lambda: {"provider": "chatgpt"})
        assert auth_client.post("/inbox/check").json()["success"] is True
        from core import inbox
        assert inbox._state["requested"] is True

    def test_schedule(self, auth_client):
        assert auth_client.put("/inbox/schedule", json={"schedule": "daily"}).json()["schedule"] == "daily"
        assert auth_client.get("/inbox").json()["schedule"] == "daily"
        assert auth_client.put("/inbox/schedule", json={"schedule": "hourly"}).status_code == 400

    def test_busy_during_a_run_or_sign_in(self, monkeypatch):
        monkeypatch.setitem(main._apply_status, "running", True)
        assert main._inbox_busy() is True
        monkeypatch.setitem(main._apply_status, "running", False)
        monkeypatch.setattr(main, "_login_process", None)
        assert main._inbox_busy() is False

    def test_prep_sheet_is_off_in_performance_mode(self, auth_client, monkeypatch):
        from core import inbox
        inbox.save({"emails": [{"id": "x", "category": "interview", "company": "Stark", "role": "Quant", "text": "call"}],
                    "schedule": "off", "last_checked": None})
        monkeypatch.setattr(main, "load_settings", lambda: {"performance_mode": True})
        assert auth_client.post("/inbox/prep", json={"id": "x"}).status_code == 400

    def test_prep_sheet_is_written_and_stored(self, auth_client, monkeypatch):
        from core import inbox
        inbox.save({"emails": [{"id": "x", "category": "interview", "company": "Stark", "role": "Quant", "text": "call"}],
                    "schedule": "off", "last_checked": None})
        monkeypatch.setattr(main, "load_settings", lambda: {})
        monkeypatch.setattr(main, "load_llm_settings", lambda: {"provider": "chatgpt"})

        async def sheet(*a, **k):
            return "THE INTERVIEW\n- 30 minutes"

        monkeypatch.setattr(inbox, "write_prep_sheet", sheet)
        import core.llm_factory as lf
        monkeypatch.setattr(lf, "create_llm", lambda s: object())
        resp = auth_client.post("/inbox/prep", json={"id": "x"}).json()
        assert resp["prep"] == "THE INTERVIEW\n- 30 minutes" and inbox.find("x")["prep"] == resp["prep"]
        assert auth_client.post("/inbox/prep", json={"id": "nope"}).status_code == 404

    def test_no_run_or_sign_in_while_gmail_is_being_checked(self, auth_client, monkeypatch):
        from core import inbox
        monkeypatch.setitem(inbox._state, "checking", True)
        monkeypatch.setattr(main, "_login_process", None)
        resp = auth_client.post("/apply/start", json={"mode": "all"}).json()
        assert resp["success"] is False and "checking Gmail" in resp["message"]
        resp = auth_client.post("/auth/login/gmail").json()
        assert resp["success"] is False and "checking Gmail" in resp["message"]


class TestQaMergeValidation:
    def _add(self, question, answer=""):
        store = main._get_memory_store()
        store.qa_add(question=question, answer=answer)
        return next(q["id"] for q in store.qa_list() if q["question"] == question)

    def test_bad_manual_merges_are_refused_with_a_reason(self, auth_client):
        a = self._add("Are you willing to relocate?", "Yes")
        b = self._add("Do you need sponsorship?", "No")
        assert auth_client.post(f"/qa/{a}/merge/{a}").status_code == 409
        assert auth_client.post(f"/qa/{a}/merge/999999").status_code == 409
        resp = auth_client.post(f"/qa/{b}/merge/{a}")
        assert resp.status_code == 409 and "different answers" in resp.json()["error"]["message"]
        assert auth_client.post(f"/qa/{b}/merge/{a}?force=true").json()["success"] is True

    def test_ai_merge_suggestions_are_checked_before_anything_changes(self, auth_client, monkeypatch):
        a = self._add("Are you legally authorized to work in Canada?", "Yes")
        b = self._add("Are you legally authorized to work in Australia?", "No")
        c = self._add("Do you have the right to work in Canada?", "Yes")
        reply = f'[{{"keep": {a}, "merge": [{a}, {b}, {c}, 424242]}}, {{"keep": 999, "merge": [{c}]}}]'

        async def fake_invoke(llm, settings, messages, **kw):
            from types import SimpleNamespace
            return SimpleNamespace(completion=reply)

        import core.llm_factory as factory
        monkeypatch.setattr(main, "load_llm_settings", lambda: {"provider": "openai"})
        monkeypatch.setattr(factory, "create_llm", lambda settings: object())
        monkeypatch.setattr(factory, "ainvoke_with_fallback", fake_invoke)
        result = auth_client.post("/qa/smart-squash").json()
        # Only c (same answer) merged; the self-merge, the conflicting answer and unknown ids were skipped.
        assert result == {"success": True, "merged": 1, "skipped": 4}
        remaining = {q["id"] for q in main._get_memory_store().qa_list()}
        assert {a, b} <= remaining and c not in remaining


class TestBrowserOwnership:
    """Applying, collecting, a sign-in window and the Gmail check take turns with the browser profiles.

    Starting or stopping one closes every browser on those profiles, so one may
    never start, or stop, while another is using them.
    """

    @pytest.fixture(autouse=True)
    def _idle(self, monkeypatch):
        from core import inbox
        self.killed = []
        monkeypatch.setattr(main, "_kill_browser_processes", lambda: self.killed.append(1))
        monkeypatch.setattr(main, "_run_async_in_thread", lambda make_coro, status: None)
        monkeypatch.setattr(main, "_login_process", None)
        monkeypatch.setitem(inbox._state, "checking", False)
        monkeypatch.setattr(main, "_apply_status", {"running": False, "mode": None, "workers": 1, "log": []})
        monkeypatch.setattr(main, "_collection_status", {"running": False, "title": None, "log": []})

    def test_collection_cannot_start_during_applications(self, auth_client):
        main._apply_status["running"] = True
        resp = auth_client.post("/jobs/collect", json={"title": "Intern"}).json()
        assert resp["success"] is False and "Applications are running" in resp["message"]
        assert self.killed == [] and main._collection_status["running"] is False

    def test_applications_cannot_start_during_collection(self, auth_client):
        main._collection_status["running"] = True
        resp = auth_client.post("/apply/start", json={"mode": "all"}).json()
        assert resp["success"] is False and "collection is running" in resp["message"]
        assert self.killed == [] and main._apply_status["running"] is False

    def test_no_sign_in_window_during_collection(self, auth_client, monkeypatch):
        opened = []
        monkeypatch.setattr(main.subprocess, "Popen", lambda *a, **k: opened.append(a))
        monkeypatch.setattr(main, "_find_playwright_chromium", lambda: "C:/chromium/chrome.exe")
        main._collection_status["running"] = True
        resp = auth_client.post("/auth/login/linkedin").json()
        assert resp["success"] is False and "collection is running" in resp["message"]
        assert opened == []

    def test_collection_cannot_start_while_signing_in(self, auth_client, monkeypatch):
        class Open:
            def poll(self):
                return None

        monkeypatch.setattr(main, "_login_process", Open())
        resp = auth_client.post("/jobs/collect", json={"title": "Intern"}).json()
        assert resp["success"] is False and "sign-in window" in resp["message"] and self.killed == []

    def test_stop_on_an_idle_operation_closes_nothing(self, auth_client, monkeypatch):
        """Stop Collection during an application run used to close the applications' browsers."""
        main._apply_status["running"] = True
        assert auth_client.post("/jobs/collect/stop").json() == {"success": True}
        assert self.killed == [] and not main._apply_status.get("cancel_requested")

        class Open:
            def poll(self):
                return None

        main._apply_status["running"] = False
        monkeypatch.setattr(main, "_login_process", Open())
        auth_client.post("/apply/stop")
        assert self.killed == []  # the sign-in window stays open

    def test_stop_closes_the_running_operations_browsers(self, auth_client):
        main._apply_status["running"] = True
        auth_client.post("/apply/stop")
        assert self.killed == [1] and main._apply_status["cancel_requested"] is True

    def test_the_gmail_check_waits_for_any_user(self, monkeypatch):
        assert main._inbox_busy() is False
        main._collection_status["running"] = True
        assert main._inbox_busy() is True

    def test_a_start_and_the_gmail_check_never_both_go_ahead(self, auth_client, monkeypatch):
        """The Inbox marks itself checking, then asks; a start claims under the same lock."""
        from core import inbox
        checks = []
        inbox._state["requested"] = True
        inbox._check_now(lambda: checks.append(1) or {"new": 0}, is_busy=main._inbox_busy)
        assert checks == [1]  # idle: the check ran
        main._apply_status["running"] = True
        inbox._state["requested"] = True
        inbox._check_now(lambda: checks.append(2) or {"new": 0}, is_busy=main._inbox_busy)
        assert checks == [1] and inbox._state["checking"] is False  # backed off


class TestRunLogs:
    """Each run's log holds only its own lines, under its own run id."""

    def test_two_runs_at_once_keep_their_own_lines(self, monkeypatch, tmp_path):
        import asyncio
        import logging
        import threading
        import memory.metrics as mem_metrics
        db = tmp_path / "m.db"
        monkeypatch.setattr(main, "_get_metrics_store", lambda: mem_metrics.MetricsStore(db))
        monkeypatch.setattr(logging.getLogger("langhire.run_test"), "level", logging.INFO)
        both_started = threading.Barrier(2, timeout=5)
        statuses = [{"log": []}, {"log": []}]

        def factory(name):
            async def run():
                await asyncio.to_thread(both_started.wait)
                for i in range(20):
                    print(f"{name} line {i}")
                    logging.getLogger("langhire.run_test").info(f"{name} record {i}")
                    await asyncio.sleep(0)
            return run

        threads = [main._run_async_in_thread(factory(n), s) for n, s in zip(("alpha", "beta"), statuses)]
        print("from an API request")  # not part of either run
        logging.getLogger("main").info("an API request's log record")
        for t in threads:
            t.join(10)
        for name, other, status in (("alpha", "beta", statuses[0]), ("beta", "alpha", statuses[1])):
            lines = status["log"]
            assert sum(1 for line in lines if line.startswith(f"{name} line")) == 20
            assert sum(1 for line in lines if line.startswith(f"{name} record")) == 20
            assert not any(other in line or "API request" in line for line in lines)
            stored = [r["message"] for r in mem_metrics.MetricsStore(db).get_run_logs(status["run_id"])]
            assert f"{name} line 0" in stored and not any(other in m for m in stored)
        assert statuses[0]["run_id"] != statuses[1]["run_id"]

    def test_a_runs_log_records_are_redacted(self, monkeypatch, tmp_path):
        import logging
        import core.shared_config as shared_config
        import memory.metrics as mem_metrics
        monkeypatch.setattr(main, "_get_metrics_store", lambda: mem_metrics.MetricsStore(tmp_path / "m.db"))
        monkeypatch.setattr(shared_config, "_secret_cache", {"key": None, "values": ()})
        monkeypatch.setattr(logging.getLogger("langhire.run_test"), "level", logging.INFO)
        main.save_settings({"sensitive_data": {"email": "", "password": "fake-run-secret-99"}})
        status = {"log": []}

        async def run():
            logging.getLogger("langhire.run_test").info("typing fake-run-secret-99 now")
            print("printed fake-run-secret-99")

        main._run_async_in_thread(run, status).join(10)
        joined = "\n".join(status["log"])
        assert "fake-run-secret-99" not in joined and joined.count("***") == 2


class TestEmployerPasswords:
    def test_list_and_reveal(self, auth_client):
        from core import secret_store
        password = secret_store.site_password("acme.wd5.myworkdayjobs.com")
        body = auth_client.get("/credentials/sites").json()
        assert body == {"sites": ["acme.wd5.myworkdayjobs.com"]} and password not in str(body)
        shown = auth_client.post("/credentials/reveal", json={"site": "ACME.wd5.myworkdayjobs.com"}).json()
        assert shown == {"site": "acme.wd5.myworkdayjobs.com", "password": password}

    def test_unknown_sites_get_nothing(self, auth_client):
        assert auth_client.post("/credentials/reveal", json={"site": "other.example"}).status_code == 404
        from core import secret_store
        assert secret_store.site_password_sites() == []   # asking never creates one


class TestJobCoverLetter:
    def test_a_letter_is_kept_on_its_job_not_the_profile(self, auth_client):
        url = "https://boards.greenhouse.io/acme/jobs/77"
        auth_client.post("/jobs/add", json={"url": url, "title": "Intern", "company": "Acme"})
        before = main.load_profile().get("cover_letter", "")
        assert auth_client.put("/jobs/cover-letter", json={"url": url, "cover_letter": " Dear Acme "}).json() == {"success": True}
        job = next(j for j in auth_client.get("/jobs").json() if j["url"] == url)
        assert job["cover_letter"] == "Dear Acme"
        assert main.load_profile().get("cover_letter", "") == before

    def test_unknown_job(self, auth_client):
        assert auth_client.put("/jobs/cover-letter", json={"url": "https://nope.example/1", "cover_letter": "x"}).status_code == 404


class TestRequestValidation:
    """Wrong types are a 400 with a reason (TestClient raises on any 500), and nothing is saved."""

    @pytest.mark.parametrize("method, path, body", [
        ("PUT", "/jobs/status", {"url": 5, "status": []}),
        ("PUT", "/jobs/note", {"url": {"a": 1}, "note": "x"}),
        ("PUT", "/jobs/cover-letter", {"url": None}),
        ("POST", "/jobs/add", {"url": ["https://x.example/1"]}),
        ("POST", "/cover-letter/generate", {"job_description": 5}),
        ("POST", "/resume/tailor", {"job_urls": "https://x.example/1"}),
        ("POST", "/resume/tailor", {"job_urls": ["u"], "options": "all"}),
        ("POST", "/resume/tailor/get-by-url", {"job_url": 7}),
    ])
    def test_bad_fields_are_a_400(self, auth_client, method, path, body):
        resp = auth_client.request(method, path, json=body)
        assert resp.status_code == 400, resp.text

    @pytest.mark.parametrize("body", [
        {"max_failures": "abc"}, {"parallel_jobs": True}, {"stealth_enabled": "yes"},
        {"blocked_domains": "spam.com"}, {"sensitive_data": "me"}, {"sensitive_data": {"email": 5}},
    ])
    def test_bad_settings_are_refused_and_nothing_changes(self, auth_client, body):
        before = main.load_settings()
        resp = auth_client.put("/settings", json=body)
        assert resp.status_code == 400 and resp.json()["error"]["code"] == "invalid_settings"
        assert main.load_settings() == before

    def test_numbers_from_number_inputs_are_fine(self, auth_client):
        assert auth_client.put("/settings", json={"human_delay_min": 5.0, "max_failures": 9}).status_code == 200
        assert main.load_settings()["max_failures"] == 9

    @pytest.mark.parametrize("body", [{"address": "1 Main St"}, {"skills": ["Python", 3]}, {"education": []}])
    def test_bad_profile_shapes_are_refused(self, auth_client, body):
        before = main.load_profile()
        resp = auth_client.put("/profile", json={**before, **body})
        assert resp.status_code == 400 and resp.json()["error"]["code"] == "invalid_profile"
        assert main.load_profile() == before

    def test_a_qa_answer_must_be_text(self, auth_client):
        store = main._get_memory_store()
        store.qa_add(question="Years of Python?", answer="")
        qa_id = store.qa_list()[0]["id"]
        assert auth_client.put(f"/qa/{qa_id}", json={"answer": 3}).status_code == 400
        assert auth_client.put(f"/qa/{qa_id}", json={"answer": " 3 "}).status_code == 200
        assert store.qa_list()[0]["answer"] == "3"


class TestResumePathContainment:
    def test_only_paths_inside_home_by_folder(self, monkeypatch, tmp_path):
        home = tmp_path / "ada"
        (home / "docs").mkdir(parents=True)
        (tmp_path / "ada2").mkdir()
        monkeypatch.setattr(main.Path, "home", classmethod(lambda cls: home))
        assert main._inside_home((home / "docs" / "cv.pdf").resolve())
        assert not main._inside_home((tmp_path / "ada2" / "cv.pdf").resolve())   # a text prefix of home
        assert not main._inside_home((home / ".." / "ada2" / "cv.pdf").resolve())

    def test_parse_refuses_a_resume_outside_home(self, auth_client, monkeypatch, tmp_path):
        home = tmp_path / "ada"
        home.mkdir()
        outside = tmp_path / "ada2" / "cv.pdf"
        outside.parent.mkdir()
        outside.write_bytes(b"%PDF-1.4")
        monkeypatch.setattr(main.Path, "home", classmethod(lambda cls: home))
        monkeypatch.setattr(main, "load_settings", lambda: {"resume_path": str(outside)})
        body = auth_client.post("/profile/parse-resume").json()
        assert body["success"] is False and "home directory" in body["message"]


class TestJobsSummary:
    def test_fields_keeps_only_what_was_asked_and_the_url(self, auth_client):
        import core.shared_config as shared_config
        shared_config.write_jobs({f"https://x.example/{i}": {"status": "applied", "company": "Acme", "description": "long " * 50,
                                                           "collected_at": f"2026-01-{i + 1:02d}"} for i in range(3)})
        body = auth_client.get("/jobs", params={"fields": "status,company", "limit": 1000000}).json()
        assert len(body) == 3
        assert all(set(j) == {"url", "status", "company"} for j in body)

    def test_limit_must_be_positive(self, auth_client):
        assert auth_client.get("/jobs", params={"limit": 0}).status_code == 422


@pytest.mark.real_readiness
class TestStartReadiness:
    """Without an AI model, a resume or a name, a run would only fail every job: it doesn't start."""

    @pytest.fixture(autouse=True)
    def _no_run(self, monkeypatch):
        monkeypatch.setattr(main, "_kill_browser_processes", lambda: None)
        monkeypatch.setattr(main, "_run_async_in_thread", lambda make_coro, status: None)
        monkeypatch.setattr(main, "_apply_status", {"running": False, "mode": None, "workers": 1, "log": []})
        monkeypatch.setattr(main, "_collection_status", {"running": False, "title": None, "log": []})

    def _ready(self, tmp_path):
        resume = tmp_path / "cv.pdf"
        resume.write_bytes(b"%PDF-1.4")
        main.save_llm_settings({"provider": "gemini", "gemini": {"api_key": "fake-key", "model": "g"}})
        main.save_settings({**main.load_settings(), "resume_path": str(resume)})
        main.save_profile({**main.load_profile(), "name": "Ada Lovelace"})
        return resume

    def test_no_ai_model(self, auth_client):
        main.save_llm_settings({"provider": "openrouter", "openrouter": {"api_key": ""}})
        for path, body in (("/apply/start", {"mode": "all"}), ("/jobs/collect", {"title": "Intern"})):
            resp = auth_client.post(path, json=body).json()
            assert resp["success"] is False and "AI model" in resp["message"]
        assert main._apply_status["running"] is False and main._collection_status["running"] is False

    def test_no_resume_or_name(self, auth_client, tmp_path):
        resume = self._ready(tmp_path)
        resume.unlink()
        assert "resume" in auth_client.post("/apply/start", json={"mode": "all"}).json()["message"]
        self._ready(tmp_path)
        main.save_profile({**main.load_profile(), "name": " "})
        assert "name" in auth_client.post("/apply/start", json={"mode": "all"}).json()["message"]

    def test_ready_starts(self, auth_client, tmp_path):
        self._ready(tmp_path)
        assert auth_client.post("/apply/start", json={"mode": "all"}).json()["success"] is True
        main._apply_status["running"] = False
        assert auth_client.post("/jobs/collect", json={"title": "Intern"}).json()["success"] is True


def test_deleting_the_same_job_twice_in_one_request(auth_client):
    url = "https://www.linkedin.com/jobs/view/5551"
    auth_client.post("/jobs/add", json={"url": url})
    resp = auth_client.request("DELETE", "/jobs", json={"urls": [url, url]})
    assert resp.status_code == 200 and resp.json()["deleted"] == 1
