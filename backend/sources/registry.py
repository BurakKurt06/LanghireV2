"""YAML-based plugin registry for job source plugins.

Loads built-in plugins from backend/sources/plugins/ and community plugins
from the user data directory (~/.langhirev2/plugins/).
"""

import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import yaml

try:
    from core.config import get_data_dir
except ImportError:
    from backend.core.config import get_data_dir


REQUIRED_FIELDS = {"name", "display_name", "version", "author", "countries", "website", "collection_prompt", "apply_prompt"}


@dataclass
class AuthCookie:
    name: str
    domain: str


@dataclass
class DomainPattern:
    pattern: str
    normalize_to: str


@dataclass
class PluginFilter:
    key: str
    label: str
    type: str
    options: list[dict] = field(default_factory=list)
    url_param: str = ""
    default: str = ""


# The shared search preferences (core.job_search) a board can take in its search address.
SHARED_PARAMS = ("job_type", "work_mode", "experience", "posted_within")
_PARAM_NAME = re.compile(r"[A-Za-z0-9_.\-\[\]]{1,40}")
_FRAGMENT = re.compile(r"[A-Za-z0-9_.\-]{1,40}=[A-Za-z0-9_.%+\-]{0,80}")


@dataclass
class SearchParam:
    """How a board takes one shared preference.

    With `param`, each value maps to the board's own code (`join` set when the board
    takes several in one parameter: f_JT=F,P). Without it, each value maps to a whole
    "name=value" pair (Reed: fullTime=true&partTime=true). `posted_within` maps day
    counts ("7") to the board's code; the smallest that covers the wish is used.
    """
    param: str = ""
    join: str = ""
    values: dict = field(default_factory=dict)


@dataclass
class ListingConfig:
    """How Quick collection reads a board's results page without an AI step per job.

    `job_link`: a pattern on each job link's address; its first group is the job's id.
    `job_url`: the job's own address from the id ("{id}", "{host}"); empty keeps the link
    without its query, apart from `keep_query`. `next_param`: the page-number parameter
    (page n = next_first + n * next_step); without it the page's own Next link or
    "show more" button is used. `easy_apply`: words on a listing that mean the board's
    own quick application. `skip_lines`: whole lines that mean skip the listing
    (LinkedIn's "Applied").
    """
    job_link: str
    job_url: str = ""
    keep_query: list[str] = field(default_factory=list)
    next_param: str = ""
    next_step: int = 1
    next_first: int = 0
    easy_apply: str = ""
    skip_lines: list[str] = field(default_factory=list)


def _safe_pattern(pattern, *, trusted=False) -> str:
    """A plugin's pattern, if it is short, valid and without nested repeats (which can take
    forever on a long address)."""
    if not isinstance(pattern, str) or not 0 < len(pattern) <= 200:
        return ""
    if re.search(r"\([^()]*[+*][^()]*\)\s*[+*{]", pattern):
        return ""
    try:
        from re import _parser, _constants
        parsed = _parser.parse(pattern, 0)
        variable_repeats = 0
        def check(items, repeated=False):
            nonlocal variable_repeats
            for op, value in items:
                if op in (_constants.MAX_REPEAT, _constants.MIN_REPEAT):
                    low, high, child = value
                    if repeated:
                        raise ValueError("nested repeat")
                    if low != high:
                        variable_repeats += 1
                    check(child, repeated or high > 1)
                elif op == _constants.SUBPATTERN:
                    check(value[-1], repeated)
                elif op == _constants.BRANCH:
                    if repeated or not trusted:
                        raise ValueError("repeated alternation")
                    for branch in value[1]:
                        check(branch, repeated)
                elif op not in (_constants.LITERAL, _constants.NOT_LITERAL, _constants.IN,
                                _constants.ANY, _constants.AT, _constants.CATEGORY):
                    raise ValueError("unsupported pattern construct")
        check(parsed)
        # Imported patterns execute in both Python and Chromium. Keep their
        # subset bounded rather than relying on a timeout in only one engine.
        # Built-ins are reviewed code with bounded input, not imported YAML.
        if not trusted and variable_repeats > 1:
            return ""
        re.compile(pattern)
    except (re.error, ValueError, OverflowError, RecursionError):
        return ""
    return pattern


