from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


def _default_db_path() -> str:
    """Where the brain (the SQLite DB + session_salt + identity) lives when MONGOOSE_DB_PATH is unset.

    CRITICAL for updates: this must NOT be inside the install/download folder. A reinstall or update is a
    fresh `mongoose` folder; if the brain lived in ./data there, every update would start from a
    blank brain and the user would lose their conversation, memory and identity. So the default is a
    STABLE per-user location in the home folder (~/.mongoose/data/mongoose.sqlite3) that survives swapping
    out the code folder entirely. The MONGOOSE_DB_PATH env var still overrides this (tests, CI, and the
    sandbox set it explicitly), so nothing here changes their behaviour.
    """
    home = Path(os.path.expanduser("~"))
    # Fallback if the home directory can't be resolved (very unusual): keep the old in-folder location
    # rather than crash, so the process still starts.
    if not str(home) or str(home) == "~":
        return "./data/mongoose.sqlite3"
    return str(home / ".mongoose" / "data" / "mongoose.sqlite3")


def _default_secret_path(filename: str) -> str:
    """Where an OAuth credential file (Google token, Microsoft MSAL cache, etc.) lives when its
    env override is unset. Same reasoning as _default_db_path, and just as critical: these
    previously defaulted to ./secrets/<file>, relative to whatever folder the installer happened
    to run from. Every fresh unzip -- which is what an update actually is in practice unless it is
    run strictly in-place -- silently lost all sign-in state, even though it had just been
    configured, because the new folder's ./secrets/ starts empty. Default to the STABLE per-user
    location instead (~/.mongoose/secrets/<file>), and migrate forward, once, any existing
    ./secrets/<file> relative to the current working directory, so an install that was already
    configured before this fix is not orphaned by it.
    """
    home = Path(os.path.expanduser("~"))
    if not str(home) or str(home) == "~":
        return f"./secrets/{filename}"
    stable_dir = home / ".mongoose" / "secrets"
    try:
        stable_dir.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass
    stable_path = stable_dir / filename
    if not stable_path.exists():
        legacy = Path("./secrets") / filename
        try:
            if legacy.exists() and legacy.is_file():
                stable_dir.mkdir(parents=True, exist_ok=True)  # retry: outer attempt may have failed silently
                import shutil as _shutil
                _shutil.copy2(legacy, stable_path)
        except Exception:
            pass  # best-effort; the caller still gets the correct stable path either way
    return str(stable_path)

def _scrub_brain_overrides(pre: dict) -> None:
    """After ANY .env load: brain-location keys that were not in the real process environment
    beforehand are refused and recorded, so no .env file anywhere can move the brain. Called
    from BOTH load sites (import time and get_settings), because either alone leaves the other
    as a way for a second, forked brain to appear."""
    for k in _BRAIN_LOCATION_KEYS:
        if not pre.get(k) and k in os.environ:
            REJECTED_BRAIN_OVERRIDES[k] = os.environ.pop(k)


def _load_stable_dotenv() -> None:
    """Load .env from the stable, install-independent path so the running server reads config from the
    same file the setup wizard writes to. Falls back to the default (relative) load if resolution
    fails, so this can never prevent startup.

    BRAIN-LOCATION PROTECTION: whatever file gets loaded, the keys that decide WHERE THE BRAIN
    LIVES (MONGOOSE_DB_PATH, MONGOOSE_DATA_DIR) are only honoured from the real process
    environment, never from a .env file. A .env sitting in whatever folder the server happened
    to be launched from once silently rebound the entire conversation history to that folder,
    forking the brain: the owner's chat continued in a database inside a downloads folder while
    every other surface read the canonical one. Rejected overrides are recorded in
    REJECTED_BRAIN_OVERRIDES so doctor can name exactly what was refused and where it pointed."""
    pre = {k: (k in os.environ) for k in _BRAIN_LOCATION_KEYS}
    try:
        from .setup_wizard import ENV_PATH as _EP
        if _EP.exists():
            load_dotenv(_EP)
        else:
            load_dotenv()
    except Exception:
        try:
            load_dotenv()
        except Exception:
            pass
    _scrub_brain_overrides(pre)


# Keys that decide WHOSE data and identity this process uses. Only the real process environment may set
# them; a .env file anywhere is refused, so no stray .env can repoint the brain or the site profile.
_BRAIN_LOCATION_KEYS = ("MONGOOSE_DB_PATH", "MONGOOSE_DATA_DIR", "MONGOOSE_PROFILE_FILE", "MONGOOSE_RTM_PROPERTY_FILE")
# What _load_stable_dotenv refused, for doctor to surface: {key: the path it pointed at}.
REJECTED_BRAIN_OVERRIDES: dict = {}

_load_stable_dotenv()


def _bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _int(name: str, default: int) -> int:
    value = os.getenv(name)
    if not value:
        return default
    try:
        return int(value)
    except ValueError:
        return default


