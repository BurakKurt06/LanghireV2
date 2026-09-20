"""Tests for the newly added job-source plugins and for the URL encoding that
the plugin collection path depends on.

These cover only the new behaviour: the Handshake and Intern Insider plugins,
and the percent-encoding of values interpolated into a plugin's ``search_url``.
The pre-existing plugin behaviour is covered by ``test_registry.py``.
"""
import pytest

from sources.registry import PluginRegistry


@pytest.fixture
def registry():
    return PluginRegistry()


NEW_PLUGINS = ["handshake", "interninsider"]


class TestNewPluginsLoad:
    @pytest.mark.parametrize("name", NEW_PLUGINS)
    def test_plugin_is_registered(self, registry, name):
        assert registry.get_by_name(name) is not None

    @pytest.mark.parametrize("name", NEW_PLUGINS)
    def test_plugin_is_builtin_and_enabled_by_default(self, registry, name):
        plugin = registry.get_by_name(name)
        assert plugin.is_builtin is True
        assert plugin.enabled is True

    @pytest.mark.parametrize("name", NEW_PLUGINS)
    def test_plugin_requires_login_with_a_login_url(self, registry, name):
        """Both boards are behind a login the user completes manually."""
        plugin = registry.get_by_name(name)
        assert plugin.requires_login is True
        assert plugin.login_url.startswith("https://")

    @pytest.mark.parametrize("name", NEW_PLUGINS)
    def test_prompts_are_non_empty(self, registry, name):
        plugin = registry.get_by_name(name)
        assert plugin.collection_prompt.strip()
        assert plugin.apply_prompt.strip()

    def test_handshake_is_available_everywhere(self, registry):
        """One Handshake domain serves every university, so it is not region-locked."""
        assert registry.get_by_name("handshake").countries == ["ALL"]

    def test_interninsider_is_north_america(self, registry):
        assert set(registry.get_by_name("interninsider").countries) == {"US", "CA"}


class TestCollectionPromptContract:
    """The collection prompt must give the agent what the parser expects."""

    @pytest.mark.parametrize("name", NEW_PLUGINS)
    def test_prompt_contains_the_search_url(self, registry, name):
        plugin = registry.get_by_name(name)
        out = plugin.render_collection_prompt(["Data Analyst"], ["Toronto"], 10)
        assert "@@JOB_FOUND" in out
        assert "http" in out

    @pytest.mark.parametrize("name", NEW_PLUGINS)
    def test_prompt_tells_the_agent_not_to_apply(self, registry, name):
        plugin = registry.get_by_name(name)
        out = plugin.render_collection_prompt(["Data Analyst"], ["Toronto"], 10)
        assert "Do NOT apply" in out

    @pytest.mark.parametrize("name", NEW_PLUGINS)
    def test_prompt_warns_about_hidden_honeypot_links(self, registry, name):
        plugin = registry.get_by_name(name)
        out = plugin.render_collection_prompt(["Data Analyst"], ["Toronto"], 10)
        assert "honeypot" in out.lower()

    @pytest.mark.parametrize("name", NEW_PLUGINS)
    def test_max_jobs_is_substituted(self, registry, name):
        plugin = registry.get_by_name(name)
        out = plugin.render_collection_prompt(["Data Analyst"], ["Toronto"], 37)
        assert "37" in out

    @pytest.mark.parametrize("name", NEW_PLUGINS)
    def test_apply_prompt_substitutes_job_url_and_resume(self, registry, name):
        plugin = registry.get_by_name(name)
        out = plugin.render_apply_prompt(
            job={"url": "https://example.test/posting/9", "title": "T", "company": "C"},
            profile={},
            qa_bank={},
            resume_path="C:/resumes/me.pdf",
        )
        assert "https://example.test/posting/9" in out
        assert "C:/resumes/me.pdf" in out


class TestSearchUrlEncoding:
    """Values going into search_url must be percent-encoded.

    Without this a multi-word title produces a URL containing raw spaces.
    The human-readable prompt body must keep the unencoded text.
    """

    def test_multiword_title_is_encoded_in_the_url(self, registry):
        plugin = registry.get_by_name("interninsider")
        out = plugin.render_collection_prompt(["Software Engineer"], [], 5)
        assert "searchTerm=Software+Engineer" in out
        assert "searchTerm=Software Engineer" not in out

    def test_location_with_comma_is_encoded(self, registry):
        plugin = registry.get_by_name("handshake")
        out = plugin.render_collection_prompt(["Analyst"], ["Toronto, ON"], 5)
        assert "location=Toronto%2C+ON" in out

    def test_filter_values_with_spaces_are_encoded(self, registry):
        plugin = registry.get_by_name("handshake")
        out = plugin.render_collection_prompt(
            ["Analyst"], ["Toronto"], 5, filters={"job_type": "On Campus Student Employment"}
        )
        assert "job.jobType=On+Campus+Student+Employment" in out

    def test_url_has_no_raw_spaces(self, registry):
        plugin = registry.get_by_name("handshake")
        out = plugin.render_collection_prompt(["Software Engineer"], ["New York, NY"], 5)
        url_lines = [ln.strip() for ln in out.splitlines() if ln.strip().startswith("http")]
        assert url_lines, "expected a URL line in the rendered prompt"
        assert " " not in url_lines[0]

    def test_prompt_body_keeps_readable_title(self, registry):
        """Only the URL is encoded; prose stays human-readable."""
        plugin = registry.get_by_name("indeed")
        out = plugin.render_collection_prompt(["Software Engineer"], ["Toronto"], 5)
        assert "q=Software+Engineer" in out