def _parse_search_params(data) -> dict:
    out = {}
    if not isinstance(data, dict):
        return out
    for key in SHARED_PARAMS:
        spec = data.get(key)
        if not isinstance(spec, dict) or not isinstance(spec.get("values"), dict):
            continue
        param = str(spec.get("param") or "")
        if param and not _PARAM_NAME.fullmatch(param):
            continue
        values = {}
        for ours, theirs in spec["values"].items():
            theirs = str(theirs)
            if (param and re.fullmatch(r"[A-Za-z0-9_.%+\-]{1,80}", theirs)) or (not param and _FRAGMENT.fullmatch(theirs)):
                values[str(ours)] = theirs
        if values:
            out[key] = SearchParam(param=param, join=str(spec.get("join") or "")[:3], values=values)
    return out


def _parse_listing(data, *, trusted=False) -> Optional[ListingConfig]:
    if not isinstance(data, dict):
        return None
    link = _safe_pattern(data.get("job_link"), trusted=trusted)
    if not link:
        return None
    job_url = str(data.get("job_url") or "")
    if job_url and (not job_url.startswith("https://") or re.search(r"\{(?!id\}|host\})", job_url)):
        return None
    nxt = data.get("next_page") if isinstance(data.get("next_page"), dict) else {}
    param = str(nxt.get("param") or "")

    def _int(value, default):
        try:
            return int(value)
        except (TypeError, ValueError):
            return default

    return ListingConfig(
        job_link=link, job_url=job_url,
        keep_query=[q for q in (data.get("keep_query") or []) if isinstance(q, str) and _PARAM_NAME.fullmatch(q)][:5],
        next_param=param if _PARAM_NAME.fullmatch(param or "-") else "",
        next_step=max(1, _int(nxt.get("step"), 1)), next_first=max(0, _int(nxt.get("first"), 0)),
        easy_apply=_safe_pattern(data.get("easy_apply"), trusted=trusted),
        skip_lines=[p for p in (_safe_pattern(x, trusted=trusted) for x in (data.get("skip_lines") or [])[:5]) if p],
    )


def shared_params(search_params: dict, prefs: dict | None) -> list[tuple[str, str]]:
    """The query pairs for the shared preferences a board can express.

    A preference goes in only when the board can say all of it: with remote and hybrid
    wanted and a board that only filters "remote", the board gets nothing and the
    collector checks each listing instead (asking the board for less would hide jobs).
    """
    if not prefs:
        return []
    wanted = {"job_type": prefs.get("job_types") or [], "work_mode": prefs.get("work_modes") or [],
              "experience": prefs.get("experience") or []}
    pairs: list[tuple[str, str]] = []
    for key, spec in search_params.items():
        if key == "posted_within":
            days = int(prefs.get("posted_within") or 0)
            fits = sorted((int(d), code) for d, code in spec.values.items() if str(d).isdigit() and int(d) >= days)
            if days and fits and spec.param:
                pairs.append((spec.param, fits[0][1]))
            continue
        values = wanted.get(key) or []
        if not values or any(v not in spec.values for v in values):
            continue
        codes = list(dict.fromkeys(spec.values[v] for v in values))
        if spec.param:
            if len(codes) == 1:
                pairs.append((spec.param, codes[0]))
            elif spec.join:
                pairs.append((spec.param, spec.join.join(codes)))
        else:
            pairs.extend(tuple(code.split("=", 1)) for code in codes)
    return pairs