def _float(name: str, default: float) -> float:
    value = os.getenv(name)
    if not value:
        return default
    try:
        return float(value)
    except ValueError:
        return default


@dataclass(frozen=True, init=False)
class Settings:
    db_path: Path
    host: str
    port: int
    timezone: str
    # home_lat / home_lon / owner_id are properties (below): their defaults come from the site profile,
    # read lazily so building Settings never touches the profile (the profile locates itself via Settings).
    ollama_base_url: str
    ollama_model: str
    ollama_chat_model: str
    ollama_tool_model: str
    ollama_code_model: str
    ollama_research_model: str
    ollama_conversation_owner: str
    ollama_big_fallback: str
    speaker_policy: str
    searxng_url: str
    tls_certfile: str
    tls_keyfile: str
    ollama_keep_alive: str
    ollama_think: bool
    ollama_request_timeout_s: float
    ollama_num_predict: int
    resource_guard_minutes: int
    num_ctx: int
    history_token_budget: int
    allow_remote: bool
    lock_local: bool
    sync_remote_from_day_one: bool
    sync_delivery_enabled: bool
    proactive_delight_enabled: bool
    api_token: str
    memory_auto_extract: bool
    ha_url: str
    ha_token: str
    ha_timeout_seconds: int
    pi_ssh_host: str
    pi_ssh_port: int
    pi_ssh_key: str
    pi_ssh_password: str
    pi_config_dir: str
    pi_build_dir: str
    pi_claude_code_cmd: str
    protocol_v3_enabled: bool
    brief_home_entities: tuple[str, ...]
    brief_time: tuple[int, int]
    surpriser_enabled: bool
    surpriser_minutes: int
    email_sync_minutes: int
    tasks_sync_minutes: int
    harden_scan_minutes: int
    brain_snapshot_hour: int
    backup_hour: int
    backup_enabled: bool
    benchmark_enabled: bool
    benchmark_window_start: int
    benchmark_window_end: int
    benchmark_reveal_minutes: int
    reindex_minutes: int
    index_roots: tuple[str, ...]
    metro_watch_dir: str
    metro_archive_dir: str
    metro_watch_minutes: int
    metro_ob_provider: str
    alexa_fallback_url: str
    alexa_fallback_token: str
    alexa_timeout_seconds: int
    ntfy_url: str
    ntfy_topic: str
    ntfy_token: str
    weather_provider_order: str
    weather_current_ttl_seconds: int
    weather_minutely_ttl_seconds: int
    weather_hourly_ttl_seconds: int
    tomorrow_api_key: str
    metoffice_api_key: str
    weatherkit_team_id: str
    weatherkit_service_id: str
    weatherkit_key_id: str
    weatherkit_private_key_path: str
    imap_host: str
    imap_port: int
    imap_username: str
    imap_password: str
    imap_use_ssl: bool
    google_client_secrets_path: str
    google_token_path: str
    wp_url: str
    wp_username: str
    wp_app_password: str
    wp_plugin_dir: str
    ms_client_id: str
    ms_tenant_id: str
    ms_token_cache: str
    anthropic_api_key: str
    anthropic_instant_model: str
    openai_api_key: str
    openai_instant_model: str
    google_api_key: str
    google_instant_model: str
    cloud_escalation_order: str
    cloud_provider_order: str
    cloud_escalation_enabled: bool
    cloud_turn_budget_pennies: float
    cloud_max_output_tokens: int
    claude_code_cmd: str
    build_step_timeout_s: int
    build_code_call_budget: int
    build_run_max_seconds: int
    shared_file_roots: str
    tts_cmd: str
    tts_voice: str
    tts_rate: str
    stt_model: str
    wake_word: str
    mic_threshold: str
    mic_silence_s: float

    def __init__(self) -> None:
        object.__setattr__(self, "db_path", Path(os.getenv("MONGOOSE_DB_PATH", _default_db_path())))
        object.__setattr__(self, "host", os.getenv("MONGOOSE_HOST", "localhost"))
        object.__setattr__(self, "port", _int("MONGOOSE_PORT", 8765))
        object.__setattr__(self, "timezone", os.getenv("MONGOOSE_TIMEZONE", "Europe/London"))
        object.__setattr__(self, "ollama_base_url", os.getenv("OLLAMA_BASE_URL", "http://127.0.0.1:11434"))
        # Default persona/chat model. Gemma 4 26B-A4B: MoE (~4B active), warm
        # voice, ~18GB at Q4_K_M, 256K context. Verified Ollama tag.
        object.__setattr__(self, "ollama_chat_model", os.getenv("OLLAMA_CHAT_MODEL", "gemma4:26b-a4b-it-q4_K_M"))
        # Tool planner model. Defaults to the SAME model as chat so it's already
        # resident (no multi-GB model swap per tool turn on a 24GB box) and so a
        # single warm model serves both. In testing, the resident chat model
        # (Gemma) emitted correct tool-call JSON where a separate tool model both
        # added a swap delay and sometimes failed to emit a call at all. Override
        # with OLLAMA_TOOL_MODEL to use a dedicated tool model if you have the RAM.
        object.__setattr__(self, "ollama_tool_model",
                           os.getenv("OLLAMA_TOOL_MODEL", self.ollama_chat_model))
        # Code model, loaded on demand. Real Q4_K_M coder tag, ~9GB.
        object.__setattr__(self, "ollama_code_model", os.getenv("OLLAMA_CODE_MODEL", "qwen2.5-coder:14b-instruct-q4_K_M"))
        # Research/worker model. A SILENT specialist: it searches, analyses and
        # returns structured evidence, it NEVER produces the final user-facing
        # sentence (that is the conversation owner's job, to keep one coherent
        # voice in a thread). Defaults to the chat model so nothing changes until
        # you deliberately set a separate worker (e.g. qwen3.5:9b). See the model
        # policy block below and docs/MODEL_ARCHITECTURE.md.
        object.__setattr__(self, "ollama_research_model",
                           os.getenv("OLLAMA_RESEARCH_MODEL", self.ollama_chat_model))
        # Conversation owner: the ONE model that speaks to the user. Defaults to
        # the chat model (Gemma 26B), the best all-rounder on the task benchmark.
        # Set to a fast model (e.g. gemma4:e2b) to experiment with a snappier
        # speaker, with the big model retained as fallback for high-stakes synthesis.
        object.__setattr__(self, "ollama_conversation_owner",
                           os.getenv("MONGOOSE_CONVERSATION_OWNER", self.ollama_chat_model))
        # Big reliable fallback for high-stakes synthesis when the owner is a small
        # fast model. Defaults to Gemma 26B regardless of the owner.
        object.__setattr__(self, "ollama_big_fallback",
                           os.getenv("MONGOOSE_BIG_FALLBACK", "gemma4:26b-a4b-it-q4_K_M"))
        # Speaker policy. owner_only (default, enforced): only the conversation
        # owner emits user-facing prose; workers return structured notes the owner
        # phrases. This is the coherence guarantee; there is deliberately no
        # "any model may answer" mode.
        object.__setattr__(self, "speaker_policy",
                           os.getenv("MONGOOSE_SPEAKER_POLICY", "owner_only"))
        # SearXNG web search for the research worker. OPT-IN: empty = disabled (no
        # web, local-only). Set to your self-hosted instance, e.g.
        # http://192.168.1.x:8080 on the LAN, or an https public instance. The
        # research worker uses it only when LOCAL retrieval is thin (web is a
        # fallback, not a default). See web_search.py for the SSRF/network guard.
        # Web search ships ON by default, pointed at the LOCAL SearXNG container
        # (docker-compose service 'searxng', bound to 127.0.0.1:8888). Local-first: your
        # search terms go out to search engines from your own machine, not through anyone
        # else's server. Override MONGOOSE_SEARXNG_URL to point elsewhere or set it empty
        # to disable web search entirely.
        object.__setattr__(self, "searxng_url", os.getenv("MONGOOSE_SEARXNG_URL", "http://127.0.0.1:8888"))
        # HTTPS for the cockpit/voice on the LAN. Empty = plain http. Point these at a
        # cert + key (mkcert recommended, so devices trust it without warnings). When
        # both are set, `mongoose serve --tls` (or auto) serves https, which unlocks
        # browser microphone capture on phones/tablets (mic needs a secure context).
        object.__setattr__(self, "tls_certfile", os.getenv("MONGOOSE_TLS_CERTFILE", ""))
        object.__setattr__(self, "tls_keyfile", os.getenv("MONGOOSE_TLS_KEYFILE", ""))
        # How long Ollama keeps a model resident. "0" unloads immediately, freeing
        # memory so a different role's model can load. Important on a 24 GB machine
        # where chat/tool/code models are mutually exclusive in memory.
        # On a 24GB base M4 the role models are mutually exclusive in memory
        # (chat ~18GB, tool ~16GB, code ~9GB). Default -1 keeps the model loaded
        # permanently so you pay the ~15s load once and every later chat is warm
        # (~2-3s) with no cold start, ever. Cost: ~17GB RAM pinned, leaving ~7GB
        # for the rest of a 24GB machine. Use "10m" to let it unload when idle,
        # or "0" to unload after each request, if you need that RAM back.
        object.__setattr__(self, "ollama_keep_alive", os.getenv("OLLAMA_KEEP_ALIVE", "-1"))
        # Gemma 4 and some other models default to a "thinking" mode that emits
        # reasoning into a separate `thinking` field and can leave `content`
        # empty, which makes replies look silent and slower. Default off for a
        # responsive household assistant; set OLLAMA_THINK=true for deeper
        # reasoning on hard questions.
        object.__setattr__(self, "ollama_think", _bool("OLLAMA_THINK", False))
        # Hard bounds for local model calls. num_predict is the output-token circuit breaker: if
        # a model starts looping or thinking forever, Ollama stops generating instead of letting a
        # chat turn or tool-planning turn monopolise the single local model lane indefinitely.
        object.__setattr__(self, "ollama_request_timeout_s", _float("MONGOOSE_OLLAMA_TIMEOUT_S", 120.0))
        object.__setattr__(self, "ollama_num_predict", _int("MONGOOSE_OLLAMA_NUM_PREDICT", 800))
        object.__setattr__(self, "resource_guard_minutes", _int("MONGOOSE_RESOURCE_GUARD_MINUTES", 5))
        # Model context window (num_ctx). Set explicitly so the conversation token budget
        # maps to a KNOWN size; otherwise Ollama applies its own default (often ~4096) and
        # silently truncates from the front on overflow, which can drop the system prompt.
        object.__setattr__(self, "num_ctx", _int("MONGOOSE_NUM_CTX", 8192))
        # Token budget for replayed conversation history (the real bound on the short-term
        # window). Kept well under num_ctx to leave room for system + facts + prompt + reply.
        object.__setattr__(self, "history_token_budget", _int("MEMORY_HISTORY_TOKEN_BUDGET", 1800))
        # API access control for sensitive routes when the server is exposed
        # beyond localhost. Default closed: localhost-only, no token needed.
        object.__setattr__(self, "allow_remote", _bool("MONGOOSE_ALLOW_REMOTE", False))
        # When true, require a login session even on loopback (the Mac), so the cockpit genuinely
        # locks and times out instead of being open to anyone at the machine. Default off (the Mac
        # console stays frictionless) but available for shared/untrusted-environment setups.
        object.__setattr__(self, "lock_local", _bool("MONGOOSE_LOCK_LOCAL", False))
        object.__setattr__(self, "api_token", os.getenv("MONGOOSE_API_TOKEN", ""))
        # Remote sync channels (chat pro, claude pro, anthropic api, openai api) for the three-a-day
        # sync checkpoints between Mongoose and the owner's Claude/chat. Enabled from day one (override);
        # this is OUTBOUND check-in, distinct from allow_remote which governs inbound API exposure.
        # Individual channels can be disabled with MONGOOSE_SYNC_<CHANNEL>=off.
        object.__setattr__(self, "sync_remote_from_day_one", _bool("MONGOOSE_SYNC_REMOTE_DAY_ONE", True))
        # Outbound sync DELIVERY is a separate, explicit consent from channel availability: a
        # checkpoint summarises your data, so it's only pushed off the machine when this is on.
        # Default OFF , "channels ready" must not silently mean "data is being sent".
        object.__setattr__(self, "sync_delivery_enabled", _bool("MONGOOSE_SYNC_DELIVERY", False))
        # Proactive delight: let the surpriser occasionally lighten a quiet cycle with an
        # interest-aware joke/fact. OFF by default (the surpriser stays silent when nothing real
        # surfaced); on-demand delight ("tell me a joke", mongoose delight) is always available.
        object.__setattr__(self, "proactive_delight_enabled", _bool("MONGOOSE_PROACTIVE_DELIGHT", True))
        # Model-assisted memory: extract durable facts the deterministic catcher
        # misses. Gated by a cheap regex pre-filter (looks_fact_bearing) so only
        # self-disclosing turns ("I like X", "my dog is Y") pay the extra
        # inference; ordinary turns skip it and stay fast. Runs on the resident
        # chat model (no swap). Default on. Set false to disable entirely.
        object.__setattr__(self, "memory_auto_extract", _bool("MEMORY_AUTO_EXTRACT", False))
        # Backwards-compatible default model alias (older callers / tests).
        object.__setattr__(self, "ollama_model", os.getenv("OLLAMA_MODEL", self.ollama_chat_model))
        object.__setattr__(self, "ha_url", os.getenv("HA_URL", ""))
        object.__setattr__(self, "ha_token", os.getenv("HA_TOKEN", ""))
        object.__setattr__(self, "ha_timeout_seconds", _int("HA_TIMEOUT_SECONDS", 4))
        # Protocol v3 (three-agent HA coordination) , SSH transport to the Pi where the shared bus and
        # lock files live. All optional/empty by default: v3 is a PROPOSAL and nothing connects until M
        # configures these and approves the cutover. PI_SSH_HOST like "pi@raspberrypi.local".
        object.__setattr__(self, "pi_ssh_host", os.getenv("PI_SSH_HOST", ""))
        object.__setattr__(self, "pi_ssh_port", _int("PI_SSH_PORT", 22))
        object.__setattr__(self, "pi_ssh_key", os.getenv("PI_SSH_KEY", ""))
        object.__setattr__(self, "pi_ssh_password", os.getenv("PI_SSH_PASSWORD", ""))
        object.__setattr__(self, "pi_config_dir", os.getenv("PI_CONFIG_DIR", "/config"))
        object.__setattr__(self, "protocol_v3_enabled", _bool("PROTOCOL_V3_ENABLED", False))
        # Selected entities the daily brief may read (read-only): locks, doors,
        # thermostat, etc. Empty by default, so home-state in the brief is opt-in
        # and a deliberate selection, not a dump of every entity.
        _bhe = os.getenv("MONGOOSE_BRIEF_HOME_ENTITIES", "")
        object.__setattr__(self, "brief_home_entities",
                           tuple(e.strip() for e in _bhe.split(",") if e.strip()))
        object.__setattr__(self, "alexa_fallback_url", os.getenv("ALEXA_FALLBACK_URL", ""))
        object.__setattr__(self, "alexa_fallback_token", os.getenv("ALEXA_FALLBACK_TOKEN", ""))
        object.__setattr__(self, "alexa_timeout_seconds", _int("ALEXA_TIMEOUT_SECONDS", 5))
        object.__setattr__(self, "ntfy_url", os.getenv("NTFY_URL", "http://127.0.0.1:8090"))
        object.__setattr__(self, "ntfy_topic", os.getenv("NTFY_TOPIC", "mongoose-private-topic"))
        object.__setattr__(self, "ntfy_token", os.getenv("NTFY_TOKEN", ""))

        # Scheduler: when the daily brief fires (local HH:MM), how often email/file
        # jobs run, and which file roots to reindex (comma-separated path or
        # path:device pairs). All have safe defaults so the scheduler works
        # out-of-the-box and does nothing surprising.
        _bt = os.getenv("MONGOOSE_BRIEF_TIME", "07:30")
        try:
            _bh, _bm = (int(x) for x in _bt.split(":", 1))
        except Exception:
            _bh, _bm = 7, 30
        object.__setattr__(self, "brief_time", (_bh, _bm))
        object.__setattr__(self, "surpriser_enabled", os.getenv("MONGOOSE_SURPRISER_ENABLED", "").lower() in ("1", "true", "yes"))
        object.__setattr__(self, "surpriser_minutes", _int("MONGOOSE_SURPRISER_MINUTES", 45))
        object.__setattr__(self, "email_sync_minutes", _int("MONGOOSE_EMAIL_SYNC_MINUTES", 30))
        object.__setattr__(self, "tasks_sync_minutes", _int("MONGOOSE_TASKS_SYNC_MINUTES", 30))
        object.__setattr__(self, "harden_scan_minutes", _int("MONGOOSE_HARDEN_SCAN_MINUTES", 15))
        object.__setattr__(self, "brain_snapshot_hour", _int("MONGOOSE_BRAIN_SNAPSHOT_HOUR", 4))
        object.__setattr__(self, "backup_hour", _int("MONGOOSE_BACKUP_HOUR", 4))
        object.__setattr__(self, "backup_enabled", _bool("MONGOOSE_BACKUP_ENABLED", False))
        object.__setattr__(self, "benchmark_enabled", _bool("MONGOOSE_BENCHMARK_ENABLED", False))
        object.__setattr__(self, "benchmark_window_start", _int("MONGOOSE_BENCHMARK_WINDOW_START", 5))
        object.__setattr__(self, "benchmark_window_end", _int("MONGOOSE_BENCHMARK_WINDOW_END", 8))
        object.__setattr__(self, "benchmark_reveal_minutes", _int("MONGOOSE_BENCHMARK_REVEAL_MINUTES", 90))
        object.__setattr__(self, "reindex_minutes", _int("MONGOOSE_REINDEX_MINUTES", 360))
        _roots = os.getenv("MONGOOSE_INDEX_ROOTS", "")
        object.__setattr__(self, "index_roots",
                           tuple(r.strip() for r in _roots.split(",") if r.strip()))
        object.__setattr__(self, "metro_watch_dir", os.getenv("MONGOOSE_METRO_WATCH_DIR", ""))
        object.__setattr__(self, "metro_archive_dir", os.getenv("MONGOOSE_METRO_ARCHIVE_DIR", ""))
        object.__setattr__(self, "metro_watch_minutes", _int("MONGOOSE_METRO_WATCH_MINUTES", 10))
        object.__setattr__(self, "metro_ob_provider", os.getenv("MONGOOSE_METRO_OB_PROVIDER", ""))
        object.__setattr__(self, "weather_provider_order", os.getenv("WEATHER_PROVIDER_ORDER", "open_meteo,tomorrow,metoffice,weatherkit"))
        object.__setattr__(self, "weather_current_ttl_seconds", _int("WEATHER_CURRENT_TTL_SECONDS", 600))
        object.__setattr__(self, "weather_minutely_ttl_seconds", _int("WEATHER_MINUTELY_TTL_SECONDS", 300))
        object.__setattr__(self, "weather_hourly_ttl_seconds", _int("WEATHER_HOURLY_TTL_SECONDS", 2700))
        object.__setattr__(self, "tomorrow_api_key", os.getenv("TOMORROW_API_KEY", ""))
        object.__setattr__(self, "metoffice_api_key", os.getenv("METOFFICE_API_KEY", ""))
        object.__setattr__(self, "weatherkit_team_id", os.getenv("WEATHERKIT_TEAM_ID", ""))
        object.__setattr__(self, "weatherkit_service_id", os.getenv("WEATHERKIT_SERVICE_ID", ""))
        object.__setattr__(self, "weatherkit_key_id", os.getenv("WEATHERKIT_KEY_ID", ""))
        object.__setattr__(self, "weatherkit_private_key_path", os.getenv("WEATHERKIT_PRIVATE_KEY_PATH", ""))
        object.__setattr__(self, "imap_host", os.getenv("IMAP_HOST", ""))
        object.__setattr__(self, "imap_port", _int("IMAP_PORT", 993))
        object.__setattr__(self, "imap_username", os.getenv("IMAP_USERNAME", ""))
        object.__setattr__(self, "imap_password", os.getenv("IMAP_PASSWORD", ""))
        object.__setattr__(self, "imap_use_ssl", _bool("IMAP_USE_SSL", True))
        object.__setattr__(self, "google_client_secrets_path", os.getenv("GOOGLE_CLIENT_SECRETS_PATH", _default_secret_path("google-client-secrets.json")))
        object.__setattr__(self, "google_token_path", os.getenv("GOOGLE_TOKEN_PATH", _default_secret_path("google-token.json")))
        object.__setattr__(self, "wp_url", os.getenv("WP_URL", ""))
        object.__setattr__(self, "wp_username", os.getenv("WP_USERNAME", ""))
        object.__setattr__(self, "wp_app_password", os.getenv("WP_APP_PASSWORD", ""))
        object.__setattr__(self, "wp_plugin_dir", os.getenv("MONGOOSE_PLUGIN_DIR", ""))
        object.__setattr__(self, "ms_client_id", os.getenv("MS_CLIENT_ID", ""))
        object.__setattr__(self, "ms_tenant_id", os.getenv("MS_TENANT_ID", "common"))
        object.__setattr__(self, "ms_token_cache", os.getenv("MS_TOKEN_CACHE", _default_secret_path("msal-token-cache.bin")))
        # Cloud escalation: local first, then instant cloud models only when
        # explicitly enabled and a request opts in. Default provider order is
        # Claude-first for this user; CLOUD_PROVIDER_ORDER is accepted as a
        # compatibility alias for earlier Pro branches.
        object.__setattr__(self, "anthropic_api_key", os.getenv("ANTHROPIC_API_KEY", ""))
        object.__setattr__(self, "anthropic_instant_model", os.getenv("ANTHROPIC_INSTANT_MODEL", "claude-haiku-4-5-20251001"))
        object.__setattr__(self, "openai_api_key", os.getenv("OPENAI_API_KEY", ""))
        object.__setattr__(self, "openai_instant_model", os.getenv("OPENAI_INSTANT_MODEL", "gpt-5.4-nano"))
        object.__setattr__(self, "google_api_key", os.getenv("GOOGLE_API_KEY", ""))
        object.__setattr__(self, "google_instant_model", os.getenv("GOOGLE_INSTANT_MODEL", "gemini-2.5-flash-lite"))
        # Order Mongoose tries cloud providers. Default honours "my Claude first"
        # (your stated preference); set to "google,anthropic,openai" for
        # cheapest-first. Only providers with a configured key are attempted.
        cloud_order = (
            os.getenv("CLOUD_ESCALATION_ORDER")
            or os.getenv("CLOUD_PROVIDER_ORDER")
            or "anthropic,openai,google"
        )
        object.__setattr__(self, "cloud_escalation_order", cloud_order)
        object.__setattr__(self, "cloud_provider_order", cloud_order)
        object.__setattr__(self, "cloud_escalation_enabled", _bool("CLOUD_ESCALATION_ENABLED", False))
        # Hard per-turn budget in pennies (GBP). A turn that is estimated to cost
        # more than this is refused before any cloud call is made.
        object.__setattr__(self, "cloud_turn_budget_pennies", _float("CLOUD_TURN_BUDGET_PENNIES", 2.0))
        object.__setattr__(self, "claude_code_cmd", os.getenv("CLAUDE_CODE_CMD", "claude"))
        object.__setattr__(self, "build_step_timeout_s", int(_float("BUILD_STEP_TIMEOUT_S", 1800)))
        # Remote (Pi) build node: where on the Pi to stage build repos, and the Claude Code command
        # there (defaults to the same `claude` binary name). The Pi is always-on and its Claude Code
        # uses the subscription, so it's the natural place to offload overnight/agentic builds.
        object.__setattr__(self, "pi_build_dir", os.getenv("PI_BUILD_DIR", "~/mongoose-builds"))
        object.__setattr__(self, "pi_claude_code_cmd", os.getenv("PI_CLAUDE_CODE_CMD", "claude"))
        object.__setattr__(self, "build_code_call_budget", int(_float("BUILD_CODE_CALL_BUDGET", 40)))
        object.__setattr__(self, "build_run_max_seconds", int(_float("BUILD_RUN_MAX_SECONDS", 43200)))
        object.__setattr__(self, "shared_file_roots", os.getenv("MONGOOSE_SHARED_FILE_ROOTS", ""))
        object.__setattr__(self, "tts_cmd", os.getenv("MONGOOSE_TTS_CMD", "say"))
        object.__setattr__(self, "tts_voice", os.getenv("MONGOOSE_TTS_VOICE", ""))
        object.__setattr__(self, "tts_rate", os.getenv("MONGOOSE_TTS_RATE", ""))
        object.__setattr__(self, "stt_model", os.getenv("MONGOOSE_STT_MODEL", "base.en"))
        object.__setattr__(self, "wake_word", os.getenv("MONGOOSE_WAKE_WORD", "mongoose"))
        object.__setattr__(self, "mic_threshold", os.getenv("MONGOOSE_MIC_THRESHOLD", "2%"))
        object.__setattr__(self, "mic_silence_s", _float("MONGOOSE_MIC_SILENCE_S", 1.5))
        # Output ceiling so the worst-case cost of a single call is bounded.
        object.__setattr__(self, "cloud_max_output_tokens", _int("CLOUD_MAX_OUTPUT_TOKENS", 600))

    @property
    def provider_names(self) -> list[str]:
        return [item.strip() for item in self.weather_provider_order.split(",") if item.strip()]

    @property
    def cloud_provider_names(self) -> list[str]:
        return [item.strip().lower() for item in self.cloud_escalation_order.split(",") if item.strip()]

    @property
    def keep_alive_value(self) -> object:
        """Ollama's keep_alive accepts a duration string (e.g. "10m") OR a bare
        number of seconds, with -1 meaning 'keep loaded forever'. A bare number
        must be sent as a JSON number, not a string, or Ollama rejects it. This
        returns an int when the configured value is a plain integer, else the
        string unchanged.
        """
        v = self.ollama_keep_alive.strip()
        try:
            return int(v)
        except ValueError:
            return v

    def model_for_role(self, role: str) -> str:
        """Resolve a model tag for a logical role.

        Roles: 'chat' (persona/default), 'tool' (tool-routing/reasoning),
        'code' (coding). Unknown roles fall back to the chat model.

        A Lane 2 (model-tuning) OVERRIDE, if present, wins over the configured base. Overrides
        are a reversible layer (see model_tuning): applying a recommendation sets one and
        reverting clears it, so the user's base .env is never rewritten.
        """
        mapping = {
            "chat": self.ollama_chat_model,
            "tool": self.ollama_tool_model,
            "code": self.ollama_code_model,
            "research": self.ollama_research_model,
        }
        base = mapping.get(role, self.ollama_chat_model)
        try:
            from .model_tuning import get_model_overrides
            override = get_model_overrides().get(role)
            if override:
                return override
        except Exception:
            pass
        return base

    @property
    def data_dir(self) -> Path:
        return self.db_path.parent

    # Per-install identity defaults. An explicit environment value wins exactly as before; when it is
    # unset the default is the site profile's value instead of a literal in code.
    @property
    def home_lat(self) -> float | None:
        return _float_or_profile("MONGOOSE_HOME_LAT", "site.home_lat")

    @property
    def home_lon(self) -> float | None:
        return _float_or_profile("MONGOOSE_HOME_LON", "site.home_lon")

    @property
    def owner_id(self) -> str:
        value = os.getenv("MONGOOSE_OWNER_ID")
        if value is not None:
            return value
        from . import profile as _profile
        return _profile.owner_id()


