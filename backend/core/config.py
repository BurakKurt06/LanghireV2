"""
Configuration management for LangHireV2.
All settings stored in OS-appropriate app data directory.
No hardcoded values.
"""
import copy
import json
import logging
import platform
from pathlib import Path

try:
    from core import secret_store
except ImportError:
    from backend.core import secret_store

# Secrets in the settings files: stored encrypted for this Windows user
# (core.secret_store), handed to the backend's own code in plain, and never sent
# to the page in full (the page gets SECRET_MASK and sends it back to keep one).
SETTINGS_SECRETS = [("sensitive_data", "password"), ("captcha_api_key",)]
LLM_SECRETS = [(p, "api_key") for p in ("openai", "anthropic", "gemini", "openrouter", "openai_compatible")] + [
    ("bedrock", "access_key"), ("bedrock", "secret_key")]
SECRET_MASK = "\u2022" * 8


def mask_secrets(data: dict, paths) -> dict:
    """A copy for the page: each saved secret at `paths` replaced by SECRET_MASK."""
    return secret_store._map_paths(data, paths, lambda value: SECRET_MASK if value else value)


def keep_masked(incoming: dict, stored: dict, paths) -> dict:
    """A copy of `incoming` where each secret the page sent back as SECRET_MASK is the stored value again."""
    out = json.loads(json.dumps(incoming or {}))
    for path in paths:
        node, saved = out, stored
        for key in path[:-1]:
            node = node.get(key) if isinstance(node, dict) else None
            saved = saved.get(key) if isinstance(saved, dict) else None
        if isinstance(node, dict) and node.get(path[-1]) == SECRET_MASK:
            node[path[-1]] = saved.get(path[-1], "") if isinstance(saved, dict) else ""
    return out


def migrate_secrets() -> int:
    """Encrypt secrets still stored in plain (settings from before encryption). Returns files rewritten.

    Each file is only replaced after every value encrypted and read back (protect
    checks), with an atomic replace; on any failure it stays as it was.
    """
    rewritten = 0
    for name, paths in (("settings.json", SETTINGS_SECRETS), ("llm_settings.json", LLM_SECRETS)):
        path = get_data_dir() / name
        try:
            raw = _load_json(path, {})
            if not isinstance(raw, dict):
                continue
            sealed = secret_store.protect_paths(raw, paths)
            if sealed != raw:
                _save_json(path, sealed)
                rewritten += 1
        except Exception as e:
            logging.getLogger("config").warning(f"Couldn't encrypt the secrets in {name}: {e}")
    try:
        from core import chatgpt_auth
    except ImportError:
        from backend.core import chatgpt_auth
    try:
        rewritten += chatgpt_auth.migrate()
    except Exception as e:
        logging.getLogger("config").warning(f"Couldn't encrypt the ChatGPT sign-in: {e}")
    return rewritten


def get_data_dir() -> Path:
    """Get OS-appropriate data directory for the app."""
    system = platform.system()
    if system == "Darwin":
        base = Path.home() / "Library" / "Application Support" / "langhirev2"
    elif system == "Windows":
        base = Path.home() / "AppData" / "Roaming" / "langhirev2"
    else:  # Linux
        base = Path.home() / ".config" / "langhirev2"
    base.mkdir(parents=True, exist_ok=True, mode=0o700)
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
    "education": {"degree": "", "major": "", "major_alternative": "", "school": "", "start_date": "", "graduation": ""},
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
    # Jobs outside this radius are skipped (remote always counts). "" = address ZIP.
    "search_area": {"center_zip": "", "radius": "any"},
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
    return secret_store.reveal_paths(_load_llm_settings_file(), LLM_SECRETS)


def _load_llm_settings_file() -> dict:
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
    _save_json(get_data_dir() / "llm_settings.json", secret_store.protect_paths(settings, LLM_SECRETS))


# ── App Settings ──────────────────────────────────────────────────────────

def load_settings() -> dict:
    return secret_store.reveal_paths(_load_settings_file(), SETTINGS_SECRETS)


def _load_settings_file() -> dict:
    return _load_json(get_data_dir() / "settings.json", {
        "resume_path": "",
        "blocked_domains": [],
        "sensitive_data": {"email": "", "password": ""},
        "max_failures": 8,
        "data_dir": str(get_data_dir()),
        "telemetry_enabled": False,  # usage analytics and crash reports: opt-in
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
        # Applications running at the same time (1-4).
        "parallel_jobs": 1,
    })


# Applications running at the same time (Settings → Agent Settings).
MAX_PARALLEL_JOBS = 4


def clamp_parallel_jobs(value) -> int:
    """1 to MAX_PARALLEL_JOBS; 1 for anything unreadable."""
    try:
        return max(1, min(int(value or 1), MAX_PARALLEL_JOBS))
    except (TypeError, ValueError):
        return 1


def split_name(name: str) -> tuple[str, str]:
    """('Ada', 'Lovelace') for 'Ada Lovelace'; ('', '') for a single word. The last word is the last name."""
    parts = (name or "").split()
    return (" ".join(parts[:-1]), parts[-1]) if len(parts) > 1 else ("", "")


def save_settings(settings: dict):
    if "max_failures" in settings:
        settings["max_failures"] = max(1, min(int(settings["max_failures"]), 50))
    if "parallel_jobs" in settings:
        settings["parallel_jobs"] = clamp_parallel_jobs(settings["parallel_jobs"])
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
    _save_json(get_data_dir() / "settings.json", secret_store.protect_paths(settings, SETTINGS_SECRETS))