@dataclass
class PluginConfig:
    name: str
    display_name: str
    version: str
    author: str
    description: str
    countries: list[str]
    website: str
    requires_login: bool
    login_url: str
    search_url: str
    collection_prompt: str
    apply_prompt: str
    auth_cookies: list[AuthCookie] = field(default_factory=list)
    domain_patterns: list[DomainPattern] = field(default_factory=list)
    filters: list[PluginFilter] = field(default_factory=list)
    search_params: dict = field(default_factory=dict)   # SHARED_PARAMS key -> SearchParam
    listing: Optional[ListingConfig] = None
    is_builtin: bool = False
    enabled: bool = True
    file_path: str = ""

    def shared_filter_params(self) -> set[str]:
        """Query parameters the shared preferences set (the board's own filter for one is hidden)."""
        names = {spec.param for spec in self.search_params.values() if spec.param}
        names |= {code.split("=", 1)[0] for spec in self.search_params.values() if not spec.param
                  for code in spec.values.values()}
        return names

    def build_search_url(
        self,
        titles: list[str],
        locations: list[str],
        max_jobs: int,
        filters: dict | None = None,
        search: dict | None = None,
        page: int = 0,
    ) -> str:
        """The pre-filtered search URL the collection prompt starts from.

        `search`: the shared preferences (core.job_search) the board can take; they replace
        a board filter's default for the same parameter, never a value the user picked.
        `page`: a later results page (Quick collection), when the board numbers its pages.
        """
        from urllib.parse import quote, quote_plus

        title = titles[0] if titles else ""
        location = locations[0] if locations else ""
        # Values interpolated into the search URL must be percent-encoded, or a
        # multi-word title ("Software Engineer") produces a URL with raw spaces.
        # The prompt body below deliberately keeps the human-readable values.
        # '+' means a space in query strings, but is a literal '+' in URL paths.
        # Reed, SEEK, Stepstone and Naukri put these placeholders in the path.
        def encode(key, value):
            prefix = self.search_url.split("{" + key + "}", 1)[0]
            if "?" in prefix:
                return quote_plus(value)
            if self.is_builtin and self.name in {"reed", "seek", "naukri"}:
                # These boards route keywords/places as slugs. A literal space
                # makes Reed parse 'Data Analyst' as Data jobs in Analyst.
                value = re.sub(r"\s+", "-", value.strip()).lower()
            return quote(value, safe="")

        search_url = self.search_url.format(
            title=encode("title", title), titles=encode("titles", ", ".join(titles)),
            location=encode("location", location), locations=encode("locations", ", ".join(locations)),
            max_jobs=max_jobs,
        )
        picked: list[tuple[str, str]] = []
        defaults: list[tuple[str, str]] = []
        for f in self.filters:
            if not f.url_param:
                continue
            value = filters.get(f.key, "") if filters else ""
            if value:
                picked.append((f.url_param, str(value)))
            elif not filters and f.default:
                defaults.append((f.url_param, str(f.default)))
        shared = shared_params(self.search_params, search)
        taken = {name for name, _ in picked}
        shared = [(n, v) for n, v in shared if n not in taken]
        taken |= {name for name, _ in shared}
        # A wish the board can't express still replaces the board's default for it: wanting
        # full- or part-time jobs on Handshake must not leave its "Internship" default on.
        wishes = {"job_type": "job_types", "work_mode": "work_modes", "experience": "experience",
                  "posted_within": "posted_within"}
        for key, spec in self.search_params.items():
            # "Any time" (0) is a wish too: the board's "past week" default goes.
            if search and (search.get(wishes[key]) or (key == "posted_within" and "posted_within" in search)):
                taken |= {spec.param} if spec.param else {c.split("=", 1)[0] for c in spec.values.values()}
        pairs = picked + [(n, v) for n, v in defaults if n not in taken] + shared
        if page and self.listing and self.listing.next_param:
            pairs = [(n, v) for n, v in pairs if n != self.listing.next_param]
            pairs.append((self.listing.next_param, str(self.listing.next_first + page * self.listing.next_step)))
        if pairs:
            sep = "&" if "?" in search_url else "?"
            search_url += sep + "&".join(f"{n}={quote_plus(v, safe=',')}" for n, v in pairs)
        return search_url

    def render_collection_prompt(
        self,
        titles: list[str],
        locations: list[str],
        max_jobs: int,
        filters: dict | None = None,
        languages: list[str] | None = None,
        search: dict | None = None,
        search_url: str | None = None,
    ) -> str:
        """`search_url`: the address the collector opens, when it differs from the built one
        (Glassdoor's place ID, sources.locations): the prompt must name the same page."""
        title = titles[0] if titles else ""
        location = locations[0] if locations else ""
        search_url = search_url or self.build_search_url(titles, locations, max_jobs, filters, search=search)
        return self.collection_prompt.format(
            search_url=search_url,
            title=title, titles=", ".join(titles),
            location=location, locations=", ".join(locations),
            max_jobs=max_jobs,
            languages=", ".join(languages) if languages else "English",
        )

    def render_apply_prompt(
        self,
        job: dict,
        profile: dict,
        qa_bank: dict,
        resume_path: str = "",
        cover_letter: str = "",
        memories: str = "",
    ) -> str:
        profile_str = _format_profile(profile)
        qa_str = "\n".join(f"Q: {q}\nA: {a}" for q, a in qa_bank.items() if a) if qa_bank else ""
        return self.apply_prompt.format(
            job_url=job.get("url", ""),
            job_title=job.get("title", ""),
            company=job.get("company", ""),
            profile=profile_str,
            resume_path=resume_path,
            qa_bank=qa_str,
            cover_letter=cover_letter,
            memories=memories,
        )