def _float_or_profile(name: str, dotted: str) -> float | None:
    """_float(name, <profile value>) with the profile read only when the env value is missing or invalid.
    An unreadable profile gives None (home location unknown) rather than a made-up location."""
    value = os.getenv(name)
    if value:
        try:
            return float(value)
        except ValueError:
            pass
    try:
        from . import profile as _profile
        return float(_profile.get(dotted))
    except Exception:
        return None


def site_domain() -> str:
    """The tenant's public site/mail domain, as CONFIG not a code literal. PC_SITE_DOMAIN wins when set;
    otherwise the site profile's domain. Compose off this (e.g. f"info@{site_domain()}",
    f"guest.{site_domain()}") instead of hardcoding."""
    value = os.getenv("PC_SITE_DOMAIN")
    if value is not None:
        return value
    from . import profile as _profile
    return _profile.site_domain()


# ---------------------------------------------------------------------------------------------------------
# Site-profile copy rendering for served text: Cockpit pages, tool and MCP descriptions, job descriptions.
# Copy carries "{{site.domain}}"-style placeholders (mongoose.profile.render), plus a few RTM tokens the
# profile has no scalar key for. Upper-case tokens never match profile.render's placeholder pattern, so a
# caller that forgets this helper shows a raw token instead of crashing a page.
#   {{RTM_BLOCK_A}} / {{RTM_BLOCK_B}}  first / last block code, for flat-id examples ("{{RTM_BLOCK_A}}F1")
#   {{RTM_COMPACT}}                    the RTM name lower-cased with spaces removed, a search keyword
#   {{RTM_NAME_LOWER}}                 the RTM name lower-cased (search keywords, example utterances)
#   {{OWNER_NAME_LOWER}}               the owner's name lower-cased, as typed to address them
# Rendered text is cached per (profile file, mtime), so a served page renders once per profile.
# ---------------------------------------------------------------------------------------------------------
import re as _re

