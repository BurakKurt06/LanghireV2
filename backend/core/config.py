"""
Configuration management for LangHireV2.
All settings stored in OS-appropriate app data directory.
No hardcoded values.
"""
import copy
import json
import platform
from pathlib import Path


def get_data_dir() -> Path:
    """Get OS-appropriate data directory for the app."""
    system = platform.system()
    if system == "Darwin":
        base = Path.home() / "Library" / "Application Support" / "langhirev2"
    elif system == "Windows":
        base = Path.home() / "AppData" / "Roaming" / "langhirev2"
    else:  # Linux
        base = Path.home() / ".config" / "langhirev2"
    base.mkdir(parents=True, exist_ok=True)
    return base


def _load_json(path: Path, default=None):
    if path.exists():
        return json.loads(path.read_text())
    return default if default is not None else {}


def _save_json(path: Path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2))
    tmp.replace(path)
    if any(s in path.name for s in ("llm_settings", "settings")):
        try:
            import os, stat
            os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
        except OSError:
            import logging
            logging.getLogger("config").warning(
                f"Could not set restrictive permissions on {path.name}"
            )


# ── Profile ───────────────────────────────────────────────────────────────

def load_profile() -> dict:
    return _load_json(get_data_dir() / "candidate_profile.json", copy.deepcopy(DEFAULT_PROFILE))


# Every field a profile can have. Saved profiles may lack some (older versions,
# hand edits), so readers fill the gaps from here.
DEFAULT_PROFILE = {
    "name": "",
    "email": "",
    "phone": "",
    "phone_country_code": "+1",
    "country": "US",
    "address": {"street": "", "city": "", "state": "", "zip": "", "country": "USA"},
    "work_authorization": "",
    "visa_sponsorship_needed": False,
    "willing_to_relocate": False,
    "preferred_work_mode": "hybrid",
    "years_of_experience": 0,
    "education": {"degree": "", "major": "", "school": "", "start_date": "", "graduation": ""},
    "current_role": "",
    "target_job_titles": [],
    "target_locations": [],
    "languages": ["English"],
    "skills": [],
    "salary_expectation": {"min": 50000, "max": 100000, "currency": "USD", "period": "annual"},
    "notice_period": "",
    "nationality": "",
    "date_of_birth": "",
    "cover_letter": "",
    "date_format": "MM/DD/YYYY",
    "notes": "",
    # Answers to common screening questions; empty ones are left to the agent's rules.
    "screening": {
        "start_date": "",
        "gpa": "",
        "linkedin_url": "",
        "portfolio_url": "",
        "gender": "",
        "race_ethnicity": "",
        "veteran_status": "",
        "disability_status": "",
    },
}


def save_profile(profile: dict):
    _save_json(get_data_dir() / "candidate_profile.json", profile)


# ── LLM Settings ──────────────────────────────────────────────────────────

def load_llm_settings() -> dict:
    return _load_json(get_data_dir() / "llm_settings.json", {
        "provider": "openrouter",
        "openai": {"api_key": "", "model": "gpt-5.4-mini"},
        "anthropic": {"api_key": "", "model": "claude-sonnet-5"},
        "bedrock": {"access_key": "", "secret_key": "", "region": "us-west-2", "model": "us.anthropic.claude-sonnet-4-6"},
        "gemini": {"api_key": "", "model": "gemini-3-flash-preview"},
        "ollama": {"base_url": "http://localhost:11434", "model": ""},
        "openrouter": {"api_key": "", "model": "qwen/qwen3.6-plus"},
        "chatgpt": {"model": "gpt-6-luna", "reasoning_effort": ""},
    })


def save_llm_settings(settings: dict):
    _save_json(get_data_dir() / "llm_settings.json", settings)


# ── App Settings ──────────────────────────────────────────────────────────

def load_settings() -> dict:
    return _load_json(get_data_dir() / "settings.json", {
        "resume_path": "",
        "blocked_domains": [],
        "sensitive_data": {"email": "", "password": ""},
        "max_failures": 8,
        "data_dir": str(get_data_dir()),
        "telemetry_enabled": True,
        "theme": "system",
        # Job board collection (LinkedIn, Indeed, ... plugins). Default ON so
        # the app behaves exactly as before.
        "job_collection_enabled": True,
        # Anti-detection: stealth patches + randomised pauses between jobs.
        "stealth_enabled": False,
        "human_delay_enabled": False,
        "human_delay_min": 5,
        "human_delay_max": 15,
        # 2Captcha solver. Off by default; the key is never sent to the LLM.
        "captcha_solver_enabled": False,
        "captcha_api_key": "",
    })


def save_settings(settings: dict):
    if "max_failures" in settings:
        settings["max_failures"] = max(1, min(int(settings["max_failures"]), 50))
    if "human_delay_min" in settings:
        settings["human_delay_min"] = max(0, min(int(settings["human_delay_min"]), 300))
    if "human_delay_max" in settings:
        settings["human_delay_max"] = max(0, min(int(settings["human_delay_max"]), 300))
    # Keep the window coherent: a max below the min would make the range empty.
    if "human_delay_min" in settings and "human_delay_max" in settings:
        if settings["human_delay_max"] < settings["human_delay_min"]:
            settings["human_delay_max"] = settings["human_delay_min"]
    if "blocked_domains" in settings:
        settings["blocked_domains"] = [
            str(d).strip() for d in settings["blocked_domains"]
            if isinstance(d, str) and d.strip()
        ]
    _save_json(get_data_dir() / "settings.json", settings)