def _format_profile(profile: dict) -> str:
    parts = []
    if profile.get("name"):
        parts.append(f"Name: {profile['name']}")
    if profile.get("email"):
        parts.append(f"Email: {profile['email']}")
    if profile.get("phone"):
        parts.append(f"Phone: {profile.get('phone_country_code', '')}{profile['phone']}")
    addr = profile.get("address", {})
    if addr.get("city"):
        parts.append(f"Location: {addr['city']}, {addr.get('state', '')} {addr.get('zip', '')} {addr.get('country', '')}")
    if profile.get("work_authorization"):
        parts.append(f"Work Authorization: {profile['work_authorization']}")
    if profile.get("notice_period"):
        parts.append(f"Notice Period: {profile['notice_period']}")
    if profile.get("nationality"):
        parts.append(f"Nationality: {profile['nationality']}")
    if profile.get("years_of_experience"):
        parts.append(f"Experience: {profile['years_of_experience']} years")
    edu = profile.get("education", {})
    if edu.get("degree"):
        major = f" in {edu['major']}" if edu.get("major") else ""
        parts.append(f"Education: {edu['degree']}{major} from {edu.get('school', '')} ({edu.get('graduation', '')})")
    if profile.get("skills"):
        parts.append(f"Skills: {', '.join(profile['skills'])}")
    sal = profile.get("salary_expectation", {})
    if sal.get("min"):
        period = sal.get("period", "annual")
        parts.append(f"Salary: {sal['currency']} {sal['min']:,}-{sal['max']:,} ({period})")
    return "\n".join(parts)


def _parse_plugin_yaml(data: dict, file_path: str, is_builtin: bool) -> Optional[PluginConfig]:
    missing = REQUIRED_FIELDS - set(data.keys())
    if missing:
        raise ValueError(f"Plugin missing required fields: {missing}")

    auth_cookies = []
    for c in data.get("auth_cookies", []):
        if isinstance(c, dict) and "name" in c and "domain" in c:
            auth_cookies.append(AuthCookie(name=c["name"], domain=c["domain"]))

    domain_patterns = []
    for p in data.get("domain_patterns", []):
        if isinstance(p, dict) and "pattern" in p and "normalize_to" in p:
            domain_patterns.append(DomainPattern(pattern=p["pattern"], normalize_to=p["normalize_to"]))

    filters = []
    for f in data.get("filters", []):
        if isinstance(f, dict) and "key" in f and "label" in f:
            filters.append(PluginFilter(
                key=f["key"],
                label=f["label"],
                type=f.get("type", "select"),
                options=f.get("options", []),
                url_param=f.get("url_param", ""),
                default=f.get("default", ""),
            ))

    return PluginConfig(
        name=data["name"],
        display_name=data["display_name"],
        version=data["version"],
        author=data["author"],
        description=data.get("description", ""),
        countries=data["countries"],
        website=data["website"],
        requires_login=data.get("requires_login", False),
        login_url=data.get("login_url", ""),
        search_url=data.get("search_url", ""),
        collection_prompt=data["collection_prompt"],
        apply_prompt=data["apply_prompt"],
        auth_cookies=auth_cookies,
        domain_patterns=domain_patterns,
        filters=filters,
        search_params=_parse_search_params(data.get("search_params")),
        listing=_parse_listing(data.get("listing"), trusted=is_builtin),
        is_builtin=is_builtin,
        enabled=True,
        file_path=file_path,
    )