_COPY_PLACEHOLDER = _re.compile(r"\{\{\s*([a-z_]+(?:\.[a-z_]+)+)\s*\}\}")  # profile.render's shape
_COPY_CACHE: dict = {}


def profile_copy_key() -> tuple:
    """Changes whenever the active site profile (its path or its file) changes; a cache key for copy."""
    from . import profile as _profile
    p = _profile.profile_path()
    try:
        mtime = p.stat().st_mtime_ns
    except OSError:
        mtime = None
    return (str(p), mtime)


def profile_copy_tokens() -> dict[str, str]:
    from . import profile as _profile
    codes = _profile.block_codes()
    return {
        "{{RTM_BLOCK_A}}": codes[0],
        "{{RTM_BLOCK_B}}": codes[-1],
        "{{RTM_COMPACT}}": _re.sub(r"[^a-z0-9]", "", _profile.rtm_name().lower()),
        "{{RTM_NAME_LOWER}}": _profile.rtm_name().lower(),
        "{{OWNER_NAME_LOWER}}": _profile.owner_name().strip().lower(),
    }


def _html_safe(value: str) -> str:
    # Entity-escape so a profile value can never break the HTML or a JS string/template literal it sits in.
    import html as _html
    return _html.escape(value, quote=True).replace("\\", "&#92;").replace("`", "&#96;")


def render_profile_copy(text: str, *, html: bool = False, extra: dict[str, str] | None = None) -> str:
    """Fill profile placeholders and RTM tokens in served copy. html=True entity-escapes every profile
    value (use for Cockpit HTML/JS). `extra` maps further tokens to text inserted verbatim (the caller
    owns its escaping). Unknown {{a.b}} placeholders fail loud, as in profile.render."""
    if not text or "{{" not in text:
        return text
    extra_items = tuple(sorted((extra or {}).items()))
    key = (profile_copy_key(), html, extra_items, text)
    hit = _COPY_CACHE.get(key)
    if hit is not None:
        return hit
    from . import profile as _profile
    out = text
    for tok, val in profile_copy_tokens().items():
        if tok in out:
            out = out.replace(tok, _html_safe(val) if html else val)
    for tok, val in extra_items:
        if tok in out:
            out = out.replace(tok, val)
    if html:
        out = _COPY_PLACEHOLDER.sub(lambda m: _html_safe(_profile.render(m.group(0))), out)
    else:
        out = _profile.render(out)
    if len(_COPY_CACHE) > 256:
        _COPY_CACHE.clear()
    _COPY_CACHE[key] = out
    return out


def get_settings() -> Settings:
    # An EMPTY value in os.environ (e.g. exported blank by a launch wrapper, or left from a prior
    # partial run) carries no information, but with load_dotenv(override=False) it would SHADOW a
    # real value in .env , so the running server, doctor and readiness would see "" while the setup
    # badge (which merges differently) sees the real value. That divergence is a claim-integrity bug
    # ("configured" in one surface, "not configured" in another). So: for any key that .env actually
    # supplies, drop a blank process value first, making .env authoritative and every surface
    # consistent. Unrelated empty env vars are left untouched.
    import os as _os
    _ep = None
    try:
        from dotenv import dotenv_values
        from .setup_wizard import ENV_PATH as _EP
        _ep = _EP if _EP.exists() else None
        _env_keys = set(dotenv_values(_ep).keys()) if _ep is not None else set()
    except Exception:
        _env_keys = set()
    for _k in _env_keys:
        if _os.environ.get(_k, None) == "":
            _os.environ.pop(_k, None)
    # Load from the SAME path we just read keys from, so the clear and the load are consistent.
    _pre = {k: (k in _os.environ) for k in _BRAIN_LOCATION_KEYS}
    if _ep is not None:
        load_dotenv(_ep, override=False)
    else:
        load_dotenv(override=False)
    # The import-time scrub alone is not enough: this second load would quietly reinstate a
    # refused brain override on every get_settings() call. Same rule, same recording, here too.
    _scrub_brain_overrides(_pre)
    return Settings()