class PluginRegistry:
    def __init__(self):
        self._plugins: dict[str, PluginConfig] = {}
        self._load_all()

    def _get_builtin_dir(self) -> Path:
        import sys
        if getattr(sys, 'frozen', False):
            # PyInstaller bundles data into sys._MEIPASS
            base = Path(sys._MEIPASS)  # type: ignore[attr-defined]
            return base / "sources" / "plugins"
        return Path(__file__).parent / "plugins"

    def _get_community_dir(self) -> Path:
        d = get_data_dir() / "plugins"
        d.mkdir(parents=True, exist_ok=True)
        return d

    def _load_yaml_file(self, path: Path, is_builtin: bool) -> Optional[PluginConfig]:
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                return None
            return _parse_plugin_yaml(data, str(path), is_builtin)
        except Exception as e:
            import logging
            logging.getLogger("plugins").warning(f"Failed to load plugin {path.name}: {e}")
            return None

    def _load_all(self):
        self._plugins.clear()
        # Load built-in plugins
        builtin_dir = self._get_builtin_dir()
        if builtin_dir.exists():
            for f in sorted(builtin_dir.glob("*.yaml")):
                plugin = self._load_yaml_file(f, is_builtin=True)
                if plugin:
                    self._plugins[plugin.name] = plugin

        # Load community plugins
        community_dir = self._get_community_dir()
        if community_dir.exists():
            for f in sorted(community_dir.glob("*.yaml")):
                plugin = self._load_yaml_file(f, is_builtin=False)
                if plugin and plugin.name not in self._plugins:
                    self._plugins[plugin.name] = plugin

        # Load enabled/disabled state
        self._load_state()

    def _get_state_path(self) -> Path:
        return get_data_dir() / "plugin_state.json"

    def _load_state(self):
        import json
        state_path = self._get_state_path()
        if state_path.exists():
            try:
                state = json.loads(state_path.read_text())
                for name, enabled in state.items():
                    if name in self._plugins:
                        self._plugins[name].enabled = enabled
            except Exception:
                pass

    def _save_state(self):
        import json
        state = {name: p.enabled for name, p in self._plugins.items()}
        state_path = self._get_state_path()
        state_path.write_text(json.dumps(state, indent=2))

    def get_all(self) -> list[PluginConfig]:
        return list(self._plugins.values())

    def get_enabled(self) -> list[PluginConfig]:
        return [p for p in self._plugins.values() if p.enabled]

    def get_by_name(self, name: str) -> Optional[PluginConfig]:
        return self._plugins.get(name)

    def get_for_country(self, country_code: str) -> list[PluginConfig]:
        return [
            p for p in self._plugins.values()
            if p.enabled and (country_code in p.countries or "ALL" in p.countries)
        ]

    def set_enabled(self, name: str, enabled: bool) -> bool:
        if name not in self._plugins:
            return False
        self._plugins[name].enabled = enabled
        self._save_state()
        return True

    def import_plugin(self, source_path: str) -> PluginConfig:
        source = Path(source_path)
        if not source.exists():
            raise FileNotFoundError(f"Plugin file not found: {source_path}")
        if source.suffix not in (".yaml", ".yml"):
            raise ValueError("Plugin must be a .yaml or .yml file")

        plugin = self._load_yaml_file(source, is_builtin=False)
        if not plugin:
            raise ValueError("Invalid plugin file: failed to parse")
        if plugin.name in self._plugins and self._plugins[plugin.name].is_builtin:
            raise ValueError(f"Cannot override built-in plugin: {plugin.name}")

        dest = self._get_community_dir() / source.name
        shutil.copy2(source, dest)
        plugin.file_path = str(dest)
        self._plugins[plugin.name] = plugin
        self._save_state()
        return plugin

    def remove_plugin(self, name: str) -> bool:
        if name not in self._plugins:
            return False
        plugin = self._plugins[name]
        if plugin.is_builtin:
            raise ValueError("Cannot remove built-in plugins (disable instead)")
        file_path = Path(plugin.file_path)
        if file_path.exists():
            file_path.unlink()
        del self._plugins[name]
        self._save_state()
        return True

    def reload(self):
        self._load_all()
